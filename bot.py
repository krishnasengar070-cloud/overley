"""Telegram video worker. Run one polling instance per token."""
import asyncio
import logging
import os
import shutil
import tempfile
import sqlite3
from contextlib import closing
from pathlib import Path

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, ReplyParameters, Update
from telegram.error import TelegramError, NetworkError, BadRequest
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from media import MediaError, inspect_video, render, supports_nvenc

ROOT = Path(tempfile.gettempdir()) / "telegram-glass-video-jobs"
LIMIT = 20 * 1024 * 1024
HELP_BUTTON = "How to use"
SET_BG = "Set background"
REMOVE_BG = "Remove background"
KEYBOARD = ReplyKeyboardMarkup([[SET_BG, REMOVE_BG], [HELP_BUTTON]], resize_keyboard=True, is_persistent=True)
GUIDE = (
    "How to use\n\n"
    "1. Attach your original video (up to 20 MB).\n"
    "2. Send up to ten videos together, individually or as an album.\n"
    "3. Save the finished MP4 from this chat.\n\n"
    "Optional: tap Set background and send a profile clip. Future videos will appear below its header. "
    "Tap Remove background for glass-only videos. Use /cancel to cancel setup.\n\n"
    "If the edited video looks unusual in Telegram’s preview, save it to your phone and open the MP4 in your gallery to see the full frame.\n\n"
    "The frame reflects the movement and colours at the edges of your clip."
)


class StartupApplication(Application):
    async def initialize(self):
        for attempt in range(1, 4):
            try:
                await super().initialize()
                return
            except BadRequest:
                raise
            except NetworkError:
                if attempt == 3:
                    raise
                delay = 3 * attempt
                print(f"Could not connect to Telegram. Retrying in {delay} seconds.", flush=True)
                await asyncio.sleep(delay)


async def start(update: Update, context):
    await update.effective_message.reply_text(
        "Send a video to add a reflective glass frame. Tap How to use for instructions.",
        reply_markup=KEYBOARD)


async def show_help(update: Update, context):
    await update.effective_message.reply_text(GUIDE, reply_markup=KEYBOARD)


async def choose_background(update: Update, context):
    context.user_data["awaiting_background"] = update.effective_chat.id
    await update.effective_message.reply_text(
        "Send your background video (up to 20 MB). Place your profile and username "
        "in the top fifth of the frame. Use /cancel to keep your current setting.")


async def cancel_setup(update: Update, context):
    context.user_data.pop("awaiting_background", None)
    await update.effective_message.reply_text("Background setup closed.", reply_markup=KEYBOARD)


async def remove_background(update: Update, context):
    context.user_data.pop("awaiting_background", None)
    db = context.bot_data["database"]
    with db:
        db.execute("DELETE FROM backgrounds WHERE user_id = ?", (update.effective_user.id,))
    cached = context.bot_data.get("recent_background")
    if cached and cached[0] == update.effective_user.id:
        context.bot_data.pop("recent_background", None)
    await update.effective_message.reply_text("Glass-only mode is active.", reply_markup=KEYBOARD)


async def process(update: Update, context):
    message = update.effective_message
    item = message.video or message.document
    if item.file_size and item.file_size > LIMIT:
        await message.reply_text("This video exceeds 20 MB. Please send a smaller export.")
        return
    db = context.bot_data["database"]
    user_id = update.effective_user.id
    saving = context.user_data.get("awaiting_background") == update.effective_chat.id
    row = db.execute("SELECT file_id FROM backgrounds WHERE user_id = ?", (user_id,)).fetchone()
    if saving:
        await process_file(context, message, item, user_id, None, True)
        return

    data = context.bot_data
    if data["pending"] >= 40:
        await message.reply_text("The queue is full. Please send this video again shortly.")
        return
    data["pending"] += 1
    context.application.create_task(queued_file(context, message, item, user_id, row), update=update)


async def queued_file(context, message, item, user_id, row):
    try:
        async with context.bot_data["transfer_slots"]:
            await process_file(context, message, item, user_id, row, False)
    finally:
        context.bot_data["pending"] -= 1


