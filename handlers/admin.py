"""Admin commands: /setgroup, /refresh, /addtopic, /help, /status."""
from __future__ import annotations

import logging
import re
import time
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
        "/saved <url>    — 🚀 FAST: send t.me link content to Saved Messages\n"
        "/scrape <url> [flags]  — 🤖 AUTO: scrape ALL media from a channel\n"
        "/stop_scrape    — 🛑 stop the active scrape\n"
        "/scrape_status  — 📊 check scrape progress\n"
        "/caption <text>  — 📝 set a custom caption (replaces original)\n"
        "/caption strip   — 📝 strip ALL captions from forwarded media\n"
        "/caption clear   — 📝 restore original captions\n"
        "/cancel         — cancel the latest pending forward\n"
        "\n*Sending content*\n"
        "• Send me a photo / video / text / file -> I show topics (if forum) "
        "or a single Forward button (if not) -> tap to forward\n"
        "• Send me a t.me/c/<id>/<msg> link -> I fetch via your Telethon "
        "session and let you pick a destination\n"
        "• `/saved <url>` -> skip the picker, send directly to Saved Messages "
        "(fastest path)\n"
        "• `/scrape <url>` -> scrape the entire channel and auto-send all "
        "media to your destination\n"
        "\n*Scrape flags*\n"
        "  `old` — oldest first (chronological)\n"
        "  `saved` — send to Saved Messages (default: destination group)\n"
        "  `photo` / `video` / `doc` / `audio` / `voice` / `animation` — filter by media type\n"
        "  `parallel=N` — set parallel sends (default 3, max 10)\n"
        "  Example: `/scrape https://t.me/c/123 saved old videos parallel=5`\n"
        "\n*Captions*\n"
        "  `/caption <text>` — set a custom caption applied to all forwards\n"
        "  `/caption strip` — strip ALL captions (forward media without any text)\n"
        "  `/caption clear` — restore original caption behavior\n"
        "  `/caption` (no args) — show current setting\n"
        "\n*Destination types*\n"
        "• Forum groups: pick a topic from the picker\n"
        "• Regular groups/channels: single Forward button (no topic picker)\n"
        "• Saved Messages: use /saved or /scrape saved",
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


