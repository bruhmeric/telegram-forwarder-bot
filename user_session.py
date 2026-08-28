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

    async def fetch_message(self, parsed: ParsedLink) -> Optional[dict]:
        """Fetch a single message (and possibly its album siblings) from a
        private / public channel the user account is a member of.

        Returns a dict with keys:
          chat_id: int (negative for channels)
          message: telethon Message object (single)
          album: list[Message] | None  (album siblings if any)
        """
        try:
            if parsed.kind == "private":
                chat_id = int(parsed.chat_ref)
                entity = await self.client.get_input_entity(chat_id)
            else:
                entity = await self.client.get_input_entity(parsed.chat_ref)
        except ChannelPrivateError:
            logger.error("You are not a member of chat %s", parsed.chat_ref)
            return None
        except Exception:
            logger.exception("Could not resolve entity for %s", parsed.chat_ref)
            return None

        try:
            messages = await self.client.get_messages(
                entity, ids=parsed.message_id
            )
        except Exception:
            logger.exception("get_messages failed")
            return None

        if not messages:
            return None
        msg = messages[0] if isinstance(messages, list) else messages
        if not msg:
            return None

        # Detect album — group_id is set on messages that are part of an album
        album: list = []
        if getattr(msg, "grouped_id", None):
            try:
                # fetch all messages around this one to get album siblings
                all_msgs = await self.client.get_messages(
                    entity,
                    limit=20,
                    # We need to find siblings; iterate a small window
                )
                album = [m for m in all_msgs
                         if getattr(m, "grouped_id", None) == msg.grouped_id]
                album.sort(key=lambda m: m.id)
            except Exception:
                logger.exception("album fetch failed")
                album = []

        chat_real_id = msg.peer_id.channel_id if isinstance(msg.peer_id, tl.PeerChannel) else msg.chat_id
        # Bot API convention: -100 concatenated with the raw channel_id
        return {
            "chat_id": -1_000_000_000_000 - chat_real_id,
            "message": msg,
            "album": album or None,
        }


__all__ = [
    "UserSession",
    "ParsedLink",
    "parse_telegram_link",
]
