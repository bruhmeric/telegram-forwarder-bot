"""Admin commands: /setgroup, /refresh, /addtopic, /help, /status."""
from __future__ import annotations

import logging
import re
import unicodedata

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Telegram clients (mobile, desktop) aggressively auto-correct hyphens to
# "nicer-looking" Unicode variants when the user types a leading minus sign.
# This breaks int() parsing silently from the user's perspective because the
# ValueError handler used to just say "group_id must be an integer" — and if
# the message delivery failed or Render was asleep, the user saw nothing.
#
# Map all common hyphen/dash variants back to ASCII '-' so int() works.
_HYPHEN_REPLACEMENTS = {
    "\u2010": "-",  # HYPHEN
    "\u2011": "-",  # NON-BREAKING HYPHEN
    "\u2012": "-",  # FIGURE DASH
    "\u2013": "-",  # EN DASH
    "\u2014": "-",  # EM DASH
    "\u2015": "-",  # HORIZONTAL BAR
    "\u2212": "-",  # MINUS SIGN (math)
    "\uFE63": "-",  # SMALL HYPHEN-MINUS
    "\uFF0D": "-",  # FULL-WIDTH HYPHEN-MINUS
}


def _normalize_int_str(s: str) -> str:
    """Normalize a string that should represent an integer — strips whitespace,
    replaces Unicode hyphen variants with ASCII '-', and removes any
    thousands separators (commas, spaces between digits)."""
    if not s:
        return s
    # Replace Unicode hyphens
    for bad, good in _HYPHEN_REPLACEMENTS.items():
        s = s.replace(bad, good)
    # Strip whitespace
    s = s.strip()
    # Remove thousand separators ONLY between digits (e.g. "-100 123 456" -> "-100123456")
    s = re.sub(r"(?<=\d)[\s,](?=\d)", "", s)
    return s


def _safe_int(value: str) -> int | None:
    """Try to parse `value` as an int, normalizing Unicode hyphens first.
    Returns None if parsing fails."""
    if value is None:
        return None
    try:
        return int(_normalize_int_str(value))
    except (ValueError, TypeError):
        return None


def _is_authorized(cfg, user_id: int) -> bool:
    """Check if user_id is in the admin whitelist. If the whitelist is empty,
    everyone is allowed (single-user self-hosted bot)."""
    return cfg.is_admin(user_id)


async def _deny_silent(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        reason: str) -> None:
    """Log a denied command attempt and (optionally) notify the user.

    For commands like /setgroup that the user EXPECTS to work, we want to
    give them feedback so they don't think the bot is broken. But for
    security, we don't want to leak "you are not authorized" to random
    users probing the bot — we just stay silent.

    Compromise: if ADMIN_IDS is set AND the user is NOT in it, log the
    attempt but don't reply. This is what the original code did.
    If ADMIN_IDS is empty (everyone allowed), this function is never called.
    """
    user = update.effective_user
    logger.warning("Unauthorized /%s attempt by user_id=%s username=%s: %s",
                    reason, user.id if user else '?',
                    getattr(user, 'username', None) if user else '?', reason)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Hi! I forward whatever you send me to a topic in your group.\n\n"
        "Setup:\n"
        "1. /setgroup <group_id>  — set the destination group\n"
        "2. /refresh              — discover forum topics (needs Telethon session)\n"
        "   or /addtopic <title> <topic_id>  — add a topic manually\n"
        "\nThen just send me anything — I'll show you a topic picker."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "*Commands*\n"
        "/setgroup <id>  — set destination group (e.g. -1001234567890)\n"
        "/refresh        — re-fetch forum topics via Telethon\n"
        "/topics         — list currently-known topics\n"
        "/addtopic <title> <id>  — add a topic manually\n"
        "/deltopic <id>  — remove a manually-added topic\n"
        "/status         — show bot status (Telethon, group, topics)\n"
        "/whoami         — show your Telegram user ID + admin status\n"
        "/cancel         — cancel the latest pending forward\n"
        "\n*Sending content*\n"
        "• Send me a photo / video / text / file -> I show topics -> tap one\n"
        "• Send me a t.me/c/<id>/<msg> link -> I fetch via your Telethon session\n"
        "  and let you pick a topic",
        parse_mode="Markdown",
    )


