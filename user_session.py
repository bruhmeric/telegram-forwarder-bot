"""Telethon user-session manager.

Used to:
  * Enumerate forum topics in the destination group (requires the user account
    to be a member of that group)
  * Fetch messages from locked / private channels the user account is a member
    of, including channels where forwarding is disabled by the admin

Login flow is handled by `login.py` separately so that bot.py never needs to
prompt interactively.
"""
from __future__ import annotations

import inspect
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

from telethon import TelegramClient
from telethon.errors import ChannelPrivateError, InviteHashInvalidError
from telethon.tl import types as tl
from telethon.tl.functions.messages import GetForumTopicsRequest

logger = logging.getLogger(__name__)


# ---------- link parsing ----------

# Private channel link: https://t.me/c/1234567890/42
PRIVATE_LINK_RE = re.compile(
    r"(?:https?://)?t(?:elegram)?\.me/c/(\d+)/(\d+)(?:/(\d+))?",
    re.IGNORECASE,
)
# Public channel link: https://t.me/channelname/42
PUBLIC_LINK_RE = re.compile(
    r"(?:https?://)?t(?:elegram)?\.me/([A-Za-z][A-Za-z0-9_]{3,})/(\d+)(?:/(\d+))?",
    re.IGNORECASE,
)


@dataclass
class ParsedLink:
    kind: str  # 'private' | 'public' | 'invite'
    chat_ref: str  # '-1001234567890' or 'channelname' or invite hash
    message_id: int
    # For albums: a list of message ids, else None
    extra_message_ids: Optional[list[int]] = None


def parse_telegram_link(url: str) -> Optional[ParsedLink]:
    """Return a ParsedLink if `url` looks like a t.me deep link to a post."""
    url = url.strip()
    m = PRIVATE_LINK_RE.search(url)
    if m:
        # Telegram private channel link format: t.me/c/<raw_id>/<msg_id>
        # Bot API chat_id = -100 concatenated with raw_id, i.e. -1e12 - raw_id
        raw_id = int(m.group(1))
        chat_id = -1_000_000_000_000 - raw_id
        msg_id = int(m.group(2))
        return ParsedLink(kind="private", chat_ref=str(chat_id), message_id=msg_id)
    m = PUBLIC_LINK_RE.search(url)
    if m:
        # Skip if this is actually an invite link like t.me/+abc...; the regex
        # already requires the first char to be a letter, so 'abc' style invites
        # won't match — but we still need to reject '+' and 'joinchat' links.
        username = m.group(1)
        if username.lower() in ("joinchat", "share", "addstickers", "setlanguage"):
            return None
        msg_id = int(m.group(2))
        return ParsedLink(kind="public", chat_ref=username, message_id=msg_id)
    # t.me/+abc... (private invite, no message_id) — we don't auto-join, skip
    return None


# Regex for channel-only links (no message_id):
# https://t.me/c/1234567890  (private channel, no msg_id)
# https://t.me/channelname   (public channel, no msg_id)
PRIVATE_CHANNEL_ONLY_RE = re.compile(
    r"(?:https?://)?t(?:elegram)?\.me/c/(\d+)/?$",
    re.IGNORECASE,
)
PUBLIC_CHANNEL_ONLY_RE = re.compile(
    r"(?:https?://)?t(?:elegram)?\.me/([A-Za-z][A-Za-z0-9_]{3,})/?$",
    re.IGNORECASE,
)


def parse_channel_link(url: str) -> Optional[ParsedLink]:
    """Parse a channel-only link (no message_id) — used by /scrape.

    Returns a ParsedLink with message_id=0 (signaling "no specific message")
    or None if the URL doesn't match.
    """
    url = url.strip()
    # Strip any trailing query string or fragment
    url = url.split("?")[0].split("#")[0]

    m = PRIVATE_CHANNEL_ONLY_RE.match(url)
    if m:
        raw_id = int(m.group(1))
        chat_id = -1_000_000_000_000 - raw_id
        return ParsedLink(kind="private", chat_ref=str(chat_id), message_id=0)

    m = PUBLIC_CHANNEL_ONLY_RE.match(url)
    if m:
        username = m.group(1)
        if username.lower() in ("joinchat", "share", "addstickers", "setlanguage"):
            return None
        return ParsedLink(kind="public", chat_ref=username, message_id=0)

    return None


# ---------- session manager ----------

