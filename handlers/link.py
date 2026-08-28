"""Link-forward flow.

When the admin sends a t.me/c/<id>/<msg_id> or t.me/<channel>/<msg_id> URL:
  * Parse it
  * Use the Telethon user session to fetch the message(s)
  * Download media to temp files (since locked channels often disable
    forwarding — we have to re-upload rather than forward)
  * Create a pending forward with the downloaded file paths + caption
  * Show the inline topic picker
"""
from __future__ import annotations

import logging
import os
import tempfile
import time
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes, MessageHandler, filters

from user_session import parse_telegram_link, UserSession
from db import Database

logger = logging.getLogger(__name__)

LINK_FILTER = filters.Regex(r"(https?://)?t(?:elegram)?\.me/")

# Heuristic: in a single user message, accept up to ~50MB total to keep
# memory / disk usage reasonable. Real limit is the Bot API's 50MB upload
# cap for documents / 2GB for videos sent via sendVideo by URL — but we're
# re-uploading from disk, so the cap is 50MB.
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detect a t.me link in the user's message and pull the content via
    the Telethon user session."""
    if not update.effective_message or not update.effective_user:
        return
    cfg = context.bot_data["config"]
    user_id = update.effective_user.id
    if not cfg.is_admin(user_id):
        return

    text = update.effective_message.text or update.effective_message.caption or ""
    user_session: UserSession = context.bot_data.get("user_session")
    if not user_session or not user_session.available:
        await update.effective_message.reply_text(
            "Locked-channel forwarding needs a Telethon user session, but "
            "none is configured. Run `python login.py` first."
        )
        return

    parsed = parse_telegram_link(text)
    if not parsed:
        await update.effective_message.reply_text(
            "Couldn't parse that link. Supported formats:\n"
            "• https://t.me/c/1234567890/42  (private channel post)\n"
            "• https://t.me/channelname/42   (public channel post)\n"
            "\nNote: invite-only links like t.me/+abc... can't be auto-fetched "
            "because we can't join chats for you."
        )
        return

    status = await update.effective_message.reply_text(
        f"Fetching message from {parsed.chat_ref} / {parsed.message_id} ..."
    )
    try:
        fetched = await user_session.fetch_message(parsed)
    except Exception as e:
        logger.exception("fetch_message failed")
        await status.edit_text(f"Failed to fetch: {e}")
        return

    if not fetched:
        await status.edit_text(
            "Couldn't fetch that message. Make sure your user account is a "
            "member of the channel and the link points to a real post."
        )
        return

    msg = fetched["message"]
    album = fetched.get("album") or []

    # Download media (if any) to temp files
    tmp_dir = tempfile.mkdtemp(prefix="forwarder_")
    media_paths: list[dict] = []
    caption: str | None = ""
    text_only: str | None = None

    messages_to_handle = album if album else [msg]
    for idx, m in enumerate(messages_to_handle):
        # Capture caption from the first message
        if idx == 0:
            caption = m.message  # caption / text

        media_path = await _download_media(user_session, m, tmp_dir, idx)
        if media_path:
            media_paths.append(media_path)
        elif m.message and not media_paths:
            # Bug fix: previously the condition was `elif not messages_to_handle
            # and m.message:` — but `messages_to_handle` is always truthy
            # (it's at least `[msg]`), so this branch was dead code and
            # `text_only` was never set. As a result, text-only posts in
            # locked channels were forwarded with `media_paths=[]` and
            # `text=None`, which triggered "Empty link payload" in
            # _forward_link.
            #
            # Fix: only set text_only if we haven't successfully downloaded
            # any media yet. For albums, the first media item's caption
            # wins.
            text_only = m.message

    if not media_paths and not text_only and not caption:
        await status.edit_text("That message has no viewable content.")
        return

    # Show diagnostic info so the user knows if media was actually downloaded
    if not media_paths and (text_only or caption):
        # text-only post — no media to forward
        pass
    elif not media_paths:
        # We expected media but got none — downloads failed silently
        await status.edit_text(
            "Could not download media from that post. The bot's user account "
            "may not have access to the file, or the file is too large "
            "(>50MB Bot API limit). Falling back to text only."
        )
        # Continue and let _forward_link send the text fallback

    payload = {
        "media_paths": media_paths,
        "caption": caption,
        "text": text_only,
        "source_chat_id": fetched["chat_id"],
        "source_message_id": parsed.message_id,
    }

    db: Database = context.bot_data["db"]
    pending_id = await db.create_pending(
        user_id=user_id,
        source_chat_id=fetched["chat_id"],
        source_message_id=parsed.message_id,
        payload=payload,
        kind="link",
    )

    # Schedule cleanup of temp files after a generous window (30 min)
    context.application.job_queue.run_once(
        _cleanup_tmp, when=30 * 60, data={"dir": tmp_dir},
        name=f"cleanup-{pending_id}",
    )

    # Show topic picker
    topics_mgr = context.bot_data["topics"]
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        await status.edit_text("No destination group set. Use /setgroup <group_id>.")
        return
    topics = await topics_mgr.get_topics(group_id)
    if not topics:
        await status.edit_text("No topics found. /refresh or /addtopic first.")
        return
    keyboard = topics_mgr.build_keyboard(pending_id, topics)
    n = len(media_paths)
    if n == 1:
        m = media_paths[0]
        # Show media type so the user knows what's about to be sent
        label = f"Fetched 1 {m['type']} — pick a topic:"
    elif n > 1:
        types = [m["type"] for m in media_paths]
        # Compact summary like "2 photo, 1 video"
        type_counts: dict[str, int] = {}
        for t in types:
            type_counts[t] = type_counts.get(t, 0) + 1
        summary = ", ".join(f"{c} {t}" for t, c in type_counts.items())
        label = f"Fetched {n} items ({summary}) — pick a topic:"
    else:
        # No media — will be sent as text
        text_preview = (text_only or caption or "")[:50]
        if text_preview:
            label = f"Fetched text only (no media): \"{text_preview}...\" — pick a topic:"
        else:
            label = "Fetched text message — pick a topic:"
    await status.edit_text(label, reply_markup=keyboard)


async def _download_media(user_session: UserSession, msg, tmp_dir: str, idx: int) -> Optional[dict]:
    """Download the media attached to a Telethon Message to disk. Returns a
    dict {'path': str, 'type': str} or None if the message has no media."""
    from telethon.tl import types as tl

    if not msg or not msg.media:
        return None
    # Skip web pages / contacts / geos (no downloadable file)
    if isinstance(msg.media, (tl.MessageMediaWebPage, tl.MessageMediaContact,
                               tl.MessageMediaGeo, tl.MessageMediaVenue,
                               tl.MessageMediaGame, tl.MessageMediaPoll,
                               tl.MessageMediaUnsupported)):
        return None

    # Determine type
    media_type = "document"
    if isinstance(msg.media, tl.MessageMediaPhoto):
        media_type = "photo"
    elif isinstance(msg.media, tl.MessageMediaDocument):
        doc = msg.media.document
        if doc and doc.mime_type:
            mt = doc.mime_type
            if mt.startswith("video/"):
                # check for video vs animation (gif)
                for attr in doc.attributes:
                    if isinstance(attr, tl.DocumentAttributeAnimated):
                        media_type = "animation"
                        break
                    if isinstance(attr, tl.DocumentAttributeVideo):
                        # round video -> video_note; for now treat as video
                        if getattr(attr, "round", False):
                            media_type = "video_note"  # send as video for simplicity
                        else:
                            media_type = "video"
                        break
            elif mt.startswith("audio/"):
                if "ogg" in mt:
                    media_type = "voice"
                else:
                    media_type = "audio"
    elif isinstance(msg.media, tl.MessageMediaWebPage):
        return None

    # Download
    out_path = os.path.join(tmp_dir, f"media_{idx}_{int(time.time())}")
    try:
        result = await user_session.client.download_media(msg, file=out_path)
    except Exception as e:
        logger.warning("download_media failed: %s", e)
        return None
    if not result:
        return None
    if isinstance(result, bytes):
        out_path = out_path  # bytes path, save to disk
        with open(out_path, "wb") as f:
            f.write(result)
    else:
        out_path = str(result)

    # Check size
    try:
        sz = os.path.getsize(out_path)
    except OSError:
        return None
    if sz > MAX_DOWNLOAD_BYTES:
        logger.warning("Skipping media (%d bytes) — exceeds %d limit",
                       sz, MAX_DOWNLOAD_BYTES)
        try:
            os.remove(out_path)
        except OSError:
            pass
        return None

    return {"path": out_path, "type": media_type}


async def _cleanup_tmp(context) -> None:
    """JobQueue callback: remove temp dir after 30 min."""
    job = context.job
    if not job:
        return
    tmp_dir = job.data.get("dir") if job.data else None
    if not tmp_dir:
        return
    import shutil
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.info("Cleaned up %s", tmp_dir)
    except Exception:
        logger.exception("cleanup failed for %s", tmp_dir)
