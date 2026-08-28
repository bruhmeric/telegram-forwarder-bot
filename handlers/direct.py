"""Direct-forward flow.

When the admin sends any message to the bot:
  * For a single message: store as pending, immediately show inline topic picker
  * For a media group (album): accumulate with a short debounce, then show
    picker once for the whole album

When the user taps a topic button:
  * For single: bot.copy_message -> destination topic
  * For album: bot.send_media_group with extracted InputMedia -> destination topic
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import (
    Update, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

ALBUM_DEBOUNCE_SECONDS = 1.5


# ---------- message classification helpers ----------

def _describe(msg) -> str:
    if msg.text:
        return f"text: {msg.text[:60]!r}"
    if msg.photo:
        return f"photo ({len(msg.photo)} sizes)"
    if msg.video:
        return f"video {msg.video.file_id[:8]}..."
    if msg.document:
        return f"document {msg.document.file_name or msg.document.file_id[:8]}"
    if msg.audio:
        return "audio"
    if msg.voice:
        return "voice"
    if msg.video_note:
        return "video_note"
    if msg.sticker:
        return "sticker"
    if msg.animation:
        return "animation"
    if msg.location:
        return "location"
    if msg.contact:
        return "contact"
    return "other"


def _to_input_media(msg) -> Any | None:
    """Convert a Telegram message to an InputMedia* object usable by
    send_media_group. Returns None for non-media messages."""
    caption = msg.caption
    if msg.photo:
        # Use largest size
        return InputMediaPhoto(media=msg.photo[-1].file_id, caption=caption)
    if msg.video:
        return InputMediaVideo(media=msg.video.file_id, caption=caption)
    if msg.document:
        # Skip if document is not a media file (e.g. a sticker)
        return InputMediaDocument(media=msg.document.file_id, caption=caption)
    if msg.audio:
        return InputMediaAudio(media=msg.audio.file_id, caption=caption)
    if msg.animation:
        return InputMediaVideo(media=msg.animation.file_id, caption=caption)
    return None


# ---------- main entry ----------

async def handle_direct_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle any non-command message the admin sends to the bot."""
    if not update.effective_message or not update.effective_user:
        return
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    user_id = update.effective_user.id

    if not cfg.is_admin(user_id):
        await update.effective_message.reply_text(
            "You are not authorized to use this bot."
        )
        return

    msg = update.effective_message
    chat_id = update.effective_chat.id
    media_group_id = msg.media_group_id
    logger.info("Direct message from %s: %s (media_group=%s)",
                user_id, _describe(msg), media_group_id)

    if media_group_id:
        await _handle_album_message(update, context, chat_id, user_id, media_group_id, msg)
        return

    # Single message — create pending and show picker
    pending_id = await db.create_pending(
        user_id=user_id,
        source_chat_id=chat_id,
        source_message_id=msg.message_id,
        payload={"kind": "single"},
        kind="direct",
    )
    await _show_picker(update, context, pending_id, label=f"Got it. Pick a topic to forward this to:")


async def _handle_album_message(update, context, chat_id, user_id, media_group_id, msg):
    """Debounce album items, show picker once after accumulation."""
    db = context.bot_data["db"]
    batches = context.chat_data.setdefault("album_batches", {})
    key = (chat_id, media_group_id)
    batch = batches.get(key, {"user_id": user_id, "chat_id": chat_id, "message_ids": []})
    batch["message_ids"].append(msg.message_id)
    batches[key] = batch

    # Cancel previously scheduled picker (debounce)
    prev_task = batch.get("task")
    if prev_task and not prev_task.done():
        prev_task.cancel()

    async def _later():
        try:
            await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            ids = batch["message_ids"]
            pending_id = await db.create_pending(
                user_id=user_id,
                source_chat_id=chat_id,
                source_message_id=ids[0],
                payload={"kind": "album", "message_ids": ids},
                kind="direct",
            )
            # Send a NEW message to the user with the picker (since we can't
            # reply to the "last" album item cleanly)
            await _send_picker_new(context, chat_id, pending_id,
                                    label=f"Album of {len(ids)} items — pick a topic:")
            batches.pop(key, None)
        except Exception:
            logger.exception("album picker task failed")
            batches.pop(key, None)

    batch["task"] = asyncio.create_task(_later())


