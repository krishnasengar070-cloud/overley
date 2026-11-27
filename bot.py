"""Telegram video worker. Run one polling instance per token."""
import asyncio
from telegram import InputMediaPhoto
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

from media import MediaError, inspect_video, render, prepare_logo, preview_mark, placement_preview

ROOT = Path(tempfile.gettempdir()) / "telegram-glass-video-jobs"
LIMIT = 20 * 1024 * 1024
HELP_BUTTON = "How to use"
SET_BG = "Set background"
REMOVE_BG = "Remove background"
KEYBOARD = ReplyKeyboardMarkup(
    [[SET_BG], ["Logo mode", "Background + logo"], ["Change logo", "Logo off"],
     ["Adjust logo", HELP_BUTTON], ["/cancel"]],
    resize_keyboard=True, is_persistent=True,
)
REMOVE = "Yes, remove background"
KEEP = "No, keep original"
BACKGROUND_KEYS = ReplyKeyboardMarkup([[REMOVE], [KEEP], ["/cancel"]], resize_keyboard=True)
POSITION_KEYS = ReplyKeyboardMarkup([["Top half", "Bottom half"], ["/cancel"]], resize_keyboard=True)
EDIT_ROWS = [["Top", "Bottom"], ["↑ Up", "↓ Down", "← Left", "→ Right"],
             ["− Smaller", "+ Larger"], ["Save placement", "Discard"]]