async def cmd_setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        # User is not in ADMIN_IDS whitelist — log and silently return.
        # This is the "nothing happens" symptom: bot received /setgroup but
        # ignored it because the user is not authorized.
        user = update.effective_user
        logger.warning("/setgroup DENIED — user_id=%s username=%s not in ADMIN_IDS=%s",
                       user.id if user else '?',
                       getattr(user, 'username', None) if user else '?',
                       cfg.admin_ids)
        return
    if not context.args or len(context.args) < 1:
        await update.effective_message.reply_text(
            "Usage: /setgroup <group_id>\n"
            "Example: /setgroup -1001234567890\n\n"
            "To find the group ID, add @RawDataBot to the group, read its "
            "reply, then remove it.\n\n"
            "Tip: if the bot says 'group_id must be an integer', your "
            "Telegram client may have auto-corrected the minus sign to a "
            "Unicode dash. Try copying the ID directly from @RawDataBot's "
            "message (long-press -> Copy)."
        )
        return

    raw_arg = context.args[0]
    gid = _safe_int(raw_arg)
    if gid is None:
        # Show the user EXACTLY what we received — this is the #1 diagnostic
        # for the "nothing happens" bug. Telegram auto-corrects hyphens.
        await update.effective_message.reply_text(
            f"Could not parse '{raw_arg}' as an integer.\n\n"
            f"Most likely cause: Telegram auto-corrected your '-' to a "
            f"Unicode dash character. Try this instead:\n"
            f"  1. Long-press the group ID from @RawDataBot's message\n"
            f"  2. Paste it directly (don't retype the '-')\n\n"
            f"Alternatively, send:\n"
            f"  /setgroup {raw_arg!r}\n...and I'll show what I received."
        )
        return

    await context.bot_data["db"].set_runtime("destination_group_id", str(gid))
    # Also stash on the config object so handlers don't need to re-read each time
    cfg.destination_group_id = gid
    await update.effective_message.reply_text(
        f"Destination group set to {gid}.\nRun /refresh to discover its topics."
    )


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        return
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        await update.effective_message.reply_text("No destination group set. /setgroup <id> first.")
        return
    topics_mgr = context.bot_data["topics"]
    if not topics_mgr.user_session or not topics_mgr.user_session.available:
        await update.effective_message.reply_text(
            "Telethon user session not available. Run `python login.py` first "
            "(and configure API_ID/API_HASH)."
        )
        return
    await update.effective_message.reply_text("Refreshing topics...")
    topics = await topics_mgr.refresh(group_id)
    if not topics:
        await update.effective_message.reply_text(
            "No topics found. Make sure your user account is a member of the "
            "destination group and that the group is a forum (topics enabled)."
        )
        return
    listing = "\n".join(f"• `{t['id']}` — {t['title']}" for t in topics)
    await update.effective_message.reply_text(
        f"Found {len(topics)} topic(s):\n{listing}", parse_mode="Markdown"
    )


async def cmd_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        return
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        await update.effective_message.reply_text("No destination group set. /setgroup <id>.")
        return
    topics = await context.bot_data["topics"].get_topics(group_id)
    if not topics:
        await update.effective_message.reply_text("No topics known. /refresh or /addtopic first.")
        return
    listing = "\n".join(f"• `{t['id']}` — {t['title']}" for t in topics)
    await update.effective_message.reply_text(f"Known topics:\n{listing}", parse_mode="Markdown")


async def cmd_addtopic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/addtopic DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /addtopic <title> <topic_id>\nExample: /addtopic Videos 12"
        )
        return
    topic_id_str = context.args[-1]
    title = " ".join(context.args[:-1])
    topic_id = _safe_int(topic_id_str)
    if topic_id is None:
        await update.effective_message.reply_text(
            f"Could not parse '{topic_id_str}' as an integer (topic_id)."
        )
        return
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        await update.effective_message.reply_text("No destination group set. /setgroup <id> first.")
        return
    await db.add_topic_override(group_id, topic_id, title)
    await update.effective_message.reply_text(
        f"Added topic override: {title} -> {topic_id} for group {group_id}"
    )


async def cmd_deltopic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/deltopic DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /deltopic <topic_id>")
        return
    topic_id = _safe_int(context.args[0])
    if topic_id is None:
        await update.effective_message.reply_text(
            f"Could not parse '{context.args[0]}' as an integer (topic_id)."
        )
        return
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        return
    await db._conn.execute(
        "DELETE FROM topic_overrides WHERE group_id = ? AND topic_id = ?",
        (group_id, topic_id),
    )
    await db._conn.commit()
    await update.effective_message.reply_text(f"Removed topic override {topic_id}.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/status DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    user_session = context.bot_data.get("user_session")
    telethon_status = "n/a"
    if user_session:
        telethon_status = "connected" if user_session.available else "disconnected"
    elif not cfg.has_user_session:
        telethon_status = "not configured"

    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = v if v else None

    topics_count = 0
    if group_id is not None:
        topics_count = len(await context.bot_data["topics"].get_topics(int(group_id)))

    msg = (
        f"Bot status:\n"
        f"• Telethon user session: {telethon_status}\n"
        f"• Destination group: {group_id or '(not set)'}\n"
        f"• Known topics: {topics_count}\n"
        f"• Admin whitelist: {len(cfg.admin_ids)} user(s)"
    )
    await update.effective_message.reply_text(msg)


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Diagnostic command — shows the user their Telegram user ID and whether
    they are in the admin whitelist. Useful for debugging 'nothing happens'
    when ADMIN_IDS is misconfigured."""
    cfg = context.bot_data["config"]
    user = update.effective_user
    if not user:
        return
    is_admin = cfg.is_admin(user.id)
    admin_list = cfg.admin_ids if cfg.admin_ids else "(empty — everyone allowed)"
    await update.effective_message.reply_text(
        f"Your Telegram user ID: `{user.id}`\n"
        f"Your username: @{getattr(user, 'username', None)}\n"
        f"Your name: {getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}\n\n"
        f"Admin whitelist (ADMIN_IDS env var): {admin_list}\n"
        f"You are{' ' if is_admin else ' NOT '}authorized to use admin commands.",
        parse_mode="Markdown",
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        return
    # Remove all pending forwards for this user
    async with db._conn.execute(
        "DELETE FROM pending_forwards WHERE user_id = ?",
        (update.effective_user.id,),
    ) as cur:
        await db._conn.commit()
        n = cur.rowcount or 0
    # Cancel pending album tasks
    batches = context.chat_data.pop("album_batches", None)
    if batches:
        for batch in batches.values():
            t = batch.get("task")
            if t and not t.done():
                t.cancel()
    await update.effective_message.reply_text(f"Cancelled {n} pending forward(s).")


def register_admin_handlers(app) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("setgroup", cmd_setgroup))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("topics", cmd_topics))
    app.add_handler(CommandHandler("addtopic", cmd_addtopic))
    app.add_handler(CommandHandler("deltopic", cmd_deltopic))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