# ---------- picker UI ----------

async def _show_picker(update: Update, context, pending_id: str, label: str) -> None:
    topics_mgr = context.bot_data["topics"]
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    group_id = cfg.destination_group_id
    if group_id is None:
        group_id_raw = await db.get_runtime("destination_group_id")
        if not group_id_raw:
            await update.effective_message.reply_text(
                "No destination group set. Use /setgroup <group_id> first."
            )
            return
        group_id = int(group_id_raw)
    topics = await topics_mgr.get_topics(group_id)
    if not topics:
        await update.effective_message.reply_text(
            "No topics found. Try /refresh (requires Telethon user session) "
            "or add manually with /addtopic <title> <topic_id>."
        )
        return
    keyboard = topics_mgr.build_keyboard(pending_id, topics)
    await update.effective_message.reply_text(label, reply_markup=keyboard)


async def _send_picker_new(context, chat_id: int, pending_id: str, label: str) -> None:
    topics_mgr = context.bot_data["topics"]
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        await context.bot.send_message(chat_id=chat_id,
                                       text="No destination group set. Use /setgroup <group_id>.")
        return
    topics = await topics_mgr.get_topics(group_id)
    if not topics:
        await context.bot.send_message(chat_id=chat_id,
                                       text="No topics found. /refresh or /addtopic first.")
        return
    keyboard = topics_mgr.build_keyboard(pending_id, topics)
    await context.bot.send_message(chat_id=chat_id, text=label, reply_markup=keyboard)


# ---------- callback handler (topic button tapped) ----------