async def process_file(context, message, item, user_id, row, saving):
    db = context.bot_data["database"]
    status = None
    try:
        status = await message.reply_text("⌛", reply_markup=KEYBOARD)
        with tempfile.TemporaryDirectory(prefix="job-", dir=ROOT) as folder:
            source = Path(folder) / "input"
            result = Path(folder) / "glass-video.mp4"
            remote = await context.bot.get_file(item.file_id)
            await remote.download_to_drive(source, read_timeout=120)
            if saving:
                await asyncio.to_thread(inspect_video, source)
                with db:
                    db.execute("INSERT OR REPLACE INTO backgrounds VALUES (?, ?)",
                               (user_id, item.file_id))
                context.bot_data["recent_background"] = (user_id, item.file_id, source.read_bytes())
                context.user_data.pop("awaiting_background", None)
                await message.reply_text("Background saved. Send the main video when ready.",
                                         reply_markup=KEYBOARD)
                return
            background = None
            if row:
                background = Path(folder) / "background"
                async with context.bot_data["cache_lock"]:
                    cached = context.bot_data.get("recent_background")
                    if cached and cached[:2] == (user_id, row[0]):
                        background.write_bytes(cached[2])
                    else:
                        file = await context.bot.get_file(row[0])
                        await file.download_to_drive(background, read_timeout=120)
                        context.bot_data["recent_background"] = (user_id, row[0], background.read_bytes())
            async with context.bot_data["render_slots"]:
                await asyncio.to_thread(render, source, result, background)
            with result.open("rb") as stream:
                await message.reply_video(stream, filename=result.name, supports_streaming=True,
                                          caption="Your framed video is ready.",
                                          reply_parameters=ReplyParameters(
                                              message_id=message.message_id, allow_sending_without_reply=True),
                                          read_timeout=180, write_timeout=180)
    except MediaError as exc:
        await message.reply_text(str(exc))
    except TelegramError:
        await message.reply_text("The Telegram transfer failed. Please try sending the video again.")
    except Exception:
        logging.exception("Video processing failed")
        await message.reply_text("Processing failed. Please try another video export.")
    finally:
        if status is not None:
            try:
                await status.delete()
            except TelegramError:
                logging.debug("The processing indicator could not be removed.")


def main():
    load_dotenv()
    token = os.environ.get("BOT_TOKEN")
    if not token or token == "YOUR_BOT_TOKEN":
        raise SystemExit("Set BOT_TOKEN in .env or in your Railway variables.")
    if not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")):
        raise SystemExit("Install FFmpeg and make ffmpeg and ffprobe available on PATH.")
    ROOT.mkdir(mode=0o700, exist_ok=True)
    # Reclaim interrupted jobs on restart. Use a separate temp root per deployment.
    for folder in ROOT.glob("job-*"):
        if folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder)
    app = (Application.builder().application_class(StartupApplication).token(token)
           .connect_timeout(20).get_updates_connect_timeout(20)
           .concurrent_updates(False).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("background", choose_background))
    app.add_handler(CommandHandler("removebackground", remove_background))
    app.add_handler(CommandHandler("cancel", cancel_setup))
    app.add_handler(MessageHandler(filters.Text([SET_BG]), choose_background))
    app.add_handler(MessageHandler(filters.Text([REMOVE_BG]), remove_background))
    app.add_handler(CommandHandler(["help", "howto"], show_help))
    app.add_handler(MessageHandler(filters.Text([HELP_BUTTON]), show_help))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, process))
    app.add_handler(MessageHandler(filters.ALL, start))
    data_dir = Path(os.environ.get("BOT_DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(data_dir / "backgrounds.sqlite3")) as db:
        db.execute("CREATE TABLE IF NOT EXISTS backgrounds "
                   "(user_id INTEGER PRIMARY KEY, file_id TEXT NOT NULL)")
        app.bot_data.update(database=db, pending=0, transfer_slots=asyncio.Semaphore(3),
                            cache_lock=asyncio.Lock(),
                            render_slots=asyncio.Semaphore(2 if supports_nvenc() else 1))
        try:
            app.run_polling(allowed_updates=["message"])
        except BadRequest:
            raise
        except NetworkError:
            raise SystemExit("Could not connect to Telegram. Check connectivity to api.telegram.org and restart.") from None


if __name__ == "__main__":
    main()
