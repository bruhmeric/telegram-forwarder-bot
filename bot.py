"""Main entry point for the Telegram Forwarder Bot.

Wires together:
  * config.py     — env-based settings
  * db.py         — SQLite persistence
  * user_session.py — Telethon user session (for locked channels & topic
                     enumeration)
  * topics.py     — forum topic discovery + inline keyboard
  * handlers/    — direct / link / admin handlers
"""
from __future__ import annotations

import asyncio
import logging
import sys

from telegram import Update
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler, ContextTypes, filters,
)

from config import Config
from db import Database
from user_session import UserSession
from topics import TopicManager
from handlers.admin import register_admin_handlers
from handlers.direct import handle_direct_message, topic_callback
from handlers.link import handle_link, LINK_FILTER

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
# Telegram lib is verbose; quiet it down
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)
logger = logging.getLogger("forwarder")


async def post_init(app: Application) -> None:
    """Called after the Application is fully initialised, before polling starts."""
    db: Database = app.bot_data["db"]
    await db.init()

    cfg: Config = app.bot_data["config"]
    user_session: UserSession | None = None
    if cfg.has_user_session:
        user_session = UserSession(
            cfg.session_name, cfg.api_id, cfg.api_hash,
            session_string=cfg.session_string,
        )
        ok = await user_session.start()
        if ok:
            app.bot_data["user_session"] = user_session
        else:
            app.bot_data["user_session"] = None
            logger.warning(
                "Telethon session unavailable. Locked-channel forwarding and "
                "topic auto-discovery will not work until you log in."
            )
    else:
        app.bot_data["user_session"] = None
        logger.info("Telethon not configured (API_ID/API_HASH missing). "
                    "Only direct forwarding will work.")

    app.bot_data["topics"] = TopicManager(db, app.bot_data["user_session"])

    me = await app.bot.get_me()
    logger.info("Bot started: @%s (%d) mode=%s", me.username, me.id, cfg.mode)
    if cfg.destination_group_id:
        logger.info("Destination group: %d", cfg.destination_group_id)
    else:
        logger.info("Destination group not set yet — use /setgroup <group_id>")
    if cfg.mode == "webhook":
        if not cfg.webhook_url:
            logger.error("MODE=webhook but WEBHOOK_URL is not set! The bot will "
                        "start but Telegram cannot reach it.")
        else:
            logger.info("Webhook URL: %s/%s", cfg.webhook_url.rstrip('/'),
                        cfg.webhook_url_path)


async def post_stop(app: Application) -> None:
    db: Database = app.bot_data.get("db")
    if db:
        await db.close()
    us: UserSession | None = app.bot_data.get("user_session")
    if us:
        await us.stop()


def build_application(cfg: Config, db: Database) -> Application:
    app = (
        Application.builder()
        .token(cfg.bot_token)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )
    app.bot_data["config"] = cfg
    app.bot_data["db"] = db

    register_admin_handlers(app)

    # Direct forward — all non-command, non-link messages
    # Note: we register the link handler FIRST with a high priority group so
    # that link messages don't get caught by the direct handler. We use
    # filters to differentiate.
    app.add_handler(
        MessageHandler(LINK_FILTER & (~filters.COMMAND), handle_link),
        group=1,
    )
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_direct_message),
        group=2,
    )
    # Topic picker callback
    app.add_handler(CallbackQueryHandler(topic_callback, pattern=r"^(fwd|cancel):"))
    return app


def main() -> None:
    cfg = Config.load()
    db = Database(cfg.db_path)

    app = build_application(cfg, db)
    logger.info("Starting bot in %s mode...", cfg.mode)

    if cfg.mode == "webhook":
        if not cfg.webhook_url:
            raise RuntimeError(
                "MODE=webhook requires WEBHOOK_URL to be set. "
                "Set it to your Render service URL, e.g. "
                "https://your-service-name.onrender.com"
            )
        url_path = cfg.webhook_url_path
        full_webhook_url = f"{cfg.webhook_url.rstrip('/')}/{url_path}"
        logger.info("Webhook: listening on 0.0.0.0:%d, URL=%s", cfg.port, full_webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=cfg.port,
            url_path=url_path,
            webhook_url=full_webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Interrupted")
