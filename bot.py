"""Main entry point for the Telegram Forwarder Bot.

Wires together:
  * config.py     — env-based settings
  * db.py         — SQLite persistence
  * user_session.py — Telethon user session (for locked channels & topic
                     enumeration)
  * topics.py     — forum topic discovery + inline keyboard
  * handlers/    — direct / link / admin handlers

Run modes:
  * MODE=polling  — local dev (uses PTB's run_polling)
  * MODE=webhook  — production / Render / fps.ms (uses our custom aiohttp
                    server that ALSO serves GET / with 200 OK so platform
                    health checks pass — PTB's built-in tornado server
                    returns 404 for /, which fails Render's deploy)
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

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


# ---------------------------------------------------------------------------
# Application lifecycle hooks (called by PTB in both polling and webhook mode)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Application builder
# ---------------------------------------------------------------------------

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

    # IMPORTANT: both MessageHandlers MUST be in the same group with mutually
    # exclusive filters. In PTB v21, handlers in DIFFERENT groups all run,
    # which caused this bug:
    #   - User sends a t.me/c/... link
    #   - handle_link (group 1) runs -> fetches media via Telethon, shows picker
    #   - handle_direct_message (group 2) ALSO runs -> treats the link as
    #     plain text, shows a SECOND picker that just copy_message's the link
    #   - User taps the wrong picker -> bot forwards the link as text, not media
    #
    # By putting both handlers in the same group (0, the default), PTB stops
    # after the first match — only one of them runs per update.
    app.add_handler(
        MessageHandler(LINK_FILTER & ~filters.COMMAND, handle_link),
        group=0,
    )
    app.add_handler(
        MessageHandler(~LINK_FILTER & ~filters.COMMAND, handle_direct_message),
        group=0,
    )
    # Topic picker callback
    app.add_handler(CallbackQueryHandler(topic_callback, pattern=r"^(fwd|cancel):"))
    return app


# ---------------------------------------------------------------------------
# Custom aiohttp webhook server (Render / fps.ms / any cloud)
# ---------------------------------------------------------------------------

async def _run_webhook_custom(app: Application, cfg: Config) -> None:
    """Run the bot in webhook mode using a custom aiohttp server.

    Routes:
      * GET  /                      -> 200 OK (for Render / fps.ms health check)
      * GET  /<bot-token-secret>    -> 200 OK (also passes health check if you
                                            point it at the secret path)
      * POST /<bot-token-secret>    -> forwards to PTB's process_update
      * *                           -> 404

    We use aiohttp instead of PTB's built-in tornado server because PTB's
    server returns 404 for /, which causes Render's deploy health check to
    fail with "Timed out waiting for successful response code".
    """
    from aiohttp import web

    if not cfg.webhook_url:
        raise RuntimeError(
            "MODE=webhook requires WEBHOOK_URL to be set. "
            "Set it to your service URL, e.g. "
            "https://your-service-name.onrender.com"
        )

    url_path = cfg.webhook_url_path
    full_webhook_url = f"{cfg.webhook_url.rstrip('/')}/{url_path}"

    # Manually initialize the app (handlers, post_init, etc.)
    await app.initialize()
    await app.start()

    # Register the webhook URL with Telegram (replaces PTB's run_webhook call)
    await app.bot.set_webhook(
        url=full_webhook_url,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,  # keep pending updates — they'll arrive
    )
    logger.info("Webhook registered with Telegram: %s", full_webhook_url)

    # ----- aiohttp handlers -----

    async def health_handler(request: web.Request) -> web.Response:
        """Return 200 OK — used by Render / fps.ms / UptimeRobot health checks."""
        return web.Response(text="OK", status=200, headers={"Content-Type": "text/plain"})

    async def webhook_handler(request: web.Request) -> web.Response:
        """Receive an update from Telegram, dispatch to PTB's process_update."""
        if request.path != f"/{url_path}":
            return web.Response(status=404)
        try:
            data = await request.json()
        except Exception:
            logger.warning("webhook_handler: invalid JSON body")
            return web.Response(status=400, text="Bad request")
        try:
            update = Update.de_json(data, app.bot)
            if update is None:
                return web.Response(status=400, text="Could not parse update")
            # Schedule the update in the background — return 200 immediately
            # so Telegram doesn't think the bot is slow.
            asyncio.create_task(app.process_update(update))
        except Exception:
            logger.exception("webhook_handler: failed to dispatch update")
            # Telegram expects 200 even on error, otherwise it retries forever
            return web.Response(status=200, text="OK (handler error)")
        return web.Response(status=200, text="OK")

    # ----- build the web app -----

    web_app = web.Application(client_max_size=10 * 1024 * 1024)  # 10 MB
    web_app.router.add_get("/", health_handler)
    # aiohttp automatically registers HEAD alongside GET, so we don't need
    # an explicit add_head call.
    web_app.router.add_get(f"/{url_path}", health_handler)  # safety: GET passes health check too
    web_app.router.add_post(f"/{url_path}", webhook_handler)

    runner = web.AppRunner(web_app, access_log=None)  # access_log=None to keep logs clean
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg.port)
    await site.start()

    logger.info("Custom webhook server listening on 0.0.0.0:%d", cfg.port)
    logger.info("Health check endpoint: GET /  -> 200 OK")
    logger.info("Webhook endpoint:      POST /%s  -> forwards to PTB", url_path)

    # ----- keep the process alive forever -----

    try:
        await asyncio.Event().wait()  # never resolves unless Ctrl+C / signal
    finally:
        logger.info("Shutting down webhook server...")
        await site.stop()
        await runner.cleanup()
        # Unregister webhook with Telegram so messages don't queue forever
        try:
            await app.bot.delete_webhook()
        except Exception:
            pass
        await app.stop()
        await app.shutdown()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = Config.load()
    db = Database(cfg.db_path)

    app = build_application(cfg, db)
    logger.info("Starting bot in %s mode...", cfg.mode)

    if cfg.mode == "webhook":
        # Use our custom aiohttp server (Render / fps.ms / etc.)
        # We use asyncio.run because we need finer control over the lifecycle
        # than PTB's run_webhook provides.
        try:
            asyncio.run(_run_webhook_custom(app, cfg))
        except KeyboardInterrupt:
            logger.info("Interrupted")
        except Exception:
            logger.exception("Webhook mode crashed")
            sys.exit(1)
    else:
        # Polling mode (local dev) — use PTB's built-in run_polling
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