class UserSession:
    def __init__(self, session_name: str, api_id: int, api_hash: str,
                 session_string: Optional[str] = None) -> None:
        """Create a Telethon client backed by either a file-based session
        (session_name) or a StringSession (session_string). StringSession is
        preferred for ephemeral filesystems like Render's free tier."""
        if session_string:
            from telethon.sessions import StringSession
            self.client = TelegramClient(StringSession(session_string), api_id, api_hash)
            self._uses_string_session = True
        else:
            self.client = TelegramClient(session_name, api_id, api_hash)
            self._uses_string_session = False
        self._started = False

    async def start(self) -> bool:
        """Connect using an existing session. Returns False if no session
        exists or the session is invalid. Callers should NOT call interactive
        login here — that's done by login.py."""
        if not self._uses_string_session and not os.path.exists(self.session_filename):
            logger.warning("Telethon session file missing — run python login.py first.")
            return False
        try:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                logger.warning("Telethon session exists but is not authorized.")
                await self.client.disconnect()
                return False
            self._started = True
            logger.info("Telethon user session connected as %s (mode=%s)",
                        await self._safe_get_me(),
                        "string" if self._uses_string_session else "file")
            return True
        except Exception:
            logger.exception("Failed to start Telethon client")
            return False

    async def _safe_get_me(self) -> str:
        try:
            me = await self.client.get_me()
            return f"@{me.username} ({me.id})" if me else "?"
        except Exception:
            return "?"

    @property
    def session_filename(self) -> str:
        if self._uses_string_session:
            return "<string-session>"
        # Telethon stores session in <session_name>.session
        try:
            return f"{self.client.session.filename}.session" if (
                hasattr(self.client, "session") and self.client.session
            ) else f"{self.client.api_id}.session"
        except Exception:
            return "user_session.session"

    async def stop(self) -> None:
        if self._started:
            await self.client.disconnect()
            self._started = False

    @property
    def available(self) -> bool:
        return self._started

    # ---------- forum topic enumeration ----------

    async def list_forum_topics(self, chat_id: int) -> list[dict]:
        """Return list of {'id': int, 'title': str} for all forum topics except
        the General topic (id=1)."""
        try:
            peer = await self.client.get_input_entity(chat_id)
        except Exception:
            logger.exception("get_input_entity failed for chat_id=%s", chat_id)
            return []

        topics: list[dict] = []
        offset_id = 0
        # Paginate up to 500 topics
        for _ in range(10):
            try:
                result = await self.client(GetForumTopicsRequest(
                    peer=peer,
                    offset_date=0,
                    offset_id=offset_id,
                    offset_topic=0,
                    limit=100,
                ))
            except Exception:
                logger.exception("GetForumTopicsRequest failed")
                break
            for t in result.topics:
                # Skip General topic (id=1)
                if getattr(t, "id", 0) == 1:
                    continue
                title = getattr(t, "title", None) or f"Topic {t.id}"
                topics.append({"id": t.id, "title": title})
            # GetForumTopics returns topics ordered by creation date; we use
            # offset_id = last topic's top_message id, but Telethon's wrapper
            # already handles pagination internally in most cases. For
            # simplicity, we break if fewer than 100 returned.
            if len(result.topics) < 100:
                break
            # advance offset — use the last topic's top_message id
            offset_id = getattr(result.topics[-1], "top_message", 0) or 0
        return topics

    # ---------- message fetching for locked channels ----------

    async def fetch_message(self, parsed: ParsedLink) -> tuple[Optional[dict], list[str]]:
        """Fetch a single message (and possibly its album siblings) from a
        private / public channel the user account is a member of.

        Returns a tuple of (result_or_none, diagnostics_log) where:
          - result_or_none is a dict with keys:
              chat_id: int (negative for channels)
              message: telethon Message object (single)
              album: list[Message] | None  (album siblings if any)
            ...or None if fetch failed
          - diagnostics_log is a list of human-readable strings describing
            each step that was attempted (for surfacing to the user when
            something fails)

        The diagnostics_log is what made the difference — previously the bot
        would say "Couldn't fetch that message" with no detail. Now the user
        gets the full step-by-step trace so they can pinpoint the failure
        (e.g. "Step 2 failed: ChannelPrivateError - you're not a member").
        """
        diag: list[str] = []
        diag.append(f"Step 1: Parsed link → kind={parsed.kind}, chat_ref={parsed.chat_ref}, msg_id={parsed.message_id}")

        # Step 2: resolve entity
        try:
            if parsed.kind == "private":
                chat_id = int(parsed.chat_ref)
                diag.append(f"Step 2: Resolving entity for chat_id={chat_id} via Telethon get_entity()...")
                # Use get_entity instead of get_input_entity — it makes a
                # network call to resolve unknown entities (e.g. channels the
                # user just joined that aren't in Telethon's session cache).
                # get_input_entity only uses the cache and can fail for new
                # channels.
                entity = await self.client.get_entity(chat_id)
            else:
                diag.append(f"Step 2: Resolving entity for username={parsed.chat_ref} via Telethon get_entity()...")
                entity = await self.client.get_entity(parsed.chat_ref)
            diag.append(f"Step 2: ✓ Entity resolved → {type(entity).__name__} (id={getattr(entity, 'id', '?')})")
        except ChannelPrivateError as e:
            diag.append(f"Step 2: ✗ FAILED — ChannelPrivateError: {e}")
            diag.append("  → Your user account is NOT a member of this chat, or the chat doesn't exist.")
            return None, diag
        except Exception as e:
            diag.append(f"Step 2: ✗ FAILED — {type(e).__name__}: {e}")
            return None, diag

        # Step 3: fetch the message
        try:
            diag.append(f"Step 3: Fetching message id={parsed.message_id} via get_messages()...")
            messages = await self.client.get_messages(entity, ids=parsed.message_id)
        except Exception as e:
            diag.append(f"Step 3: ✗ FAILED — {type(e).__name__}: {e}")
            return None, diag

        if not messages:
            diag.append(f"Step 3: ✗ No message returned — message_id={parsed.message_id} may not exist in this chat.")
            return None, diag

        msg = messages[0] if isinstance(messages, list) else messages
        if not msg:
            diag.append("Step 3: ✗ Message object is empty.")
            return None, diag

        diag.append(f"Step 3: ✓ Message fetched (id={msg.id}, has_media={bool(getattr(msg, 'media', None))})")

        # Step 4: detect album
        album: list = []
        if getattr(msg, "grouped_id", None):
            try:
                diag.append(f"Step 4: Message is part of an album (grouped_id={msg.grouped_id}). Fetching siblings...")
                all_msgs = await self.client.get_messages(entity, limit=20)
                album = [m for m in all_msgs
                         if getattr(m, "grouped_id", None) == msg.grouped_id]
                album.sort(key=lambda m: m.id)
                diag.append(f"Step 4: ✓ Found {len(album)} sibling(s) in the album")
            except Exception as e:
                diag.append(f"Step 4: ✗ Album fetch failed (continuing with single message) — {type(e).__name__}: {e}")
                album = []
        else:
            diag.append("Step 4: Message is NOT part of an album (single message).")

        # Step 5: compute chat_id in Bot API format
        chat_real_id = msg.peer_id.channel_id if isinstance(msg.peer_id, tl.PeerChannel) else msg.chat_id
        bot_api_chat_id = -1_000_000_000_000 - chat_real_id
        diag.append(f"Step 5: ✓ Chat resolved to Bot API chat_id={bot_api_chat_id}")

        return {
            "chat_id": bot_api_chat_id,
            "message": msg,
            "album": album or None,
        }, diag


    async def test_link(self, parsed: ParsedLink) -> dict:
        """Diagnostic method — try to resolve and fetch the link, return
        a structured result with all the info. Used by /test_link command.

        Returns a dict with keys:
          parsed: dict — what was parsed from the URL
          steps: list[str] — step-by-step diagnostic log
          success: bool — whether fetch succeeded
          error: str | None — top-level error message if any
          has_media: bool — whether the message has downloadable media
          media_type: str | None — 'photo' | 'video' | 'document' | etc.
          chat_title: str | None — resolved chat title (if entity was resolved)
        """
        result = {
            "parsed": {
                "kind": parsed.kind,
                "chat_ref": parsed.chat_ref,
                "message_id": parsed.message_id,
            },
            "steps": [],
            "success": False,
            "error": None,
            "has_media": False,
            "media_type": None,
            "chat_title": None,
        }

        fetched, diag = await self.fetch_message(parsed)
        result["steps"] = diag

        if fetched is None:
            result["error"] = "Fetch failed — see steps above for the cause."
            return result

        msg = fetched["message"]
        result["success"] = True
        result["has_media"] = bool(getattr(msg, "media", None))

        # Determine media type
        from telethon.tl import types as tl_types
        if isinstance(msg.media, tl_types.MessageMediaPhoto):
            result["media_type"] = "photo"
        elif isinstance(msg.media, tl_types.MessageMediaDocument):
            doc = msg.media.document
            if doc and doc.mime_type:
                if doc.mime_type.startswith("video/"):
                    result["media_type"] = "video"
                elif doc.mime_type.startswith("audio/"):
                    result["media_type"] = "audio"
                else:
                    result["media_type"] = "document"
            else:
                result["media_type"] = "document"
        elif isinstance(msg.media, tl_types.MessageMediaWebPage):
            result["media_type"] = "web_page"
        else:
            result["media_type"] = type(msg.media).__name__ if msg.media else None

        # Try to get chat title (best effort)
        try:
            entity = await self.client.get_entity(int(fetched["chat_id"]))
            result["chat_title"] = getattr(entity, "title", None)
        except Exception:
            pass

        return result

    # ---------- direct send to destination (NEW — fast path) ----------

    async def send_to_destination(
        self,
        source_chat_id: int,
        source_message_ids: list[int],
        dest_chat_id: int,
        topic_id: int | None = None,
        progress_callback=None,
        custom_caption: str | None = None,
    ) -> tuple[bool, list[str]]:
        """Send message(s) from a source chat to the destination chat using
        the user account (Telethon). This is the NEW fast path that avoids
        downloading media to disk and re-uploading via Bot API.

        Strategy (per the user's research on hybrid bot patterns):
          1. Try `user_client.forward_messages(dest, source_msg_ids, source_chat)`
             — this is the TRUE Telegram forward, fastest, preserves original
             sender info, and works for albums natively.
          2. If forward_messages fails (because the source has noforwards
             restriction), fall back to `user_client.send_message(dest,
             file=msg.media, formatting_entities=msg.entities)` for each
             message — this re-uploads from Telegram's servers WITHOUT
             downloading to disk. For protected albums, items are sent
             individually (loses album grouping in destination, but works).
          3. NEW: if (2) also fails (fully-protected content), download to
             disk + send_file(file=path) — uploads as a brand-new file with
             no link to the protected source. We PRESERVE the original
             document attributes (DocumentAttributeVideo, duration, dims,
             supports_streaming) and mime_type so the file is sent as a
             PLAYABLE VIDEO (not a generic document).

        Args:
          progress_callback: optional callable(sent_bytes, total_bytes) called
            during downloads and uploads so the caller can show progress.
            Currently used by the third (download+upload) path only.

        Requirements:
          - The user account must be a member of BOTH source and destination
          - For topics, pass topic_id (the topic's top message ID)

        Returns:
          (success: bool, diagnostics_log: list[str])
        """
        diag: list[str] = []
        diag.append(f"send_to_destination: {len(source_message_ids)} message(s) "
                    f"from {source_chat_id} to {dest_chat_id}"
                    f"{f' topic {topic_id}' if topic_id else ''}")

        # Helper: compute the effective caption based on custom_caption policy
        # - custom_caption=None: use original caption (msg.message)
        # - custom_caption="" (empty string): strip ALL captions
        # - custom_caption="<text>": use this string for the first item only
        #   (albums only show the first item's caption in Telegram)
        def _caption_for(msg, item_index: int = 0) -> str:
            """Return the caption to send for this message."""
            if custom_caption is None:
                # Use original caption (legacy behavior)
                return msg.message or ""
            if custom_caption == "":
                # Strip captions entirely
                return ""
            # Use custom caption, but only on the first item of an album
            if item_index == 0:
                return custom_caption
            return ""

        def _formatting_entities_for(msg, item_index: int = 0):
            """Return formatting entities to preserve. When using a custom
            caption, we strip entities (they wouldn't match the new text)."""
            if custom_caption is None:
                return msg.entities if item_index == 0 else None
            # Strip formatting entities when using custom caption
            return None

        try:
            source_entity = await self.client.get_entity(source_chat_id)
            dest_entity = await self.client.get_entity(dest_chat_id)
            diag.append(f"✓ Resolved source ({type(source_entity).__name__}) and "
                        f"destination ({type(dest_entity).__name__})")
        except Exception as e:
            diag.append(f"✗ Failed to resolve entities: {type(e).__name__}: {e}")
            return False, diag

        # Fetch the messages first so we have them ready (also confirms access)
        try:
            messages = await self.client.get_messages(source_entity, ids=source_message_ids)
            # Telethon returns None for missing messages; filter those out
            if isinstance(messages, list):
                messages = [m for m in messages if m is not None]
            else:
                messages = [messages] if messages else []
            if not messages:
                diag.append(f"✗ No messages found with IDs {source_message_ids}")
                return False, diag
            diag.append(f"✓ Fetched {len(messages)} message(s) from source")
        except Exception as e:
            diag.append(f"✗ Failed to fetch messages: {type(e).__name__}: {e}")
            return False, diag

        # ---- Step 1: try true forward (works for non-protected content) ----
        # Skip this step if a custom_caption is set — forward_messages doesn't
        # let us override the caption, so we need to use the send_message path
        # (which DOES let us set the caption) for caption control.
        if custom_caption is None:
            try:
                # Build reply_to for topics if specified
                kwargs: dict = {}
                if topic_id:
                    from telethon.tl.types import MessageReplyHeader
                    kwargs["reply_to"] = MessageReplyHeader(
                        reply_to_top_id=topic_id,
                        reply_to_msg_id=topic_id,
                    )

                await self.client.forward_messages(
                    dest_entity,
                    [m.id for m in messages],
                    source_entity,
                )
                diag.append(f"✓ True forward succeeded — {len(messages)} message(s) "
                            f"forwarded to destination"
                            f"{f' (topic {topic_id})' if topic_id else ''}")
                return True, diag
            except Exception as forward_err:
                diag.append(f"⚠ True forward failed: {type(forward_err).__name__}: {forward_err}")
                diag.append("  → Falling back to copy-and-resend (for protected content)")
        else:
            diag.append("ℹ Skipping true forward (custom_caption is set — using send_message)")

        # ---- Step 2: fallback — re-upload via send_message(file=msg.media) ----
        # This bypasses the noforwards restriction by re-uploading from
        # Telegram's servers directly (no disk download).
        sent_count = 0
        last_error = None
        for i, msg in enumerate(messages):
            try:
                send_kwargs = dict(
                    message=_caption_for(msg, i),
                    file=msg.media,
                    formatting_entities=_formatting_entities_for(msg, i),
                    link_preview=False,
                )
                if topic_id:
                    from telethon.tl.types import MessageReplyHeader
                    send_kwargs["reply_to"] = MessageReplyHeader(
                        reply_to_top_id=topic_id,
                        reply_to_msg_id=topic_id,
                    )
                # For multi-item albums, only the first item gets a caption
                # (already handled by _caption_for which returns "" for i>0
                # when custom_caption is set; for None it returns "" for i>0
                # since the original captions of album items >0 don't make
                # sense to repeat — only the first item's caption matters in
                # a Telegram album).
                if i > 0 and custom_caption is None:
                    send_kwargs["message"] = ""

                await self.client.send_message(dest_entity, **send_kwargs)
                sent_count += 1
                diag.append(f"✓ Re-sent message {i+1}/{len(messages)} "
                            f"(type: {type(msg.media).__name__ if msg.media else 'text'})")
            except Exception as e:
                last_error = e
                diag.append(f"✗ Failed to send message {i+1}/{len(messages)} "
                            f"via send_message(file=msg.media): "
                            f"{type(e).__name__}: {e}")

        if sent_count == len(messages):
            diag.append(f"✓ All {sent_count} message(s) re-sent to destination")
            return True, diag
        elif sent_count > 0:
            diag.append(f"⚠ Partial success: {sent_count}/{len(messages)} sent")
            return True, diag

        # ---- Step 3: third fallback — download to disk + send_file ----
        # The "send_message(file=msg.media)" path fails with
        # ChatForwardsRestrictedError because Telethon detects that the file
        # object references an existing message and treats it as a forward.
        # The only reliable way to send protected content is to:
        #   1. Download the media bytes to disk (Telethon allows this — you
        #      have view access as a member)
        #   2. Upload as a brand new file via send_file(file=path) — no link
        #      to the protected source, Telegram can't tell it's a "forward"
        #   3. PRESERVE the original document attributes (DocumentAttributeVideo
        #      with duration, dimensions) and mime_type so the file is sent
        #      as a PLAYABLE VIDEO (not a generic document). Pass
        #      supports_streaming=True to send_file for videos.
        diag.append("⚠ Falling back to download-to-disk + send_file (third path)")
        diag.append("  → This is slower but works for fully protected content")

        import tempfile
        import shutil
        from telethon.tl import types as tl_types
        tmp_dir = tempfile.mkdtemp(prefix="forwarder_protected_")
        try:
            sent_count = 0
            for i, msg in enumerate(messages):
                if not msg.media:
                    # Text-only message — just send the text
                    try:
                        send_kwargs = dict(
                            message=_caption_for(msg, i),
                            link_preview=False,
                        )
                        if topic_id:
                            from telethon.tl.types import MessageReplyHeader
                            send_kwargs["reply_to"] = MessageReplyHeader(
                                reply_to_top_id=topic_id,
                                reply_to_msg_id=topic_id,
                            )
                        # For albums, only first item has caption (already
                        # handled by _caption_for returning "" for i>0 when
                        # custom_caption is set; for None, the original caption
                        # of subsequent items would be repetitive in an album,
                        # so we strip them too).
                        if i > 0 and custom_caption is None:
                            send_kwargs["message"] = ""
                        await self.client.send_message(dest_entity, **send_kwargs)
                        sent_count += 1
                        diag.append(f"✓ Sent text-only message {i+1}/{len(messages)}")
                    except Exception as e:
                        diag.append(f"✗ Failed to send text message {i+1}: {type(e).__name__}: {e}")
                    continue

                # ----- Build the progress callback for this iteration -----
                # IMPORTANT: Python closure bug — `i` and `phase` would all
                # resolve to the last loop iteration's value if we just used
                # them in a closure. We bind them as default args to make
                # each callback capture its own values.
                #
                # Also: Telethon's progress_callback can be either sync OR
                # async — Telethon awaits it via _maybe_await. So we can use
                # async def. But we keep it sync and call the outer
                # progress_callback (which is async) — _maybe_await will
                # await the returned coroutine.
                def make_progress_cb(item_index: int, total_items: int,
                                      phase: str, filename: str):
                    """Build a progress callback bound to the given iteration.
                    `phase` is 'Downloading' or 'Uploading'."""
                    def _cb(sent_bytes: int, total_bytes: int):
                        if progress_callback:
                            label = f"{phase} {filename} ({item_index+1}/{total_items})"
                            # progress_callback is async — _maybe_await will
                            # await the returned coroutine
                            return progress_callback(sent_bytes, total_bytes, label)
                    return _cb

                # ----- Helper: download thumbnail (if available) -----
                async def _download_thumbnail(msg_media, thumb_dir: str, idx: int):
                    """Download the thumbnail for a video/document as a JPEG.
                    Returns the path to the .jpg file, or None if no thumb.

                    Telegram documents have a `thumbs` list (PhotoSize
                    objects). The largest is typically a small JPEG used as
                    the video poster / preview. We download it via Telethon's
                    `download_media(msg, file=bytes, thumb=-1)` which uses
                    Telethon's _get_thumb to pick the largest size.
                    """
                    try:
                        thumb_bytes = await self.client.download_media(
                            msg_media, file=bytes, thumb=-1,
                        )
                        if not thumb_bytes:
                            return None
                        thumb_path = os.path.join(thumb_dir, f"thumb_{idx}.jpg")
                        with open(thumb_path, "wb") as f:
                            f.write(thumb_bytes)
                        return thumb_path
                    except Exception as e:
                        diag.append(f"  • (thumbnail download skipped: {type(e).__name__})")
                        return None

                # ----- Photos: MessageMediaPhoto -----
                if isinstance(msg.media, tl_types.MessageMediaPhoto):
                    out_path = os.path.join(tmp_dir, f"photo_{i+1}_{int(time.time())}.jpg")
                    dl_cb = make_progress_cb(i, len(messages), "Downloading", f"photo_{i+1}.jpg")
                    try:
                        result = await self.client.download_media(
                            msg, file=out_path, progress_callback=dl_cb,
                        )
                        if not result:
                            diag.append(f"✗ Could not download photo for message {i+1}")
                            continue
                        if isinstance(result, bytes):
                            with open(out_path, "wb") as f:
                                f.write(result)
                        else:
                            out_path = str(result)
                        sz = os.path.getsize(out_path)
                        diag.append(f"  • Downloaded photo_{i+1}.jpg ({sz/1024:.1f} KB)")
                    except Exception as e:
                        diag.append(f"✗ Failed to download photo {i+1}: {type(e).__name__}: {e}")
                        continue
                    # Send as photo — force_document=False lets Telethon treat
                    # the .jpg file as a photo (InputMediaUploadedPhoto)
                    try:
                        send_kwargs = dict(
                            file=out_path,
                            caption=_caption_for(msg, i),
                            formatting_entities=_formatting_entities_for(msg, i),
                            force_document=False,
                        )
                        if topic_id:
                            from telethon.tl.types import MessageReplyHeader
                            send_kwargs["reply_to"] = MessageReplyHeader(
                                reply_to_top_id=topic_id,
                                reply_to_msg_id=topic_id,
                            )
                        ul_cb = make_progress_cb(i, len(messages), "Uploading", f"photo_{i+1}.jpg")
                        await self.client.send_file(
                            dest_entity, progress_callback=ul_cb, **send_kwargs,
                        )
                        sent_count += 1
                        diag.append(f"✓ Sent re-uploaded photo {i+1}/{len(messages)}")
                    except Exception as e:
                        diag.append(f"✗ Failed to send photo {i+1}: {type(e).__name__}: {e}")
                        last_error = e
                    continue

                # ----- Documents (video, audio, animation, generic document) -----
                if isinstance(msg.media, tl_types.MessageMediaDocument):
                    doc = msg.media.document
                    if not doc:
                        diag.append(f"✗ No document in message {i+1}")
                        continue

                    # Extract original mime_type
                    original_mime = doc.mime_type or "application/octet-stream"

                    # Determine media type from mime + attributes
                    is_video = original_mime.startswith("video/")
                    is_audio = original_mime.startswith("audio/")
                    is_animation = any(isinstance(a, tl_types.DocumentAttributeAnimated)
                                       for a in (doc.attributes or []))
                    is_image_doc = original_mime.startswith("image/")

                    # Find original filename
                    original_filename = None
                    for attr in (doc.attributes or []):
                        if isinstance(attr, tl_types.DocumentAttributeFilename):
                            original_filename = attr.file_name
                            break
                    if not original_filename:
                        # Generate based on mime type
                        ext_map = {
                            "video/mp4": "mp4", "video/quicktime": "mov",
                            "video/x-matroska": "mkv",
                            "audio/mpeg": "mp3", "audio/ogg": "ogg",
                            "audio/x-wav": "wav",
                            "image/jpeg": "jpg", "image/png": "png",
                        }
                        ext = ext_map.get(original_mime, "bin")
                        original_filename = f"media_{i+1}.{ext}"

                    # Build the attributes list to pass to send_file.
                    #
                    # KEY INSIGHT: We must NOT pass DocumentAttributeFilename
                    # because Telethon's get_attributes() regenerates it from
                    # the local file path. If we pass both, Telethon may
                    # override the regenerated one with our (potentially
                    # inconsistent) one — and that's actually fine.
                    # But we MUST keep DocumentAttributeVideo (with duration,
                    # w, h, supports_streaming) and DocumentAttributeAudio
                    # so Telegram knows it's a video/audio.
                    #
                    # We filter out DocumentAttributeFilename to avoid the
                    # conflict; Telethon will use the local file's basename.
                    original_attributes = [
                        attr for attr in (doc.attributes or [])
                        if not isinstance(attr, tl_types.DocumentAttributeFilename)
                    ]

                    # Decide force_document — True for generic files (pdf, zip)
                    force_document = not (is_video or is_audio or
                                          is_animation or is_image_doc)

                    # Download to disk with LARGER CHUNK SIZE for speed.
                    # Telethon's auto-picker uses 128KB for <100MB files,
                    # which is 4x smaller than the 512KB max. Each chunk is a
                    # separate network round-trip, so 4x smaller = 4x slower.
                    # We bypass the auto-picker by calling _download_file with
                    # part_size_kb=512 (4x speedup).
                    out_path = os.path.join(tmp_dir, original_filename)
                    dl_cb = make_progress_cb(i, len(messages), "Downloading", original_filename)
                    try:
                        # Use Telethon's _download_file directly with a large
                        # chunk size for faster download. The InputDocumentFileLocation
                        # is what Telethon would use internally for documents.
                        from telethon.tl.types import InputDocumentFileLocation
                        thumb_size_type = ""  # main file, not a thumb
                        file_location = InputDocumentFileLocation(
                            id=doc.id,
                            access_hash=doc.access_hash,
                            file_reference=doc.file_reference,
                            thumb_size=thumb_size_type,
                        )
                        await self.client._download_file(
                            file_location,
                            out_path,
                            part_size_kb=512,  # 4x larger than auto-picked 128KB
                            file_size=doc.size,
                            progress_callback=dl_cb,
                        )
                        sz = os.path.getsize(out_path)
                        diag.append(f"  • Downloaded {original_filename} "
                                    f"({sz/1024/1024:.1f} MB, mime={original_mime})")
                    except Exception as e:
                        diag.append(f"✗ Failed to download media {i+1}: "
                                    f"{type(e).__name__}: {e}")
                        # Fallback: try the regular download_media
                        try:
                            diag.append(f"  → Retrying with regular download_media()...")
                            result = await self.client.download_media(
                                msg, file=out_path, progress_callback=dl_cb,
                            )
                            if not result:
                                continue
                            sz = os.path.getsize(out_path)
                            diag.append(f"  • Downloaded (fallback) {original_filename} "
                                        f"({sz/1024/1024:.1f} MB)")
                        except Exception as e2:
                            diag.append(f"✗ Fallback download also failed: {type(e2).__name__}: {e2}")
                            continue

                    # Download the thumbnail (for videos). The thumb is a small
                    # JPEG that Telegram shows as the video poster before play.
                    # Without it, the destination video has no preview/thumbnail.
                    thumb_path = None
                    if is_video and getattr(doc, "thumbs", None):
                        thumb_path = await _download_thumbnail(msg, tmp_dir, i)
                        if thumb_path:
                            try:
                                tsize = os.path.getsize(thumb_path)
                                diag.append(f"  • Downloaded thumbnail ({tsize/1024:.1f} KB)")
                            except OSError:
                                pass

                    # Send the file with preserved attributes + thumbnail +
                    # supports_streaming. We PRE-UPLOAD the file with a large
                    # chunk size (512KB max) for 4x speedup vs Telethon's
                    # auto-picker, then pass the resulting InputFile to send_file.
                    #
                    # For files >10MB, we use a PARALLEL uploader that splits
                    # the file into chunks and uploads them concurrently. This
                    # gives ~4x speedup for large files (the sequential
                    # bottleneck was each chunk waiting for the previous
                    # chunk's response before sending).
                    try:
                        ul_cb = make_progress_cb(i, len(messages), "Uploading", original_filename)
                        file_size_bytes = os.path.getsize(out_path)

                        if file_size_bytes > 10 * 1024 * 1024:
                            # Large file — use parallel upload (4 concurrent chunks)
                            diag.append(f"  • Using parallel upload (4 chunks) for {file_size_bytes/1024/1024:.1f} MB file")
                            file_handle = await self._parallel_upload_file(
                                out_path,
                                file_size_bytes,
                                part_size_kb=512,
                                parallel=4,
                                progress_callback=ul_cb,
                            )
                        else:
                            # Small file — sequential upload is fast enough
                            file_handle = await self.client.upload_file(
                                out_path,
                                part_size_kb=512,
                                file_size=file_size_bytes,
                                progress_callback=ul_cb,
                            )

                        send_kwargs = dict(
                            file=file_handle,  # already-uploaded InputFile — send_file skips re-upload
                            caption=_caption_for(msg, i),
                            formatting_entities=_formatting_entities_for(msg, i),
                            # Pass the original attributes (minus Filename)
                            # so Telegram sees the video duration/dims.
                            attributes=original_attributes,
                            mime_type=original_mime,
                            force_document=force_document,
                        )
                        # KEY FIX: Pass the downloaded thumbnail so the
                        # destination video shows a poster/preview image.
                        if thumb_path:
                            send_kwargs["thumb"] = thumb_path
                        # KEY FIX: Pass supports_streaming=True for videos
                        # so Telegram shows it as a streamable video.
                        if is_video:
                            send_kwargs["supports_streaming"] = True
                        # voice_note for voice messages
                        if is_audio:
                            for attr in (doc.attributes or []):
                                if isinstance(attr, tl_types.DocumentAttributeAudio) and getattr(attr, "voice", False):
                                    send_kwargs["voice_note"] = True
                                    break

                        if topic_id:
                            from telethon.tl.types import MessageReplyHeader
                            send_kwargs["reply_to"] = MessageReplyHeader(
                                reply_to_top_id=topic_id,
                                reply_to_msg_id=topic_id,
                            )
                        await self.client.send_file(dest_entity, **send_kwargs)
                        sent_count += 1
                        if is_video:
                            thumb_msg = " with thumbnail" if thumb_path else " (no thumbnail)"
                            diag.append(f"✓ Sent re-uploaded video {i+1}/{len(messages)} "
                                        f"(playable, streaming{thumb_msg}, "
                                        f"duration/dims preserved)")
                        elif is_audio:
                            diag.append(f"✓ Sent re-uploaded audio {i+1}/{len(messages)}")
                        elif is_animation:
                            diag.append(f"✓ Sent re-uploaded animation {i+1}/{len(messages)}")
                        else:
                            diag.append(f"✓ Sent re-uploaded document {i+1}/{len(messages)}")
                    except Exception as e:
                        diag.append(f"✗ Failed to send re-uploaded media {i+1}: "
                                    f"{type(e).__name__}: {e}")
                        last_error = e
        finally:
            # Cleanup tmp dir
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

        if sent_count == len(messages):
            diag.append(f"✓ All {sent_count} message(s) re-uploaded (third path)")
            return True, diag
        elif sent_count > 0:
            diag.append(f"⚠ Partial success (third path): {sent_count}/{len(messages)} sent")
            return True, diag
        else:
            diag.append(f"✗ All sends failed. Last error: {last_error}")
            return False, diag

    # ---------- parallel upload (for large files) ----------

    async def _parallel_upload_file(
        self,
        file_path: str,
        file_size: int,
        part_size_kb: int = 512,
        parallel: int = 4,
        progress_callback=None,
    ):
        """Upload a file in PARALLEL chunks for faster throughput.

        This replaces Telethon's sequential upload_file for large files.
        The sequential approach sends one 512KB chunk, waits for the
        response, then sends the next. For a 50MB file that's 100 chunks
        × ~200ms round-trip = ~20 seconds.

        This parallel approach splits the file into chunks and uploads
        `parallel` chunks concurrently. For 4x parallel, a 50MB file
        takes ~5 seconds instead of 20.

        Telegram's SaveBigFilePartRequest is stateless — each part is
        stored independently, associated by file_id. So parallel uploads
        work: we just need to use the same file_id for all parts and
        different file_part indices.

        Args:
          file_path: Path to the file to upload.
          file_size: Size of the file in bytes.
          part_size_kb: Chunk size in KB (max 512, Telethon's limit).
          parallel: Number of concurrent chunk uploads (default 4).
          progress_callback: Sync callable(sent, total) — called after
            each chunk is uploaded.

        Returns:
          InputFileBig (for files >10MB) or InputFile (for smaller) —
          ready to pass to send_file.
        """
        import asyncio as _asyncio
        import hashlib
        from telethon.tl.functions.upload import (
            SaveBigFilePartRequest, SaveFilePartRequest,
        )
        from telethon.tl.types import InputFile, InputFileBig
        from telethon import helpers

        part_size = part_size_kb * 1024
        part_count = (file_size + part_size - 1) // part_size
        file_id = helpers.generate_random_long()
        file_name = os.path.basename(file_path)

        is_big = file_size > 10 * 1024 * 1024
        logger.info("Parallel upload: %s (%d bytes, %d chunks, %d parallel)",
                    file_name, file_size, part_count, parallel)

        # Track progress across parallel tasks
        bytes_uploaded = [0]  # mutable container for closure
        progress_lock = _asyncio.Lock()

        async def upload_one_part(part_index: int):
            """Read and upload a single chunk of the file."""
            offset = part_index * part_size
            # Read this specific part from disk
            with open(file_path, "rb") as f:
                f.seek(offset)
                data = f.read(part_size)

            # Use the correct request type based on file size
            if is_big:
                request = SaveBigFilePartRequest(
                    file_id, part_index, part_count, data,
                )
            else:
                request = SaveFilePartRequest(
                    file_id, part_index, data,
                )

            result = await self.client(request)
            if not result:
                raise RuntimeError(
                    f"Telegram rejected part {part_index}/{part_count}"
                )

            # Update progress (thread-safe via lock)
            async with progress_lock:
                bytes_uploaded[0] += len(data)
                if progress_callback:
                    try:
                        r = progress_callback(bytes_uploaded[0], file_size)
                        if inspect.isawaitable(r):
                            await r
                    except Exception:
                        pass

        # Use a semaphore to limit concurrency
        sem = _asyncio.Semaphore(parallel)

        async def bounded_upload(part_index: int):
            async with sem:
                return await upload_one_part(part_index)

        # Launch all upload tasks
        tasks = [_asyncio.create_task(bounded_upload(i)) for i in range(part_count)]
        results = await _asyncio.gather(*tasks, return_exceptions=True)

        # Check for errors
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                # Cancel any remaining tasks (shouldn't be any since gather waits)
                raise RuntimeError(
                    f"Parallel upload failed at part {i}/{part_count}: "
                    f"{type(r).__name__}: {r}"
                )

        # Build the InputFile / InputFileBig to pass to send_file
        if is_big:
            return InputFileBig(id=file_id, parts=part_count, name=file_name)
        else:
            # For small files, compute MD5 (needed for InputFile dedup)
            md5 = hashlib.md5()
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    md5.update(chunk)
            return InputFile(
                id=file_id,
                parts=part_count,
                name=file_name,
                md5_digest=md5.digest(),
            )


    # ---------- legacy: download media to disk (fallback path) ----------

    async def download_message_media(
        self,
        source_chat_id: int,
        source_message_ids: list[int],
        tmp_dir: str,
        max_bytes: int = 50 * 1024 * 1024,
    ) -> tuple[list[dict], str | None, str | None, list[str]]:
        """Legacy fallback: download media from source chat to disk for
        re-upload via Bot API. Used when send_to_destination fails (e.g.,
        user account is not a member of the destination).

        Returns:
          (media_paths, caption, text_only, diagnostics_log)

        media_paths is a list of {'path': str, 'type': str}
        caption is the original caption
        text_only is set if no media was downloaded (text-only message)
        """
        diag: list[str] = []
        media_paths: list[dict] = []
        caption: str | None = None
        text_only: str | None = None

        try:
            source_entity = await self.client.get_entity(source_chat_id)
        except Exception as e:
            diag.append(f"✗ Failed to resolve source entity: {type(e).__name__}: {e}")
            return [], None, None, diag

        messages = await self.client.get_messages(source_entity, ids=source_message_ids)
        if isinstance(messages, list):
            messages = [m for m in messages if m is not None]
        else:
            messages = [messages] if messages else []

        if not messages:
            diag.append(f"✗ No messages found with IDs {source_message_ids}")
            return [], None, None, diag

        from telethon.tl import types as tl

        for idx, msg in enumerate(messages):
            if idx == 0:
                caption = msg.message

            if not msg.media:
                if msg.message and not media_paths:
                    text_only = msg.message
                continue

            # Skip non-downloadable media types
            if isinstance(msg.media, (tl.MessageMediaWebPage, tl.MessageMediaContact,
                                       tl.MessageMediaGeo, tl.MessageMediaVenue,
                                       tl.MessageMediaGame, tl.MessageMediaPoll,
                                       tl.MessageMediaUnsupported)):
                continue

            # Determine type
            media_type = "document"
            if isinstance(msg.media, tl.MessageMediaPhoto):
                media_type = "photo"
            elif isinstance(msg.media, tl.MessageMediaDocument):
                doc = msg.media.document
                if doc and doc.mime_type:
                    mt = doc.mime_type
                    if mt.startswith("video/"):
                        for attr in doc.attributes:
                            if isinstance(attr, tl.DocumentAttributeAnimated):
                                media_type = "animation"
                                break
                            if isinstance(attr, tl.DocumentAttributeVideo):
                                media_type = "video_note" if getattr(attr, "round_message", False) else "video"
                                break
                    elif mt.startswith("audio/"):
                        media_type = "voice" if "ogg" in mt else "audio"

            # Check size before download
            try:
                if isinstance(msg.media, tl.MessageMediaDocument) and msg.media.document:
                    sz = msg.media.document.size or 0
                    if sz > max_bytes:
                        diag.append(f"⚠ Skipping item {idx}: file too large "
                                    f"({sz/1024/1024:.1f} MB > {max_bytes/1024/1024:.0f} MB)")
                        continue
            except Exception:
                pass

            # Download
            out_path = os.path.join(tmp_dir, f"media_{idx}_{int(time.time())}")
            try:
                result = await self.client.download_media(msg, file=out_path)
                if not result:
                    continue
                if isinstance(result, bytes):
                    with open(out_path, "wb") as f:
                        f.write(result)
                else:
                    out_path = str(result)
                sz = os.path.getsize(out_path)
                if sz > max_bytes:
                    diag.append(f"⚠ Skipping item {idx}: downloaded {sz/1024/1024:.1f} MB "
                                f"(> {max_bytes/1024/1024:.0f} MB Bot API limit)")
                    os.remove(out_path)
                    continue
                media_paths.append({"path": out_path, "type": media_type})
                diag.append(f"✓ Downloaded item {idx} ({media_type}, {sz/1024:.1f} KB)")
            except Exception as e:
                diag.append(f"✗ Failed to download item {idx}: {type(e).__name__}: {e}")

        return media_paths, caption, text_only, diag

    # ---------- channel scraping ----------

    async def scrape_channel(
        self,
        source_chat_ref,
        dest_chat_id,
        topic_id: int | None = None,
        reverse: bool = False,
        min_id: int = 0,
        max_id: int = 0,
        cancel_event=None,
        progress_callback=None,
        status_callback=None,
        stats_callback=None,
        media_types: list[str] | None = None,
        parallel: int = 3,
        custom_caption: str | None = None,
    ) -> dict:
        """Iterate all messages in a channel and forward each media message
        to the destination chat. Used by the /scrape command.

        Args:
          source_chat_ref: chat_id (int) or username (str) of the source channel
          dest_chat_id: destination chat_id (int) or "me" for Saved Messages
          topic_id: optional topic thread (for forum destinations)
          reverse: if True, oldest first (default: newest first)
          min_id: skip messages with id <= min_id
          max_id: skip messages with id > max_id (or 0 for no upper bound)
          cancel_event: asyncio.Event — set to cancel scraping
          progress_callback: async callable(sent, total_seen, last_msg_id, label)
          status_callback: async callable(status_text) — for status updates
          media_types: list of types to include. None means all media.
            Valid values: 'photo', 'video', 'animation', 'document', 'audio',
            'voice'. If specified, only matching media is sent; others are
            skipped (counted in skipped_count).
          parallel: number of concurrent sends. Default 3 (safe for Telegram).
            Higher values risk FloodWait. Each task uses its own asyncio task.

        Returns:
          dict with keys: sent_count, failed_count, skipped_count,
                          total_seen, last_message_id, cancelled (bool),
                          flood_waits (int)

        Rate limiting:
          - Per-task 0.3 sec delay between sends (safe even with parallel=3)
          - On FloodWaitError, sleep for the requested seconds + 5s buffer
            and retry the message once

        Notes:
          - parallel sends are independent — they don't share rate-limit state.
            The actual bottleneck is Telegram's per-account rate limit, not
            our concurrency. Setting parallel=5+ risks FloodWait.
          - Each task holds its own network connection (no shared TCP overhead)
          - The order of sent messages in the destination may differ from the
            source order when parallel > 1 (out-of-order arrival)
        """
        from telethon.errors import FloodWaitError
        import asyncio as _asyncio

        # Normalize media_types: lowercase, validate, default to None (all)
        if media_types is not None:
            media_types = [t.lower() for t in media_types]
            valid = {"photo", "video", "animation", "document", "audio", "voice"}
            invalid = set(media_types) - valid
            if invalid:
                if status_callback:
                    await status_callback(f"❌ Invalid media types: {invalid}. "
                                          f"Valid: {valid}")
                return {
                    "sent_count": 0, "failed_count": -1, "skipped_count": 0,
                    "total_seen": 0, "last_message_id": 0,
                    "cancelled": False, "flood_waits": 0,
                }

        result = {
            "sent_count": 0,
            "failed_count": 0,
            "skipped_count": 0,  # text-only or no-media messages
            "total_seen": 0,
            "last_message_id": 0,
            "cancelled": False,
            "flood_waits": 0,
        }

        try:
            source_entity = await self.client.get_entity(source_chat_ref)
            dest_entity = await self.client.get_entity(dest_chat_id)
        except Exception as e:
            if status_callback:
                await status_callback(f"❌ Failed to resolve entities: {type(e).__name__}: {e}")
            result["failed_count"] = -1  # signal error
            return result

        if status_callback:
            await status_callback(
                f"🔍 Scraping channel: {getattr(source_entity, 'title', source_chat_ref)}\n"
                f"   → destination: {getattr(dest_entity, 'title', dest_chat_id)}"
                f"{f' (topic {topic_id})' if topic_id else ''}\n"
                f"   order: {'oldest first' if reverse else 'newest first'}"
            )

        # Build kwargs for iter_messages
        iter_kwargs = {
            "reverse": reverse,
            "limit": None,  # iterate ALL messages (use async iterator)
        }
        if min_id > 0:
            iter_kwargs["min_id"] = min_id
        if max_id > 0:
            iter_kwargs["max_id"] = max_id

        # Statistics for status updates
        last_status_time = 0
        status_interval = 5.0  # update status every 5 seconds

        # Helper: classify a message's media type
        def _classify_media(msg) -> str | None:
            """Return 'photo', 'video', 'animation', 'document', 'audio',
            'voice', or None (for non-media messages)."""
            if not msg.media:
                return None
            if isinstance(msg.media, tl.MessageMediaPhoto):
                return "photo"
            if isinstance(msg.media, tl.MessageMediaDocument):
                doc = msg.media.document
                if not doc or not doc.mime_type:
                    return "document"
                mt = doc.mime_type
                if mt.startswith("video/"):
                    for attr in (doc.attributes or []):
                        if isinstance(attr, tl.DocumentAttributeAnimated):
                            return "animation"
                        if isinstance(attr, tl.DocumentAttributeVideo):
                            # round_message=True means round video (video note)
                            return "video_note" if getattr(attr, "round_message", False) else "video"
                    return "video"
                if mt.startswith("audio/"):
                    for attr in (doc.attributes or []):
                        if isinstance(attr, tl.DocumentAttributeAudio) and getattr(attr, "voice", False):
                            return "voice"
                    return "audio" if "ogg" not in mt else "voice"
                return "document"
            # Non-downloadable types: web pages, contacts, geos, polls, etc.
            return None

        # Helper: send one message with FloodWait handling
        async def _send_one(msg_id: int):
            """Send a single message via send_to_destination, with FloodWait
            retry. Updates `result` in place. Returns True if sent."""
            src_ref = source_chat_ref if isinstance(source_chat_ref, int) else source_entity.id
            try:
                success, _ = await self.send_to_destination(
                    source_chat_id=src_ref,
                    source_message_ids=[msg_id],
                    dest_chat_id=dest_chat_id,
                    topic_id=topic_id,
                    progress_callback=None,
                    custom_caption=custom_caption,
                )
                if success:
                    result["sent_count"] += 1
                    return True
                result["failed_count"] += 1
                return False
            except FloodWaitError as e:
                result["flood_waits"] += 1
                wait_seconds = e.seconds + 5
                if status_callback:
                    await status_callback(
                        f"⏳ Flood wait: sleeping {wait_seconds}s before retrying...\n"
                        f"   Sent so far: {result['sent_count']}"
                    )
                await _asyncio.sleep(wait_seconds)
                # Retry once
                try:
                    success, _ = await self.send_to_destination(
                        source_chat_id=src_ref,
                        source_message_ids=[msg_id],
                        dest_chat_id=dest_chat_id,
                        topic_id=topic_id,
                        progress_callback=None,
                        custom_caption=custom_caption,
                    )
                    if success:
                        result["sent_count"] += 1
                        return True
                except Exception:
                    pass
                result["failed_count"] += 1
                return False
            except Exception as e:
                logger.warning("scrape: failed to send msg %d: %s", msg_id, e)
                result["failed_count"] += 1
                return False

        # Semaphore for parallel sends (limits concurrent in-flight sends)
        # to avoid hitting Telegram's rate limit. Default 3 = ~9 msgs/sec total.
        sem = _asyncio.Semaphore(parallel)
        pending_send_tasks: list = []  # asyncio tasks for in-flight sends

        async def _send_with_semaphore(msg_id: int):
            """Wrap _send_one with the semaphore. Awaits the send."""
            async with sem:
                # Check cancellation before sending
                if cancel_event and cancel_event.is_set():
                    return False
                await _send_one(msg_id)
                # Small delay between sends within a single task slot
                await _asyncio.sleep(0.3)

        try:
            async for msg in self.client.iter_messages(source_entity, **iter_kwargs):
                # Check for cancellation
                if cancel_event and cancel_event.is_set():
                    result["cancelled"] = True
                    # Wait for in-flight sends to complete (don't abandon them)
                    if pending_send_tasks:
                        if status_callback:
                            await status_callback(
                                f"🛑 Cancel signal received. Waiting for "
                                f"{len(pending_send_tasks)} in-flight send(s) to complete..."
                            )
                        await _asyncio.gather(*pending_send_tasks, return_exceptions=True)
                    if status_callback:
                        await status_callback(
                            f"🛑 Scraping cancelled by user.\n"
                            f"   Sent: {result['sent_count']}, "
                            f"Failed: {result['failed_count']}, "
                            f"Skipped: {result['skipped_count']}"
                        )
                    break

                result["total_seen"] += 1
                result["last_message_id"] = msg.id

                # Classify the message's media type
                m_type = _classify_media(msg)

                # Skip if no media
                if m_type is None:
                    result["skipped_count"] += 1
                    continue

                # Apply user's media type filter
                if media_types is not None:
                    # Normalize: 'voice' and 'video_note' both count as their
                    # respective primary types. 'video_note' is treated as 'video'.
                    type_check = m_type
                    if type_check == "video_note":
                        type_check = "video"
                    if type_check not in media_types:
                        result["skipped_count"] += 1
                        continue

                # Schedule the send in parallel (up to `parallel` at once)
                task = _asyncio.create_task(_send_with_semaphore(msg.id))
                pending_send_tasks.append(task)

                # Periodically drain completed tasks to avoid the list growing
                # unboundedly for huge channels.
                if len(pending_send_tasks) >= parallel * 4:
                    # Wait for at least one to complete before continuing.
                    # NOTE: asyncio.wait returns (done, pending) as SETS, not
                    # lists. We must convert back to a list to keep using
                    # .append() later. (Bug: was assigning the set directly,
                    # causing "AttributeError: 'set' object has no attribute
                    # 'append'" on the next iteration.)
                    done, pending = await _asyncio.wait(
                        pending_send_tasks, return_when=_asyncio.FIRST_COMPLETED,
                    )
                    # Drain all done tasks (their results were already applied
                    # via _send_one updating `result`)
                    for t in done:
                        try:
                            t.result()  # raise exceptions if any
                        except Exception:
                            pass
                    # Convert pending set back to list for .append() compatibility
                    pending_send_tasks = list(pending)

                # Progress callback
                if progress_callback:
                    try:
                        await progress_callback(
                            result["sent_count"],
                            result["total_seen"],
                            result["last_message_id"],
                            f"Sent {result['sent_count']} / seen {result['total_seen']}",
                        )
                    except Exception:
                        pass

                # Periodic status update
                import time as _time
                now = _time.time()
                if status_callback and now - last_status_time > status_interval:
                    last_status_time = now
                    await status_callback(
                        f"📊 Scraping in progress...\n\n"
                        f"Total seen: {result['total_seen']}\n"
                        f"Sent: {result['sent_count']}\n"
                        f"Failed: {result['failed_count']}\n"
                        f"Skipped (filtered/no media): {result['skipped_count']}\n"
                        f"Last msg ID: {result['last_message_id']}\n"
                        f"Parallel sends: {parallel}"
                    )

                # Send stats to stats_callback (for bot_data updates so
                # /scrape_status shows current progress, not stale data)
                if stats_callback:
                    try:
                        await stats_callback(result)
                    except Exception:
                        pass

        except Exception as e:
            logger.exception("scrape_channel: iter_messages failed")
            if status_callback:
                await status_callback(f"❌ Scrape error: {type(e).__name__}: {e}")
            result["failed_count"] = -1
            return result

        # Wait for any remaining in-flight send tasks to complete
        if pending_send_tasks:
            try:
                await _asyncio.gather(*pending_send_tasks, return_exceptions=True)
            except Exception:
                pass

        # Final status
        if status_callback and not result["cancelled"]:
            await status_callback(
                f"✅ Scraping complete!\n\n"
                f"Total seen: {result['total_seen']}\n"
                f"Sent: {result['sent_count']}\n"
                f"Failed: {result['failed_count']}\n"
                f"Skipped (filtered/no media): {result['skipped_count']}\n"
                f"Flood waits: {result['flood_waits']}\n"
                f"Last msg ID: {result['last_message_id']}\n"
                f"Parallel sends: {parallel}"
            )

        return result


__all__ = [
    "UserSession",
    "ParsedLink",
    "parse_telegram_link",
    "parse_channel_link",
]
