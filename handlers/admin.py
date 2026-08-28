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
        "/setgroup <id>  — set destination group/channel (forum OR non-forum)\n"
        "/info           — show destination chat info (is it a forum? title?)\n"
        "/refresh        — re-fetch forum topics via Telethon (forum only)\n"
        "/topics         — list currently-known topics (forum only)\n"
        "/addtopic <title> <id>  — add a topic manually (forum only)\n"
        "/deltopic <id>  — remove a manually-added topic\n"
        "/status         — show bot status (Telethon, group, topics)\n"
        "/whoami         — show your Telegram user ID + admin status\n"
        "/test_link <url>  — diagnostic: test fetching a t.me link\n"
        "/cancel         — cancel the latest pending forward\n"
        "\n*Sending content*\n"
        "• Send me a photo / video / text / file -> I show topics (if forum) "
        "or a single Forward button (if not) -> tap to forward\n"
        "• Send me a t.me/c/<id>/<msg> link -> I fetch via your Telethon "
        "session and let you pick a destination\n"
        "\n*Destination types*\n"
        "• Forum groups: pick a topic from the picker\n"
        "• Regular groups/channels: single Forward button (no topic picker)",
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

    # Invalidate the cached "is_forum" check so it gets re-evaluated for the
    # new destination on next use
    context.bot_data.pop("destination_is_forum", None)
    context.bot_data.pop("destination_is_forum_group_id", None)
    context.bot_data.pop("destination_chat_title", None)

    # Try to fetch chat info immediately so we can tell the user whether
    # it's a forum or not (and update the cache at the same time)
    chat_type_info = ""
    try:
        chat = await context.bot.get_chat(chat_id=gid)
        is_forum = bool(getattr(chat, "is_forum", False))
        title = getattr(chat, "title", None) or "(no title)"
        # Cache it
        context.bot_data["destination_is_forum"] = is_forum
        context.bot_data["destination_is_forum_group_id"] = gid
        context.bot_data["destination_chat_title"] = title
        chat_type_info = (
            f"\n\n📊 Chat info:\n"
            f"  • Title: {title}\n"
            f"  • Type: {getattr(chat, 'type', '?')}\n"
            f"  • Is forum: {'✅ YES (use /refresh to discover topics)' if is_forum else '❌ NO (single Forward button)'}"
        )
    except Exception as e:
        chat_type_info = (
            f"\n\n⚠️ Couldn't fetch chat info: {type(e).__name__}: {e}\n"
            f"Make sure the bot is a member of this chat."
        )

    await update.effective_message.reply_text(
        f"✅ Destination group set to {gid}.{chat_type_info}"
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


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show destination chat info — is it a forum? what's the title?
    Useful for debugging "is my destination a forum or not?"""
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/info DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        await update.effective_message.reply_text("No destination group set. /setgroup <id> first.")
        return

    status_msg = await update.effective_message.reply_text(f"Fetching info for chat `{group_id}`...", parse_mode="Markdown")

    try:
        chat = await context.bot.get_chat(chat_id=group_id)
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Failed to fetch chat info: `{type(e).__name__}: {e}`\n\n"
            f"Make sure the bot is a member of the chat with permission to view chat info.",
            parse_mode="Markdown",
        )
        return

    is_forum = bool(getattr(chat, "is_forum", False))
    title = getattr(chat, "title", None) or "(no title)"
    chat_type = getattr(chat, "type", "?")
    username = getattr(chat, "username", None)
    member_count = "?"
    try:
        member_count = await context.bot.get_chat_member_count(chat_id=group_id)
    except Exception:
        pass

    # If it's a forum, list known topics
    topics_info = ""
    if is_forum:
        topics_mgr = context.bot_data.get("topics")
        if topics_mgr:
            topics = await topics_mgr.get_topics(group_id)
            if topics:
                topics_info = f"\n\n📋 Known topics ({len(topics)}):"
                for t in topics[:20]:  # show up to 20
                    topics_info += f"\n  • `{t['id']}` — {t['title']}"
                if len(topics) > 20:
                    topics_info += f"\n  ... and {len(topics) - 20} more"
            else:
                topics_info = "\n\n📋 No topics cached. Run /refresh to fetch them."
        else:
            topics_info = "\n\n📋 Topics manager not initialized."

    # Update the cache
    context.bot_data["destination_is_forum"] = is_forum
    context.bot_data["destination_is_forum_group_id"] = group_id
    context.bot_data["destination_chat_title"] = title

    await status_msg.edit_text(
        f"📊 Destination chat info:\n\n"
        f"• ID: `{group_id}`\n"
        f"• Title: {title}\n"
        f"• Type: {chat_type}\n"
        f"• Username: @{username}\n"
        f"• Is forum (has topics): {'✅ YES' if is_forum else '❌ NO'}\n"
        f"• Member count: {member_count}"
        f"{topics_info}",
        parse_mode="Markdown",
    )


