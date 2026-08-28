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

        # ---- Step 2: fallback — re-upload via send_message(file=msg.media) ----
        # This bypasses the noforwards restriction by re-uploading from
        # Telegram's servers directly (no disk download).
        sent_count = 0
        last_error = None
        for i, msg in enumerate(messages):
            try:
                send_kwargs = dict(
                    message=msg.message or "",
                    file=msg.media,
                    formatting_entities=msg.entities,
                    link_preview=False,
                )
                if topic_id:
                    from telethon.tl.types import MessageReplyHeader
                    send_kwargs["reply_to"] = MessageReplyHeader(
                        reply_to_top_id=topic_id,
                        reply_to_msg_id=topic_id,
                    )
                # Only send the caption on the first message of an album
                if i > 0:
                    send_kwargs["message"] = ""

                await self.client.send_message(dest_entity, **send_kwargs)
                sent_count += 1
                diag.append(f"✓ Re-sent message {i+1}/{len(messages)} "
                            f"(type: {type(msg.media).__name__ if msg.media else 'text'})")
            except Exception as e:
                last_error = e
                diag.append(f"✗ Failed to send message {i+1}/{len(messages)}: "
                            f"{type(e).__name__}: {e}")

        if sent_count == len(messages):
            diag.append(f"✓ All {sent_count} message(s) re-sent to destination")
            return True, diag
        elif sent_count > 0:
            diag.append(f"⚠ Partial success: {sent_count}/{len(messages)} sent")
            return True, diag
        else:
            diag.append(f"✗ All sends failed. Last error: {last_error}")
            return False, diag


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
                                media_type = "video_note" if getattr(attr, "round", False) else "video"
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


__all__ = [
    "UserSession",
    "ParsedLink",
    "parse_telegram_link",
]