async def cmd_saved(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a t.me link directly to Saved Messages via Telethon — FAST PATH.

    This bypasses the topic picker entirely. The user account sends the
    message to its own Saved Messages ("me" in Telethon), which is the
    fastest destination because:
      1. The user account is ALWAYS a member of its own Saved Messages
      2. forward_messages or send_message works directly (no topic thread)
      3. No need to wait for the user to tap a button

    Usage: /saved https://t.me/c/1234567890/42
    """
    from user_session import parse_telegram_link
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/saved DENIED — user_id=%s not in ADMIN_IDS=%s",
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
            "Usage: `/saved <t.me URL>`\n\n"
            "Sends the content directly to your Saved Messages (fastest path — "
            "no topic picker needed).\n\n"
            "Example: `/saved https://t.me/c/1234567890/42`",
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
        f"📩 Sending to Saved Messages...\n\n"
        f"Source: `{parsed.chat_ref}` / msg `{parsed.message_id}`",
        parse_mode="Markdown",
    )

    # Build a progress callback that updates the status message
    import time as _time
    last_update = {"time": 0.0, "text": "", "first": True}

    async def progress_cb(sent_bytes: int, total_bytes: int, label: str):
        now = _time.time()
        if total_bytes > 0:
            pct = (sent_bytes / total_bytes) * 100
            sent_mb = sent_bytes / (1024 * 1024)
            total_mb = total_bytes / (1024 * 1024)
            text = (f"📡 {label}\n\n"
                    f"Progress: {pct:.1f}%\n"
                    f"{sent_mb:.2f} / {total_mb:.2f} MB")
        else:
            text = f"📡 {label}..."

        if text == last_update["text"]:
            return

        if not last_update["first"]:
            if now - last_update["time"] < 0.5:
                return
        last_update["first"] = False
        last_update["time"] = now
        last_update["text"] = text
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass

    try:
        # Send to "me" — Telethon's special entity for Saved Messages.
        # This is a direct call to send_to_destination with dest_chat_id="me"
        # which Telethon resolves to the user's own Saved Messages chat.
        custom_caption = await _get_custom_caption(context)
        success, diag = await user_session.send_to_destination(
            source_chat_id=int(parsed.chat_ref) if parsed.kind == "private" else parsed.chat_ref,
            source_message_ids=[parsed.message_id],
            dest_chat_id="me",  # Saved Messages
            topic_id=None,
            progress_callback=progress_cb,
            custom_caption=custom_caption,
        )
    except Exception as e:
        logger.exception("/saved failed")
        await status_msg.edit_text(
            f"❌ Exception: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        return

    if success:
        await status_msg.edit_text("✅ Sent to Saved Messages!")
    else:
        err_lines = "\n".join(diag[-7:])
        await status_msg.edit_text(
            f"❌ Failed to send to Saved Messages.\n\n"
            f"Last diagnostic steps:\n{err_lines}",
        )


async def cmd_scrape(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scrape a channel — send all media (photos/videos) to destination.

    Usage:
      /scrape <channel_url> [flags]

    Flags (any combination, space-separated):
      old           — oldest first (chronological order)
      saved         — send to Saved Messages (default: destination group)
      photo         — only photos
      video         — only videos
      doc           — only documents
      audio         — only audio
      voice         — only voice messages
      animation     — only animations (GIFs)
      photos        — only photos (alias)
      videos        — only videos (alias)
      docs          — only documents (alias)
      parallel=N    — set parallel send count (default 3, max 10)

    Examples:
      /scrape https://t.me/publicchannel
      /scrape https://t.me/c/1234567890 saved old
      /scrape https://t.me/c/1234567890 photo video   — only photos and videos
      /scrape https://t.me/c/1234567890 saved old videos parallel=5

    Notes:
      - If no media type filter is given, ALL media is forwarded
      - Text-only messages are always skipped (they have no media)
      - Rate limit: 0.3 sec delay between sends (per parallel slot)
      - On FloodWait, the bot sleeps and retries automatically
      - Protected (noforwards) channels use the same three-tier fallback
        as /saved — forward → send_message(file=) → download+send_file
    """
    import asyncio
    from user_session import parse_channel_link, parse_telegram_link
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/scrape DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return
    user_session = context.bot_data.get("user_session")
    if not user_session or not user_session.available:
        await update.effective_message.reply_text(
            "❌ Telethon user session not available.\n"
            "Run `python login.py --string` locally and set SESSION_STRING env var."
        )
        return

    # Check if there's already an active scrape
    if context.bot_data.get("scrape_task") and not context.bot_data["scrape_task"].done():
        await update.effective_message.reply_text(
            "⚠️ A scrape is already running. Use /stop_scrape to stop it first, "
            "or /scrape_status to check progress."
        )
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: `/scrape <channel_url> [flags]`\n\n"
            "Flags:\n"
            "  `old` — oldest first (chronological)\n"
            "  `saved` — send to Saved Messages (default: destination group)\n"
            "  `photo` / `photos` — only photos\n"
            "  `video` / `videos` — only videos\n"
            "  `doc` / `docs` — only documents\n"
            "  `audio` — only audio\n"
            "  `voice` — only voice messages\n"
            "  `animation` — only animations (GIFs)\n"
            "  `parallel=N` — set parallel sends (default 3, max 10)\n\n"
            "Examples:\n"
            "  `/scrape https://t.me/publicchannel`\n"
            "  `/scrape https://t.me/c/1234567890 saved old`\n"
            "  `/scrape https://t.me/c/1234567890 photo video`\n"
            "  `/scrape https://t.me/c/123 saved old videos parallel=5`",
            parse_mode="Markdown",
        )
        return

    # Parse args: first arg is URL, rest are flags
    url = context.args[0]
    raw_flags = [a.lower() for a in context.args[1:]]
    send_to_saved = "saved" in raw_flags
    oldest_first = "old" in raw_flags or "oldest" in raw_flags

    # Parse media type filters
    valid_media_types = {"photo", "video", "animation", "document", "audio", "voice"}
    # Aliases: photos -> photo, videos -> video, docs -> document
    alias_map = {"photos": "photo", "videos": "video", "docs": "document"}
    media_types: list[str] = []
    parallel = 3  # default
    for flag in raw_flags:
        # Resolve aliases
        actual = alias_map.get(flag, flag)
        if actual in valid_media_types:
            if actual not in media_types:
                media_types.append(actual)
        elif flag.startswith("parallel="):
            try:
                p = int(flag.split("=", 1)[1])
                parallel = max(1, min(p, 10))  # clamp 1..10
            except ValueError:
                pass
    # If no media types specified, set to None (all media)
    if not media_types:
        media_types = None

    # Try parsing as a channel-only link first, then fall back to a post link
    parsed = parse_channel_link(url)
    if not parsed:
        # Maybe they passed a post link (t.me/c/123/42) — extract the channel part
        parsed_post = parse_telegram_link(url)
        if parsed_post:
            # Use the same chat_ref but message_id=0 (whole channel)
            parsed = type(parsed_post)(kind=parsed_post.kind,
                                        chat_ref=parsed_post.chat_ref,
                                        message_id=0)
    if not parsed:
        await update.effective_message.reply_text(
            f"❌ Could not parse URL: `{url}`\n\n"
            f"Supported formats:\n"
            f"  • `https://t.me/c/1234567890`  (private channel)\n"
            f"  • `https://t.me/channelname`   (public channel)\n"
            f"  • `https://t.me/c/1234567890/42`  (private channel + start msg)\n"
            f"  • `https://t.me/channelname/42`   (public channel + start msg)",
            parse_mode="Markdown",
        )
        return

    # Determine destination
    if send_to_saved:
        dest_chat_id = "me"
        dest_label = "Saved Messages"
    else:
        db = context.bot_data["db"]
        dest_chat_id = cfg.destination_group_id
        if dest_chat_id is None:
            v = await db.get_runtime("destination_group_id")
            dest_chat_id = int(v) if v else None
        if dest_chat_id is None:
            await update.effective_message.reply_text(
                "No destination group set. Either:\n"
                "  • /setgroup <group_id> first, OR\n"
                "  • Use /scrape <url> saved — to send to Saved Messages"
            )
            return
        dest_label = f"chat {dest_chat_id}"

    # Build the filter description for the status message
    filter_desc = "ALL media"
    if media_types:
        filter_desc = "only: " + ", ".join(media_types)

    # Initial status message
    status_msg = await update.effective_message.reply_text(
        f"🔍 Starting scrape...\n\n"
        f"Source: `{parsed.chat_ref}`\n"
        f"Destination: {dest_label}\n"
        f"Order: {'oldest first' if oldest_first else 'newest first'}\n"
        f"Filter: {filter_desc}\n"
        f"Parallel: {parallel} sends\n\n"
        f"_Use /stop_scrape to cancel, /scrape_status to check progress._",
        parse_mode="Markdown",
    )

    # Set up cancellation event and status storage
    cancel_event = asyncio.Event()
    context.bot_data["scrape_cancel"] = cancel_event
    context.bot_data["scrape_status"] = {
        "sent_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "total_seen": 0,
        "last_message_id": 0,
        "started_at": time.time(),
        "source_ref": parsed.chat_ref,
        "dest_label": dest_label,
        "order": "oldest" if oldest_first else "newest",
        "filter": filter_desc,
        "parallel": parallel,
    }

    # Build the status callback that edits the status message AND updates
    # context.bot_data["scrape_status"]
    last_status_text = {"value": ""}

    async def status_callback(text: str):
        # Throttle status edits to ~1 per 5s (the scrape_channel itself
        # already throttles to 5s, but we double-check here)
        if text == last_status_text["value"]:
            return
        last_status_text["value"] = text
        # Update bot_data status
        context.bot_data["scrape_status"].update({
            "last_update": time.time(),
        })
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass  # rate-limited or message unchanged

    # Stats callback — updates context.bot_data["scrape_status"] with the
    # latest counts from scrape_channel's result dict. This is what makes
    # /scrape_status show CURRENT progress (not stale data from start time).
    async def stats_callback(result_dict: dict):
        context.bot_data["scrape_status"].update({
            "sent_count": result_dict.get("sent_count", 0),
            "failed_count": result_dict.get("failed_count", 0),
            "skipped_count": result_dict.get("skipped_count", 0),
            "total_seen": result_dict.get("total_seen", 0),
            "last_message_id": result_dict.get("last_message_id", 0),
            "flood_waits": result_dict.get("flood_waits", 0),
            "cancelled": result_dict.get("cancelled", False),
            "last_update": time.time(),
        })

    # Run the scrape as a background task
    async def scrape_task():
        try:
            # Load the custom_caption setting (None=original, ""=strip, "<text>"=custom)
            custom_caption = await _get_custom_caption(context)
            await user_session.scrape_channel(
                source_chat_ref=int(parsed.chat_ref) if parsed.kind == "private" else parsed.chat_ref,
                dest_chat_id=dest_chat_id,
                topic_id=None,  # scraper doesn't support topics yet (use /saved for that)
                reverse=oldest_first,
                cancel_event=cancel_event,
                status_callback=status_callback,
                stats_callback=stats_callback,
                media_types=media_types,
                parallel=parallel,
                custom_caption=custom_caption,
            )
        except Exception as e:
            logger.exception("/scrape task failed")
            try:
                await status_msg.edit_text(f"❌ Scrape crashed: {type(e).__name__}: {e}")
            except Exception:
                pass
        finally:
            # Clean up the scrape state when done
            context.bot_data.pop("scrape_task", None)
            context.bot_data.pop("scrape_cancel", None)

    context.bot_data["scrape_task"] = asyncio.create_task(scrape_task())
    logger.info("Scrape started for %s -> %s", parsed.chat_ref, dest_label)


