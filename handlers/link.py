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

import asyncio
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
        f"🔗 Fetching message from `{parsed.chat_ref}` / msg `{parsed.message_id}`...",
        parse_mode="Markdown",
    )
    try:
        fetched, diag = await user_session.fetch_message(parsed)
    except Exception as e:
        logger.exception("fetch_message failed")
        await status.edit_text(
            f"❌ Failed to fetch: `{type(e).__name__}: {e}`\n\n"
            f"This is unexpected — the fetch_message method should have caught this. "
            f"Check the bot logs.",
            parse_mode="Markdown",
        )
        return

    if not fetched:
        # Show the full step-by-step diagnostic log to the user so they can
        # pinpoint exactly where the fetch failed. This is the key change —
        # previously the user just got "Couldn't fetch that message" with
        # no detail.
        diag_text = "\n".join(diag)
        # Telegram messages have a 4096-char limit. Truncate if needed.
        if len(diag_text) > 3800:
            diag_text = diag_text[:3800] + "\n... (truncated)"
        await status.edit_text(
            f"❌ Couldn't fetch that message.\n\n"
            f"📋 Step-by-step diagnostic:\n```\n{diag_text}\n```",
            parse_mode="Markdown",
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
    download_errors: list[str] = []
    for idx, m in enumerate(messages_to_handle):
        # Capture caption from the first message
        if idx == 0:
            caption = m.message  # caption / text

        media_path, dl_err = await _download_media(user_session, m, tmp_dir, idx)
        if media_path:
            media_paths.append(media_path)
        elif dl_err:
            download_errors.append(f"  • item {idx}: {dl_err}")
        elif m.message and not media_paths:
            text_only = m.message

    if not media_paths and not text_only and not caption:
        msg_text = "That message has no viewable content."
        if download_errors:
            msg_text += "\n\nDownload errors:\n" + "\n".join(download_errors)
        await status.edit_text(msg_text)
        return

    # Show diagnostic info so the user knows if media was actually downloaded
    if not media_paths and (text_only or caption):
        # text-only post — no media to forward
        pass
    elif not media_paths:
        err_text = "\n".join(download_errors[:5]) if download_errors else "(no specific errors)"
        await status.edit_text(
            f"⚠️ Could not download media from that post.\n\n"
            f"Errors:\n{err_text}\n\n"
            f"Common causes:\n"
            f"  • File is too large (>50MB Bot API limit for re-upload)\n"
            f"  • Telethon session expired — re-run `python login.py --string`\n"
            f"  • Render free tier timeout (60s webhook limit)\n\n"
            f"Falling back to text only."
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

    # Schedule cleanup of temp files after 30 minutes.
    #
    # Previously used: context.application.job_queue.run_once(...)
    # But job_queue is None in our custom webhook mode (PTB only initializes
    # JobQueue when you call run_polling / run_webhook, AND only if the
    # 'job-queue' extra is installed). This caused:
    #   Internal error: 'NoneType' object has no attribute 'run_once'
    # whenever a user sent a t.me link to a locked channel.
    #
    # Fix: just use asyncio.create_task + asyncio.sleep. Simpler, no
    # extra dependencies, and works in both webhook and polling modes.
    asyncio.create_task(_cleanup_tmp_later(tmp_dir, 30 * 60))

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


async def _download_media(user_session: UserSession, msg, tmp_dir: str, idx: int) -> tuple[Optional[dict], Optional[str]]:
    """Download the media attached to a Telethon Message to disk.

    Returns a tuple (result, error):
      - result: dict {'path': str, 'type': str} on success, None on failure
      - error: str | None — human-readable error message if download failed,
                or None if there was simply no media (not an error)

    The error string is what's new — previously the function returned None
    for both "no media" and "download failed", which made it impossible to
    surface the actual cause to the user. Now the caller can distinguish
    and show the user exactly why the download failed.
    """
    from telethon.tl import types as tl

    if not msg or not msg.media:
        return None, None  # no media — not an error
    # Skip web pages / contacts / geos (no downloadable file)
    if isinstance(msg.media, (tl.MessageMediaWebPage, tl.MessageMediaContact,
                               tl.MessageMediaGeo, tl.MessageMediaVenue,
                               tl.MessageMediaGame, tl.MessageMediaPoll,
                               tl.MessageMediaUnsupported)):
        return None, f"unsupported media type: {type(msg.media).__name__}"

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
        return None, "media is a web page (no file to download)"

    # Get file size BEFORE downloading if possible — avoids downloading
    # a huge file only to reject it for being too large
    try:
        if isinstance(msg.media, tl.MessageMediaDocument) and msg.media.document:
            file_size = msg.media.document.size or 0
            if file_size > MAX_DOWNLOAD_BYTES:
                mb = file_size / (1024 * 1024)
                return None, f"file too large ({mb:.1f} MB > {MAX_DOWNLOAD_BYTES/1024/1024:.0f} MB limit)"
        elif isinstance(msg.media, tl.MessageMediaPhoto) and msg.media.photo:
            # Photo sizes are in msg.media.photo.sizes; the largest is the
            # last one (or one with type='x')
            sizes = msg.media.photo.sizes
            if sizes:
                largest = sizes[-1]
                file_size = getattr(largest, "size", 0) or 0
                if file_size > MAX_DOWNLOAD_BYTES:
                    mb = file_size / (1024 * 1024)
                    return None, f"photo too large ({mb:.1f} MB > {MAX_DOWNLOAD_BYTES/1024/1024:.0f} MB limit)"
    except Exception:
        pass  # best-effort size check; proceed with download

    # Download
    out_path = os.path.join(tmp_dir, f"media_{idx}_{int(time.time())}")
    try:
        result = await user_session.client.download_media(msg, file=out_path)
    except Exception as e:
        logger.warning("download_media failed: %s", e)
        return None, f"download failed: {type(e).__name__}: {e}"
    if not result:
        return None, "download returned no result"
    if isinstance(result, bytes):
        out_path = out_path  # bytes path, save to disk
        with open(out_path, "wb") as f:
            f.write(result)
    else:
        out_path = str(result)

    # Check size
    try:
        sz = os.path.getsize(out_path)
    except OSError as e:
        return None, f"could not get file size: {e}"
    if sz > MAX_DOWNLOAD_BYTES:
        logger.warning("Skipping media (%d bytes) — exceeds %d limit",
                       sz, MAX_DOWNLOAD_BYTES)
        try:
            os.remove(out_path)
        except OSError:
            pass
        mb = sz / (1024 * 1024)
        return None, f"downloaded file too large ({mb:.1f} MB > {MAX_DOWNLOAD_BYTES/1024/1024:.0f} MB Bot API limit)"

    return {"path": out_path, "type": media_type}, None


async def _cleanup_tmp_later(tmp_dir: str, delay_seconds: int) -> None:
    """Wait `delay_seconds`, then delete the temp directory and all files in it.

    Replacement for the old JobQueue-based cleanup. We use asyncio.sleep
    because the PTB JobQueue is None in our custom webhook mode (PTB only
    initializes it for run_polling / run_webhook, and only if the
    'job-queue' extra is installed).

    This is a fire-and-forget task scheduled via asyncio.create_task — the
    caller doesn't await it. If the process exits before the delay elapses,
    the cleanup doesn't run, but that's OK because we use /tmp which the
    OS cleans up, and Render's filesystem is ephemeral anyway.
    """
    try:
        await asyncio.sleep(delay_seconds)
    except asyncio.CancelledError:
        return
    import shutil
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.info("Cleaned up %s", tmp_dir)
    except Exception:
        logger.exception("cleanup failed for %s", tmp_dir)
