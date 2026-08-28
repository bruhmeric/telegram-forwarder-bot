"""Configuration loader — reads .env and exposes typed settings.

Supports two run modes:
  * MODE=polling  (default, for local dev) — bot uses long-polling
  * MODE=webhook  (recommended for Render) — bot serves a webhook on $PORT

For Render:
  * Render injects PORT automatically — the bot listens on 0.0.0.0:$PORT
  * The Telethon session must be a StringSession stored in SESSION_STRING,
    because Render's free tier filesystem is ephemeral (the .session file
    would be lost on every restart)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # dotenv is optional; environment may already be set
    pass


def _int_list(value: str) -> list[int]:
    return [int(x.strip()) for x in value.split(",") if x.strip()]


@dataclass
class Config:
    bot_token: str
    api_id: Optional[int]
    api_hash: Optional[str]
    phone: Optional[str]
    destination_group_id: Optional[int]
    admin_ids: list[int] = field(default_factory=list)
    session_name: str = "user_session"
    db_path: str = "forwarder.db"
    # --- deployment mode ---
    mode: str = "polling"  # "polling" | "webhook"
    webhook_url: Optional[str] = None
    port: int = 8080
    session_string: Optional[str] = None

    @classmethod
    def load(cls) -> "Config":
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN missing — copy .env.example to .env and fill it in.")

        api_id_raw = os.environ.get("API_ID", "").strip()
        api_hash = os.environ.get("API_HASH", "").strip()
        phone = os.environ.get("PHONE", "").strip() or None

        dest_raw = os.environ.get("DESTINATION_GROUP_ID", "").strip()
        dest = int(dest_raw) if dest_raw else None

        admin_raw = os.environ.get("ADMIN_IDS", "").strip()
        admins = _int_list(admin_raw) if admin_raw else []

        mode_raw = os.environ.get("MODE", "polling").strip().lower()
        mode = mode_raw if mode_raw in ("polling", "webhook") else "polling"

        webhook_url = os.environ.get("WEBHOOK_URL", "").strip() or None

        port_raw = os.environ.get("PORT", "").strip()
        port = int(port_raw) if port_raw else 8080

        session_string = os.environ.get("SESSION_STRING", "").strip() or None

        return cls(
            bot_token=token,
            api_id=int(api_id_raw) if api_id_raw else None,
            api_hash=api_hash or None,
            phone=phone,
            destination_group_id=dest,
            admin_ids=admins,
            session_name=os.environ.get("SESSION_NAME", "user_session") or "user_session",
            db_path=os.environ.get("DB_PATH", "forwarder.db") or "forwarder.db",
            mode=mode,
            webhook_url=webhook_url,
            port=port,
            session_string=session_string,
        )

    @property
    def has_user_session(self) -> bool:
        # Session works if we have api_id+api_hash AND at least one of
        # (file-based session name, session_string).
        return bool(self.api_id and self.api_hash)

    def is_admin(self, user_id: int) -> bool:
        if not self.admin_ids:
            # No whitelist configured -> allow anyone (single-user self-hosted bot)
            return True
        return user_id in self.admin_ids

    @property
    def webhook_url_path(self) -> str:
        """Path component of the webhook URL — uses the secret part of the
        bot token so the webhook endpoint is not easily guessable."""
        return self.bot_token.split(":")[-1]