async def _get_custom_caption(context) -> str | None:
    """Load the custom_caption setting from DB / bot_data cache.
    Returns:
      None — use original captions (legacy behavior)
      "" — strip all captions
      "<text>" — use this custom caption
    """
    # Check in-memory cache first (set by /caption command)
    if "custom_caption" in context.bot_data:
        return context.bot_data["custom_caption"]
    # Fall back to DB
    db = context.bot_data["db"]
    raw = await db.get_runtime("custom_caption", None)
    if raw is None or raw == "__none__":
        return None  # use original
    return raw  # "" means strip, anything else is custom text


async def cmd_caption(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or clear a custom caption applied to all forwarded media.

    Usage:
      /caption <text>      — set a custom caption (applied to all forwards)
      /caption clear       — clear the custom caption (use original captions)
      /caption strip       — always strip captions (no caption at all)
      /caption             — show current setting

    When a custom caption is set:
      - /scrape sends media with your custom caption (no original captions)
      - /saved sends media with your custom caption (no original captions)
      - Direct forwards (links) send with your custom caption (no original)

    Use `/caption clear` to restore original-caption behavior.
    Use `/caption strip` to remove ALL captions (forward media without any text).
    """
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    if not cfg.is_admin(update.effective_user.id):
        logger.warning("/caption DENIED — user_id=%s not in ADMIN_IDS=%s",
                       update.effective_user.id, cfg.admin_ids)
        return

    if not context.args:
        # Show current setting
        current = await _get_custom_caption(context)
        if current is None:
            current_str = "(not set — using original captions)"
        elif current == "":
            current_str = "(strip mode — all captions removed)"
        else:
            preview = current[:200] + ("..." if len(current) > 200 else "")
            current_str = f"`{preview}`"
        await update.effective_message.reply_text(
            f"📝 Current caption setting:\n\n{current_str}\n\n"
            f"Usage:\n"
            f"  `/caption <text>` — set custom caption\n"
            f"  `/caption clear` — restore original captions\n"
            f"  `/caption strip` — remove all captions\n",
            parse_mode="Markdown",
        )
        return

    arg = " ".join(context.args)
    if arg.lower() == "clear":
        await db.set_runtime("custom_caption", "__none__")  # sentinel for "use original"
        # Also clear from in-memory config cache if present
        context.bot_data.pop("custom_caption", None)
        await update.effective_message.reply_text(
            "✅ Custom caption cleared. Forwards will use original captions."
        )
    elif arg.lower() == "strip":
        await db.set_runtime("custom_caption", "")  # empty string = strip all
        context.bot_data["custom_caption"] = ""
        await update.effective_message.reply_text(
            "✅ Caption mode: STRIP. All forwarded media will have no caption."
        )
    else:
        # Set custom caption (truncate to Telegram's 1024-char caption limit)
        if len(arg) > 1024:
            arg = arg[:1024]
            await update.effective_message.reply_text(
                f"⚠️ Caption truncated to 1024 chars (Telegram's limit)."
            )
        await db.set_runtime("custom_caption", arg)
        context.bot_data["custom_caption"] = arg
        preview = arg[:200] + ("..." if len(arg) > 200 else "")
        await update.effective_message.reply_text(
            f"✅ Custom caption set:\n\n`{preview}`\n\n"
            f"All forwarded media will use this caption instead of the original.",
            parse_mode="Markdown",
        )


async def cmd_stop_scrape(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop the currently running scrape."""
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        return
    cancel_event = context.bot_data.get("scrape_cancel")
    task = context.bot_data.get("scrape_task")
    if not task or task.done():
        await update.effective_message.reply_text("No active scrape to stop.")
        return
    # Set the cancel event — the scrape loop will pick it up on next iteration
    if cancel_event:
        cancel_event.set()
    await update.effective_message.reply_text(
        "🛑 Stop signal sent. The scrape will stop after the current message "
        "(within a few seconds). Use /scrape_status to see final stats."
    )


async def cmd_scrape_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current scrape status."""
    cfg = context.bot_data["config"]
    if not cfg.is_admin(update.effective_user.id):
        return
    status = context.bot_data.get("scrape_status")
    task = context.bot_data.get("scrape_task")
    if not status:
        await update.effective_message.reply_text("No scrape has been started yet.")
        return
    running = task and not task.done()
    elapsed = time.time() - status.get("started_at", 0) if status.get("started_at") else 0
    state_str = "🟢 running" if running else "🔴 finished"
    await update.effective_message.reply_text(
        f"📊 Scrape status: {state_str}\n\n"
        f"Source: `{status.get('source_ref', '?')}`\n"
        f"Destination: {status.get('dest_label', '?')}\n"
        f"Order: {status.get('order', '?')}\n"
        f"Filter: {status.get('filter', 'ALL media')}\n"
        f"Parallel: {status.get('parallel', 3)}\n"
        f"Elapsed: {elapsed:.0f} sec\n\n"
        f"Total seen: {status.get('total_seen', 0)}\n"
        f"Sent: {status.get('sent_count', 0)}\n"
        f"Failed: {status.get('failed_count', 0)}\n"
        f"Skipped (filtered/no media): {status.get('skipped_count', 0)}\n"
        f"Flood waits: {status.get('flood_waits', 0)}\n"
        f"Last msg ID: {status.get('last_message_id', 0)}",
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
    app.add_handler(CommandHandler("saved", cmd_saved))
    app.add_handler(CommandHandler("scrape", cmd_scrape))
    app.add_handler(CommandHandler("stop_scrape", cmd_stop_scrape))
    app.add_handler(CommandHandler("scrape_status", cmd_scrape_status))
    app.add_handler(CommandHandler("caption", cmd_caption))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