EDIT_KEYS = ReplyKeyboardMarkup(EDIT_ROWS, resize_keyboard=True)
GUIDE = (
    "How to use\n\n"
    "1. For a glass frame, send a video (up to 20 MB). For a logo, tap Logo mode. "
    "For a background too, tap Set background, send your template, then tap Background + logo.\n\n"
    "2. Upload a tightly cropped logo, keeping its full animation inside the crop. "
    "Choose whether to keep its original background or remove a solid colour.\n\n"
    "3. Choose Top half or Bottom half to start. The preview shows your logo on a 9:16 grid. "
    "Use the arrows to move it by 2% of the frame. Smaller and Larger change size by 5 points "
    "from 10% to 100%. Top and Bottom return it to the centered preset.\n\n"
    "4. Tap Save placement to apply it to future videos, or Discard to keep your previous settings. "
    "Tap Adjust logo any time to reopen the preview. Logo mode follows the original video shape, "
    "so framing can differ from the portrait preview. Background + logo exports in 9:16.\n\n"
    "5. Send one video or a batch. Results arrive one at a time. If Telegram’s preview looks unusual, "
    "save the MP4 and open it in your gallery. Use Change logo to replace the logo, Logo off to disable it, "
    "or Set background again to remove the background."
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
    context.user_data.pop("editor", None)
    await update.effective_message.reply_text(
        "Send a video to add a reflective glass frame. Tap the keyboard icon beside the message field to see all options.",
        reply_markup=KEYBOARD)


async def show_commands(update, context):
    await update.effective_message.reply_text(
        "Choose a command below. Set background toggles your saved background. "
        "Use /cancel to exit setup.", reply_markup=KEYBOARD)


async def show_help(update: Update, context):
    await update.effective_message.reply_text(GUIDE, reply_markup=KEYBOARD)


async def logo_mode(update, context):
    context.user_data.pop("editor", None)
    user = update.effective_user.id
    db = context.bot_data["database"]
    text = update.effective_message.text
    context.user_data.pop("pending_background_choice", None)
    context.user_data.pop("pending_position", None)
    context.user_data.pop("awaiting_logo", None)
    context.user_data.pop("awaiting_background", None)
    if text == "Logo off":
        with db:
            db.execute("UPDATE logos SET mode='off' WHERE user_id=?", (user,))
        await update.effective_message.reply_text("Logo off. Your saved logo is available for next time.", reply_markup=KEYBOARD)
        return
    mode = "both" if text == "Background + logo" else "logo"
    if mode == "both" and not db.execute("SELECT 1 FROM backgrounds WHERE user_id=?", (user,)).fetchone():
        await update.effective_message.reply_text("Tap Set background and save a video first, then tap Background + logo.", reply_markup=KEYBOARD)
        return
    row = db.execute("SELECT mode FROM logos WHERE user_id=?", (user,)).fetchone()
    if text == "Change logo" or row is None:
        if text == "Change logo" and row:
            mode = "both" if row[0] == "both" else "logo"
        context.user_data["awaiting_logo"] = (update.effective_chat.id, mode)
        await update.effective_message.reply_text("First crop your file to just the logo or banner, including its full motion. Send the cropped PNG, JPEG, GIF or MP4 (up to 20 MB). "
                                                 "For transparency, send a PNG or GIF as a file. Use /cancel to exit.", reply_markup=KEYBOARD)
        return
    with db:
        db.execute("UPDATE logos SET mode=? WHERE user_id=?", (mode, user))
    await update.effective_message.reply_text(f"{text} enabled. Send your main video.", reply_markup=KEYBOARD)


async def choose_background(update: Update, context):
    context.user_data.pop("editor", None)
    context.user_data.pop("pending_background_choice", None)
    context.user_data.pop("pending_position", None)
    context.user_data.pop("awaiting_logo", None)
    db = context.bot_data["database"]
    if db.execute("SELECT 1 FROM backgrounds WHERE user_id=?", (update.effective_user.id,)).fetchone():
        await remove_background(update, context)
        return
    context.user_data["awaiting_background"] = update.effective_chat.id
    await update.effective_message.reply_text(
        "Send your background video (up to 20 MB). Place your profile and username "
        "in the top fifth of the frame. Use /cancel to keep your current setting.", reply_markup=KEYBOARD)


async def cancel_setup(update: Update, context):
    context.user_data.pop("editor", None)
    context.user_data.pop("awaiting_background", None)
    context.user_data.pop("pending_background_choice", None)
    context.user_data.pop("pending_position", None)
    context.user_data.pop("awaiting_logo", None)
    await update.effective_message.reply_text("Setup closed.", reply_markup=KEYBOARD)


async def remove_background(update: Update, context):
    context.user_data.pop("awaiting_background", None)
    db = context.bot_data["database"]
    with db:
        db.execute("UPDATE logos SET mode='logo' WHERE mode='both' AND user_id=?", (update.effective_user.id,))
        db.execute("DELETE FROM backgrounds WHERE user_id = ?", (update.effective_user.id,))
    cached = context.bot_data.get("recent_background")
    if cached and cached[0] == update.effective_user.id:
        context.bot_data.pop("recent_background", None)
    await update.effective_message.reply_text("Background removed.", reply_markup=KEYBOARD)


async def choose_logo_background(update, context):
    pending = context.user_data.get("pending_background_choice")
    if not pending or pending[0] != update.effective_chat.id:
        await update.effective_message.reply_text("Use Change logo to upload a cropped logo first.", reply_markup=KEYBOARD)
        return
    remove_bg = update.effective_message.text == REMOVE
    context.user_data["pending_position"] = (*pending, remove_bg)
    context.user_data.pop("pending_background_choice")
    await update.effective_message.reply_text("Where should this logo appear?", reply_markup=POSITION_KEYS)


async def open_editor(update, context):
    message = update.effective_message
    chat = update.effective_chat.id
    user = update.effective_user.id
    db = context.bot_data["database"]
    pending = context.user_data.get("pending_position")
    new = message.text in ("Top half", "Bottom half")
    if new:
        if not pending or pending[0] != chat:
            await message.reply_text("Use Adjust logo to edit your saved logo.", reply_markup=KEYBOARD)
            return
        _, file_id, mode, remove = pending
        size, x, y = 95, 50, 12 if message.text == "Top half" else 88
    else:
        row = db.execute("SELECT file_id, mode, remove_bg, size_pct, x_pct, y_pct FROM logos WHERE user_id=?",
                         (user,)).fetchone()
        if not row:
            await message.reply_text("Tap Logo mode and upload your logo first.", reply_markup=KEYBOARD)
            return
        file_id, mode, remove, size, x, y = row
    try:
        with tempfile.TemporaryDirectory(prefix="job-", dir=ROOT) as folder:
            path = Path(folder) / "logo"
            cached = context.bot_data.get("recent_logo")
            if cached and cached[0] == file_id:
                path.write_bytes(cached[1])
            else:
                remote = await context.bot.get_file(file_id)
                await remote.download_to_drive(path, read_timeout=120)
            mark = await asyncio.to_thread(preview_mark, path, bool(remove))
        draft = dict(chat=chat, file=file_id, mode=mode, remove=remove,
                     size=size, x=x, y=y, mark=mark)
        picture = await message.reply_photo(placement_preview(mark, size, x, y),
                                            caption=editor_caption(draft), reply_markup=EDIT_KEYS)
    except (MediaError, TelegramError):
        await message.reply_text("Could not open the preview. Try again.", reply_markup=POSITION_KEYS if new else KEYBOARD)
        return
    draft["message"] = picture.message_id
    for key in ("awaiting_logo", "awaiting_background", "pending_background_choice", "pending_position"):
        context.user_data.pop(key, None)
    context.user_data["editor"] = draft


def editor_caption(d):
    return (f"9:16 placement preview · Size {d['size']}%\n"
            f"Target center: {d['x']}% from left, {d['y']}% from top. "
            "Edges stop at the frame. Tap Save placement when ready.")


async def edit_logo(update, context):
    message = update.effective_message
    draft = context.user_data.get("editor")
    if not draft or draft["chat"] != update.effective_chat.id:
        await message.reply_text("Tap Adjust logo to open the preview.", reply_markup=KEYBOARD)
        return
    action = message.text
    if action in ("Save placement", "Discard"):
        if action == "Save placement":
            db = context.bot_data["database"]
            with db:
                db.execute("INSERT OR REPLACE INTO logos(user_id,file_id,mode,position,remove_bg,size_pct,x_pct,y_pct) "
                           "VALUES(?,?,?,?,?,?,?,?)", (update.effective_user.id, draft["file"], draft["mode"],
                           "top" if draft["y"] <= 50 else "bottom", draft["remove"], draft["size"], draft["x"], draft["y"]))
        context.user_data.pop("editor")
        await message.reply_text("Placement saved for future videos." if action == "Save placement" else
                                 "Draft discarded.", reply_markup=KEYBOARD)
        return
    updated = draft.copy()
    if action in ("Top", "Bottom"):
        updated.update(x=50, y=12 if action == "Top" else 88)
    else:
        key, delta = {"↑ Up": ("y", -2), "↓ Down": ("y", 2), "← Left": ("x", -2),
                      "→ Right": ("x", 2), "− Smaller": ("size", -5), "+ Larger": ("size", 5)}[action]
        updated[key] = min(100, max(10 if key == "size" else 0, updated[key] + delta))
    if updated == draft:
        return
    photo = placement_preview(updated["mark"], updated["size"], updated["x"], updated["y"])
    try:
        await context.bot.edit_message_media(chat_id=draft["chat"], message_id=draft["message"],
                                             media=InputMediaPhoto(photo, caption=editor_caption(updated)))
    except TelegramError:
        sent = await message.reply_photo(photo, caption=editor_caption(updated), reply_markup=EDIT_KEYS)
        updated["message"] = sent.message_id
    context.user_data["editor"] = updated


async def process(update: Update, context):
    message = update.effective_message
    draft = context.user_data.get("editor")
    if draft and draft["chat"] == update.effective_chat.id:
        await message.reply_text("Save or discard the placement before sending a video.", reply_markup=EDIT_KEYS)
        return
    choice = context.user_data.get("pending_background_choice")
    if choice and choice[0] == update.effective_chat.id:
        await message.reply_text("Remove the logo background?", reply_markup=BACKGROUND_KEYS)
        return
    pending = context.user_data.get("pending_position")
    if pending and pending[0] == update.effective_chat.id:
        await message.reply_text("Choose a position or use /cancel.", reply_markup=POSITION_KEYS)
        return
    setup = context.user_data.get("awaiting_logo")
    saving_logo = setup and setup[0] == update.effective_chat.id
    item = message.video or message.animation or message.document or (message.photo[-1] if message.photo else None)
    if not saving_logo and not (message.video or (message.document and (message.document.mime_type or "").startswith("video/"))):
        await start(update, context)
        return
    if item.file_size and item.file_size > LIMIT:
        await message.reply_text("This video exceeds 20 MB. Please send a smaller export.", reply_markup=KEYBOARD)
        return
    db = context.bot_data["database"]
    user_id = update.effective_user.id
    if saving_logo:
        try:
            with tempfile.TemporaryDirectory(prefix="job-", dir=ROOT) as folder:
                path = Path(folder) / "logo"
                remote = await context.bot.get_file(item.file_id)
                await remote.download_to_drive(path, read_timeout=120)
                await asyncio.to_thread(prepare_logo, path, False)
                cached_logo = (item.file_id, path.read_bytes(), None, None)
            context.bot_data["recent_logo"] = cached_logo
            context.user_data.pop("awaiting_logo", None)
            context.user_data["pending_background_choice"] = (update.effective_chat.id, item.file_id, setup[1])
            await message.reply_text("Remove the solid background from this logo?", reply_markup=BACKGROUND_KEYS)
        except (MediaError, TelegramError):
            await message.reply_text("Could not load that logo. Send a PNG, JPEG, GIF or MP4, or use /cancel.", reply_markup=KEYBOARD)
        return
    logo = db.execute("SELECT file_id, mode, position, remove_bg, size_pct, x_pct, y_pct FROM logos WHERE user_id=? AND mode!='off'", (user_id,)).fetchone()
    saving = context.user_data.get("awaiting_background") == update.effective_chat.id
    row = db.execute("SELECT file_id FROM backgrounds WHERE user_id = ?", (user_id,)).fetchone()
    if saving:
        await process_file(context, message, item, user_id, None, True)
        return

    if logo and logo[1] == "logo":
        row = None
    # Sequential update handling keeps the indicator, render and delivery in order.
    await process_file(context, message, item, user_id, row, False, logo)


async def process_file(context, message, item, user_id, row, saving, logo=None):
    db = context.bot_data["database"]
    status = None
    try:
        status = await message.reply_text("⌛")
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
                cached = context.bot_data.get("recent_background")
                if cached and cached[:2] == (user_id, row[0]):
                    background.write_bytes(cached[2])
                else:
                    file = await context.bot.get_file(row[0])
                    await file.download_to_drive(background, read_timeout=120)
                    context.bot_data["recent_background"] = (user_id, row[0], background.read_bytes())
            logo_path = None
            details = None
            if logo:
                logo_path = Path(folder) / "logo"
                cached = context.bot_data.get("recent_logo")
                if cached and cached[0] == logo[0]:
                    logo_path.write_bytes(cached[1])
                    if cached[3] == logo[3]:
                        details = cached[2]
                else:
                    mark = await context.bot.get_file(logo[0])
                    await mark.download_to_drive(logo_path, read_timeout=120)
                if details is None:
                    details = await asyncio.to_thread(prepare_logo, logo_path, bool(logo[3]))
                    context.bot_data["recent_logo"] = (logo[0], logo_path.read_bytes(), details, logo[3])
            await asyncio.to_thread(render, source, result, background, logo_path, details, logo[2] if logo else "top",
                                    *(logo[4:7] if logo else (95, 50, None)))
            with result.open("rb") as stream:
                await message.reply_video(stream, filename=result.name, supports_streaming=True,
                                          caption="Your framed video is ready.",
                                          reply_parameters=ReplyParameters(
                                              message_id=message.message_id, allow_sending_without_reply=True),
                                          read_timeout=180, write_timeout=180, reply_markup=KEYBOARD)
    except MediaError as exc:
        await message.reply_text(str(exc), reply_markup=KEYBOARD)
    except TelegramError:
        await message.reply_text("The Telegram transfer failed. Please try sending the video again.", reply_markup=KEYBOARD)
    except Exception:
        logging.exception("Video processing failed")
        await message.reply_text("Processing failed. Please try another video export.", reply_markup=KEYBOARD)
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
    app.add_handler(MessageHandler(filters.Text([REMOVE, KEEP]), choose_logo_background))
    app.add_handler(MessageHandler(filters.Text(["Adjust logo", "Top half", "Bottom half"]), open_editor))
    app.add_handler(MessageHandler(filters.Text([label for row in EDIT_ROWS for label in row]), edit_logo))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("commands", show_commands))
    app.add_handler(MessageHandler(filters.Text(["Commands"]), show_commands))
    app.add_handler(CommandHandler("background", choose_background))
    app.add_handler(CommandHandler("removebackground", remove_background))
    app.add_handler(CommandHandler("cancel", cancel_setup))
    app.add_handler(MessageHandler(filters.Text([SET_BG]), choose_background))
    app.add_handler(MessageHandler(filters.Text([REMOVE_BG]), remove_background))
    app.add_handler(CommandHandler(["help", "howto"], show_help))
    app.add_handler(MessageHandler(filters.Text([HELP_BUTTON]), show_help))
    app.add_handler(MessageHandler(filters.Text(["Logo mode", "Background + logo", "Change logo", "Logo off"]), logo_mode))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL | filters.ANIMATION | filters.PHOTO, process))
    app.add_handler(MessageHandler(filters.ALL, start))
    data_dir = Path(os.environ.get("BOT_DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(data_dir / "backgrounds.sqlite3")) as db:
        db.execute("CREATE TABLE IF NOT EXISTS backgrounds "
                   "(user_id INTEGER PRIMARY KEY, file_id TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS logos (user_id INTEGER PRIMARY KEY, file_id TEXT NOT NULL, mode TEXT NOT NULL)")
        if "position" not in {col[1] for col in db.execute("PRAGMA table_info(logos)")}:
            with db:
                db.execute("ALTER TABLE logos ADD COLUMN position TEXT NOT NULL DEFAULT 'top'")
        if "remove_bg" not in {col[1] for col in db.execute("PRAGMA table_info(logos)")}:
            with db:
                db.execute("ALTER TABLE logos ADD COLUMN remove_bg INTEGER NOT NULL DEFAULT 1")
        columns = {col[1] for col in db.execute("PRAGMA table_info(logos)")}
        with db:
            if "size_pct" not in columns:
                db.execute("ALTER TABLE logos ADD COLUMN size_pct INTEGER NOT NULL DEFAULT 95")
            if "x_pct" not in columns:
                db.execute("ALTER TABLE logos ADD COLUMN x_pct INTEGER NOT NULL DEFAULT 50")
            if "y_pct" not in columns:
                db.execute("ALTER TABLE logos ADD COLUMN y_pct INTEGER NOT NULL DEFAULT 12")
                db.execute("UPDATE logos SET y_pct=88 WHERE position='bottom'")
        app.bot_data["database"] = db
        try:
            app.run_polling(allowed_updates=["message"])
        except BadRequest:
            raise
        except NetworkError:
            raise SystemExit("Could not connect to Telegram. Check connectivity to api.telegram.org and restart.") from None


if __name__ == "__main__":
    main()