async def cmd_test_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Diagnostic command — try to fetch a t.me link via Telethon and show
    the user a detailed report. Useful for debugging "forwarding from locked
    private channels doesn't work".

    Usage: /test_link https://t.me/c/1234567890/42
    """
    from user_session import parse_telegram_link
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/test_link DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    user_session = context.bot_data.get("user_session")
    if not user_session or not user_session.available:
        await update.effective_message.reply_text(
            "❌ Telethon user session not available.\n"
            "Run `python login.py --string` locally and set SESSION_STRING env var."
        )
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: `/test_link <t.me URL>`\n\n"
            "Example: `/test_link https://t.me/c/1234567890/42`\n"
            "         `/test_link https://t.me/somechannel/42`",
            parse_mode="Markdown",
        )
        return

    url = " ".join(context.args)
    parsed = parse_telegram_link(url)
    if not parsed:
        await update.effective_message.reply_text(
            f"❌ Could not parse URL: `{url}`\n\n"
            f"Supported formats:\n"
            f"  • `https://t.me/c/1234567890/42`  (private channel post)\n"
            f"  • `https://t.me/channelname/42`   (public channel post)",
            parse_mode="Markdown",
        )
        return

    status_msg = await update.effective_message.reply_text(
        f"🔍 Testing link...\n\n"
        f"Parsed:\n"
        f"  kind: `{parsed.kind}`\n"
        f"  chat_ref: `{parsed.chat_ref}`\n"
        f"  message_id: `{parsed.message_id}`\n\n"
        f"Fetching via Telethon...",
        parse_mode="Markdown",
    )

    try:
        result = await user_session.test_link(parsed)
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Exception: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        return

    # Build report
    steps_text = "\n".join(result.get("steps", []))
    if len(steps_text) > 3000:
        steps_text = steps_text[:3000] + "\n... (truncated)"

    report = (
        f"📋 Test link report:\n\n"
        f"Parsed:\n"
        f"  kind: `{result['parsed']['kind']}`\n"
        f"  chat_ref: `{result['parsed']['chat_ref']}`\n"
        f"  message_id: `{result['parsed']['message_id']}`\n\n"
        f"Result:\n"
        f"  success: {'✅ YES' if result['success'] else '❌ NO'}\n"
        f"  has_media: {'✅' if result['has_media'] else '❌'}\n"
        f"  media_type: `{result['media_type']}`\n"
        f"  chat_title: {result['chat_title'] or '(unknown)'}\n"
        f"  error: {result['error'] or '(none)'}\n\n"
        f"Steps:\n```\n{steps_text}\n```"
    )

    await status_msg.edit_text(report, parse_mode="Markdown")


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
    # Cancel pending batch tasks (time-based batch window)
    # Look for both old and new batch keys for backward compatibility
    batches = context.chat_data.pop("msg_batches", None)
    if batches:
        for batch in batches.values():
            t = batch.get("task")
            if t and not t.done():
                t.cancel()
    # Legacy key (old album_batches)
    legacy_batches = context.chat_data.pop("album_batches", None)
    if legacy_batches:
        for batch in legacy_batches.values():
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
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("test_link", cmd_test_link))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