async def topic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Invoked when user taps a topic button under a pending forward."""
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = (q.data or "").split(":")
    # Expected formats:
    #   fwd:<pending_id>:<topic_id>
    #   cancel:<pending_id>
    if not data:
        return
    action = data[0]

    if action == "cancel":
        pending_id = data[1] if len(data) > 1 else ""
        if pending_id:
            await context.bot_data["db"].delete_pending(pending_id)
        await q.edit_message_text("Cancelled.")
        return

    if action != "fwd":
        return
    if len(data) < 3:
        return
    pending_id = data[1]
    topic_id = int(data[2])

    db = context.bot_data["db"]
    cfg = context.bot_data["config"]
    pending = await db.get_pending(pending_id)
    if not pending:
        await q.edit_message_text("This forward expired. Send the content again.")
        return

    user_id = pending["user_id"]
    if not cfg.is_admin(user_id):
        await q.edit_message_text("Not authorized.")
        await db.delete_pending(pending_id)
        return

    # Resolve destination group id
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        if not v:
            await q.edit_message_text("No destination group set. /setgroup <group_id>.")
            return
        group_id = int(v)

    payload = pending["payload"]
    kind = pending["kind"]
    src_chat_id = pending["source_chat_id"]
    src_msg_id = pending["source_message_id"]

    await q.edit_message_text(f"Forwarding to topic {topic_id}...")

    try:
        if kind == "direct":
            if payload.get("kind") == "album":
                await _forward_album(context, src_chat_id,
                                      payload["message_ids"], group_id, topic_id)
            else:
                await _forward_single(context, src_chat_id, src_msg_id,
                                       group_id, topic_id)
        elif kind == "link":
            await _forward_link(context, payload, group_id, topic_id)
    except Exception as e:
        logger.exception("forward failed")
        await q.edit_message_text(f"Forward failed: {e}")
        return

    await db.delete_pending(pending_id)
    await q.edit_message_text(f"Done — sent to topic {topic_id}.")


# ---------- actual forward primitives ----------

async def _forward_single(context, src_chat_id, src_msg_id, dest_group_id, topic_id) -> None:
    """Copy a single message to the destination topic using bot.copy_message."""
    await context.bot.copy_message(
        chat_id=dest_group_id,
        from_chat_id=src_chat_id,
        message_id=src_msg_id,
        message_thread_id=topic_id,
    )


async def _forward_album(context, src_chat_id, message_ids, dest_group_id, topic_id) -> None:
    """Send multiple messages as a media group (album) to the destination topic.

    Strategy:
      1. Fetch each source message via bot.get_chat + bot.forward_message? No —
         we don't have a "get message by id" in the Bot API. Instead, we use
         `bot.copy_message` for each item. Telegram will deliver them in order;
         they won't appear as a single grouped album in the destination but
         will appear sequentially under the same topic.

    If you need grouped albums in the destination, use the user_session /
    Telethon path to extract media and re-send via send_media_group. For now,
    the copy approach is simpler and preserves captions.
    """
    for mid in message_ids:
        await context.bot.copy_message(
            chat_id=dest_group_id,
            from_chat_id=src_chat_id,
            message_id=mid,
            message_thread_id=topic_id,
        )


async def _forward_link(context, payload, dest_group_id, topic_id) -> None:
    """Forward a previously-fetched locked-channel message to the destination
    topic. Used by handlers/link.py via the pending mechanism."""
    # The actual fetching happens in handlers/link.py before creating the
    # pending record. The payload contains pre-downloaded media file paths
    # and the original caption.
    from telethon.tl import types as tl

    bot = context.bot
    media_paths: list[dict] = payload.get("media_paths", [])
    caption: str | None = payload.get("caption")
    text: str | None = payload.get("text")

    if media_paths:
        # Use send_media_group for albums, send_* for single
        if len(media_paths) == 1:
            m = media_paths[0]
            await _send_one(bot, dest_group_id, topic_id, m, caption or text)
        else:
            from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument
            input_media = []
            for i, m in enumerate(media_paths):
                cap = caption if i == 0 else None
                if m["type"] == "photo":
                    input_media.append(InputMediaPhoto(media=open(m["path"], "rb"), caption=cap))
                elif m["type"] == "video":
                    input_media.append(InputMediaVideo(media=open(m["path"], "rb"), caption=cap))
                elif m["type"] == "document":
                    input_media.append(InputMediaDocument(media=open(m["path"], "rb"), caption=cap))
            await bot.send_media_group(
                chat_id=dest_group_id,
                media=input_media,
                message_thread_id=topic_id,
            )
    elif text:
        await bot.send_message(
            chat_id=dest_group_id, text=text, message_thread_id=topic_id,
        )
    else:
        raise RuntimeError("Empty link payload")


async def _send_one(bot, dest_group_id, topic_id, media, caption):
    path = media["path"]
    mtype = media["type"]
    if mtype == "photo":
        await bot.send_photo(chat_id=dest_group_id, photo=open(path, "rb"),
                              caption=caption, message_thread_id=topic_id)
    elif mtype == "video":
        await bot.send_video(chat_id=dest_group_id, video=open(path, "rb"),
                              caption=caption, message_thread_id=topic_id,
                              supports_streaming=True)
    elif mtype == "animation":
        await bot.send_animation(chat_id=dest_group_id, animation=open(path, "rb"),
                                  caption=caption, message_thread_id=topic_id)
    elif mtype == "audio":
        await bot.send_audio(chat_id=dest_group_id, audio=open(path, "rb"),
                              caption=caption, message_thread_id=topic_id)
    elif mtype == "voice":
        await bot.send_voice(chat_id=dest_group_id, voice=open(path, "rb"),
                              caption=caption, message_thread_id=topic_id)
    elif mtype == "document":
        await bot.send_document(chat_id=dest_group_id, document=open(path, "rb"),
                                 caption=caption, message_thread_id=topic_id)
    else:
        # fallback
        await bot.send_document(chat_id=dest_group_id, document=open(path, "rb"),
                                 caption=caption, message_thread_id=topic_id)
