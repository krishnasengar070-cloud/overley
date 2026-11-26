"""Render a translucent glass rim over the original video."""
import json
import math
import subprocess
import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageChops

MAX_OUTPUT = 49_000_000
FORMATS = "mov,matroska,webm,avi,flv,mpeg,mpegts,asf,ogg"


class MediaError(Exception):
    pass


def run(args):
    try:
        return subprocess.run(args, check=True, capture_output=True, timeout=900).stdout
    except subprocess.TimeoutExpired as exc:
        raise MediaError("This video took too long to process.") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaError("This video could not be decoded or exported.") from exc


# Fractions of the shorter video side, so the same style works at any resolution.
LOGO_WIDTH_FRACTION = 0.65
LOGO_CENTER_Y = 0.11
GLASS_WIDTH = 0.018
GAP_RATIO = 0.0135


def make_rim(size, rim, inset, radius, target):
    """Static lighting only. FFmpeg supplies the moving reflection beneath it."""
    w, h = size
    layer = Image.new("RGBA", size, (0, 0, 0, 255))
    pen = ImageDraw.Draw(layer)

    def box(d):
        return (d, d, w - 1 - d, h - 1 - d)

    for d in range(rim):
        t = (d + 0.5) / rim
        outer_light = math.exp(-((t - 0.18) / 0.045) ** 2)
        inner_light = math.exp(-((t - 0.88) / 0.045) ** 2)
        shadow = math.exp(-((t - 0.60) / 0.13) ** 2)
        light = 0.42 * outer_light + 0.51 * inner_light - 0.06 * shadow
        tone = 255 if light > 0 else 0
        pen.rounded_rectangle(box(d), radius=max(1, radius-d),
                              fill=(tone, tone, tone, round(255 * abs(light))))
    pen.rounded_rectangle(box(rim), radius=max(1, radius-rim), fill="black")
    pen.rounded_rectangle(box(inset), radius=max(1, radius-inset), fill=(0, 0, 0, 0))

    # Elongated glints on opposite edges give the rounded bevel directional light.
    sheen = Image.new("L", size)
    draw = ImageDraw.Draw(sheen)
    for fraction, edge in [(0.19, 0), (0.77, h - 1)]:
        for step in range(36, 0, -1):
            f = step / 36
            x = round(w * fraction)
            y = round(rim * 0.22) if edge == 0 else edge - round(rim * 0.22)
            dx, dy = round(w * 0.12 * f), max(1, round(rim * 0.18 * f))
            draw.ellipse((x-dx, y-dy, x+dx, y+dy), fill=round(180*(1-f)**2))
    glints = Image.new("RGBA", size, "white")
    glints.putalpha(ImageChops.multiply(sheen, layer.getchannel("A").point(
        lambda alpha: 255 if 0 < alpha < 255 else 0)))
    Image.alpha_composite(layer, glints).save(target)


def inspect_video(source: Path):
    local = ["-protocol_whitelist", "file", "-format_whitelist", FORMATS]
    info = json.loads(run(["ffprobe", "-v", "error", *local, "-show_streams",
                           "-show_format", "-of", "json", str(source)]))
    video = next((s for s in info["streams"] if s["codec_type"] == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if not video:
        raise MediaError("Please send a file containing a video track.")
    w, h = video["width"], video["height"]
    rotation = next((s["rotation"] for s in video.get("side_data_list", [])
                     if "rotation" in s), 0)
    if abs(rotation) % 180 == 90:
        w, h = h, w
    if min(w, h) < 8 or w*h > 4096*2160:
        raise MediaError("Please send a video up to 4K resolution.")
    if video.get("color_transfer") in {"smpte2084", "arib-std-b67"}:
        raise MediaError("Please send an SDR export so the original colours are preserved.")
    return info, video, w, h


@lru_cache(maxsize=1)
def supports_nvenc():
    # An FFmpeg build may list NVENC even on a machine without a working GPU.
    probe = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
         "color=size=256x256", "-frames:v", "1", "-c:v", "h264_nvenc",
         "-preset", "p1", "-f", "null", "-"],
        capture_output=True, timeout=20)
    return probe.returncode == 0


LOGO_FORMATS = "png_pipe,jpeg_pipe,gif,mov"


def inspect_logo(path):
    info = json.loads(run(["ffprobe", "-v", "error", "-protocol_whitelist", "file",
                           "-format_whitelist", LOGO_FORMATS, "-show_format",
                           "-show_streams", "-of", "json", str(path)]))
    stream = next((s for s in info["streams"] if s["codec_type"] == "video"
                   and not s.get("disposition", {}).get("attached_pic")), None)
    if not stream or stream["width"] * stream["height"] > 4096 * 2160:
        raise MediaError("Send a PNG, JPEG, GIF or MP4 logo up to 4K.")
    return stream, info["format"]["format_name"] in {"png_pipe", "jpeg_pipe"}


def logo_filters(path, index):
    """Inspect small RGBA previews, keeping a single crop for the whole animation."""
    from statistics import median
    from PIL import ImageFilter
    side = 96
    args = ["ffmpeg", "-v", "error", "-protocol_whitelist", "file", "-format_whitelist",
            LOGO_FORMATS, "-i", str(path), "-map", f"0:{index}", "-vf",
            "select='isnan(prev_selected_t)+gte(t-prev_selected_t,0.125)',scale=96:96",
            "-fps_mode", "vfr", "-frames:v", "960", "-pix_fmt", "rgba", "-f", "rawvideo", "-"]
    raw = run(args)
    stride = side * side * 4
    if not raw:
        raise MediaError("The logo contains no readable frames.")
    frames = [Image.frombytes("RGBA", (side, side), raw[i:i+stride])
              for i in range(0, len(raw), stride)]
    edges = []
    for frame in frames[::max(1, len(frames)//12)]:
        for n in range(2, side-2, 8):
            edges.extend(frame.getpixel(xy) for xy in [(n,1),(n,side-2),(1,n),(side-2,n)])
    greens = [(r,g,b) for r,g,b,a in edges if a > 240 and g > max(r,b)+45]
    key = None
    prefix = ""
    if len(greens) > len(edges)*0.70:
        color = tuple(round(median(channel)) for channel in zip(*greens))
        if sum(max(abs(v-c) for v,c in zip(pixel,color)) < 25 for pixel in greens) > len(edges)*0.65:
            key = color
            prefix = "colorkey=0x%02x%02x%02x:0.12:0.035," % key
    union = Image.new("L", (side, side))
    for frame in frames:
        mask = frame.getchannel("A").point(lambda a: 255 if a > 32 else 0)
        if key:
            diff = ImageChops.difference(frame.convert("RGB"), Image.new("RGB",frame.size,key))
            r,g,b = diff.split()
            solid = ImageChops.lighter(ImageChops.lighter(r,g),b).point(lambda d: 255 if d > 42 else 0)
            mask = ImageChops.multiply(mask, solid)
        union = ImageChops.lighter(union, mask)
    box = union.filter(ImageFilter.MaxFilter(5)).getbbox()
    if box and box != (0,0,side,side) and len(frames) < 960:
        l,t,r,b = box
        prefix += f"crop=ceil(iw*{r-l}/96):ceil(ih*{b-t}/96):floor(iw*{l}/96):floor(ih*{t}/96),"
        return prefix, (r-l)/side, (b-t)/side
    return prefix, 1, 1


def prepare_logo(path):
    stream, still = inspect_logo(path)
    prefix, x_fraction, y_fraction = logo_filters(path, stream["index"])
    w, h = stream["width"], stream["height"]
    rotation = next((x["rotation"] for x in stream.get("side_data_list", [])
                     if "rotation" in x), 0)
    if abs(rotation) % 180 == 90:
        w, h = h, w
    return stream["index"], still, prefix, math.ceil(w*x_fraction), math.ceil(h*y_fraction)


def render(source: Path, output: Path, background: Path | None = None, logo: Path | None = None, logo_details=None):
    info, video, w, h = inspect_video(source)
    local = ["-protocol_whitelist", "file", "-format_whitelist", FORMATS]
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    logo_only = logo is not None and background is None
    mask_input = []
    if logo_only:
        graph = f"[0:{video['index']}]null[v]"
    else:
        mask = output.with_suffix(".png")
        short_side = min(w, h)
        rim = max(2, round(short_side * GLASS_WIDTH))
        gap = max(1, round(short_side * GAP_RATIO))
        inset = rim + gap
        width, height = w + 2 * inset, h + 2 * inset
        radius = inset + max(1, round(short_side * 0.047))
        make_rim((width, height), rim, inset, radius, mask)
        # Reflect each live edge into the added margin, including both axes at corners.
        graph = (f"[0:{video['index']}]format=yuv444p,pad={width}:{height}:{inset}:{inset},"
                 f"fillborders=left={inset}:right={inset}:top={inset}:bottom={inset}:mode=mirror[reflected];"
                 "[reflected][1:v]overlay=0:0:format=yuv444,"
                 "pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p[v]")
        mask_input = ["-i", str(mask)]
    extra_inputs = []
    if logo is not None:
        index, still, prep, _, _ = logo_details or prepare_logo(logo)
    if background is not None:
        _, bg, bw, bh = inspect_video(background)
        # Phone templates use square display pixels. Normalize anamorphic backgrounds.
        sar = bg.get("sample_aspect_ratio", "1:1")
        if sar and ":" in sar:
            n, d = map(int, sar.split(":"))
            if n > 0 and d > 0:
                bw = round(bw * n / d)
        bw, bh = max(2, bw // 2 * 2), max(2, bh // 2 * 2)
        if bw * bh > 4096 * 2160:
            raise MediaError("The background display resolution exceeds 4K.")
        # Sample header ends near 19%. Keep a little breathing room below it.
        top = 2 * round(bh * 0.23 / 2)
        box_w = max(2, 2 * int(bw * 0.90 / 2))
        box_h = max(2, 2 * int((bh * 0.72) / 2))
        main_seconds = float(video.get("duration") or info["format"]["duration"])
        if not math.isfinite(main_seconds) or main_seconds <= 0:
            raise MediaError("The main video's duration could not be read.")
        extra_inputs = ["-stream_loop", "-1", "-t", str(main_seconds + 1), *local, "-threads", "2", "-i", str(background)]
        graph += (f";[v]scale={box_w}:{box_h}:force_original_aspect_ratio=decrease:"
                  "force_divisible_by=2,setsar=1")
        rate = video.get("avg_frame_rate")
        if (rate and rate != "0/0" and rate == video.get("r_frame_rate")
                and float(video.get("start_time") or 0) == 0):
            graph += (f"[front];[2:{bg['index']}]setpts=PTS-STARTPTS,fps={rate},"
                      f"scale={bw}:{bh},setsar=1[scene];")
        else:
            # Use the foreground clock to retain variable frame timestamps.
            graph += (f",split[clock][front];[clock]pad={bw}:{bh}:0:0[canvas];"
                      f"[2:{bg['index']}]scale={bw}:{bh},setsar=1,setpts=PTS-STARTPTS[bg];"
                      "[canvas][bg]overlay=0:0:shortest=1[scene];")
        graph += f"[scene][front]overlay=(W-w)/2:{top}:shortest=1[composite]"
    video_label = "[composite]" if background is not None else "[v]"
    if logo is not None:
        input_number = 3 if background is not None else 1
        if not still:
            seconds = float(video.get("duration") or info["format"]["duration"])
            extra_inputs += ["-stream_loop", "-1", "-t", str(seconds + 1)]
        extra_inputs += ["-protocol_whitelist", "file", "-format_whitelist", LOGO_FORMATS,
                         "-i", str(logo)]
        eof = "" if still else ":shortest=1"
        overlay_format = ":format=yuv444" if logo_only and (w % 2 or h % 2) else ""
        canvas_w, canvas_h = (w, h) if logo_only else (bw, bh)
        max_w = max(2, round(canvas_w * LOGO_WIDTH_FRACTION))
        max_h = max(2, round(canvas_h * 2 * LOGO_CENTER_Y))
        fit = (f"scale={max_w}:{max_h}:force_original_aspect_ratio=decrease:"
               "flags=lanczos,")
        y_position = f"max(0,{LOGO_CENTER_Y}*H-h/2)"
        graph += (f";[{input_number}:{index}]format=rgba,{prep}{fit}setsar=1,"
                  f"setpts=PTS-STARTPTS[mark];{video_label}[mark]overlay="
                  f"(W-w)/2:'{y_position}'{eof}{overlay_format}[branded]")
        video_label = "[branded]"
    base = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-filter_complex_threads", "3",
            *local, "-threads", "2", "-i", str(source), *mask_input,
            *extra_inputs, "-filter_complex", graph, "-map", video_label]
    if audio:
        base += ["-map", f"0:{audio['index']}"]
    base += ["-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1",
             "-fps_mode", "vfr", "-movflags", "+faststart"]
    audio_opts = ["-c:a", "copy"] if audio and audio["codec_name"] == "aac" else [
        "-c:a", "aac", "-b:a", "160k"]
    cpu = ["-c:v", "libx264", "-preset", "veryfast", "-threads",
           str(min(8, os.cpu_count() or 2))]
    limit = ["-fs", str(MAX_OUTPUT + 1_000_000), str(output)]
    encoder = cpu
    if supports_nvenc():
        encoder = ["-c:v", "h264_nvenc", "-preset", "p1"]
        try:
            run(base + encoder + ["-rc", "vbr", "-cq", "18", "-b:v", "0"] + audio_opts + limit)
        except MediaError:
            # Some dimensions or driver configurations are CPU-only.
            encoder = cpu
    if encoder == cpu:
        run(base + cpu + ["-crf", "17"] + audio_opts + limit)
    if output.stat().st_size <= MAX_OUTPUT:
        return
    duration = float(info["format"].get("duration", 0))
    if duration <= 0 or not math.isfinite(duration):
        raise MediaError("The output exceeds Telegram's upload limit.")
    bitrate = int(MAX_OUTPUT * 8 * 0.90 / duration) - (128_000 if audio else 0)
    if bitrate < 80_000:
        raise MediaError("Please send a shorter clip to fit Telegram's upload limit.")
    run(base + cpu + ["-b:v", str(bitrate), "-maxrate", str(bitrate),
                "-bufsize", str(bitrate*2), "-c:a", "aac", "-b:a", "128k",
                "-fs", str(MAX_OUTPUT+1_000_000), str(output)])
    if output.stat().st_size > MAX_OUTPUT:
        raise MediaError("The result is still too large. Please send a shorter clip.")
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

from media import MediaError, inspect_video, render, prepare_logo

ROOT = Path(tempfile.gettempdir()) / "telegram-glass-video-jobs"
LIMIT = 20 * 1024 * 1024
HELP_BUTTON = "How to use"
SET_BG = "Set background"
REMOVE_BG = "Remove background"
KEYBOARD = ReplyKeyboardMarkup(
    [[SET_BG], ["Logo mode", "Background + logo"], ["Change logo", "Logo off"],
     [HELP_BUTTON, "/cancel"]],
    resize_keyboard=True, is_persistent=True,
)
GUIDE = (
    "How to use\n\n"
    "1. Attach your original video (up to 20 MB).\n"
    "2. Send up to ten videos together, individually or as an album.\n"
    "3. Save the finished MP4 from this chat.\n\n"
    "Optional: tap Set background and send a profile clip. Future videos will appear below its header. "
    "Tap Set background again to remove your saved background. Use /cancel to cancel setup.\n\n"
    "Logo mode places your logo in the upper part of the original video. Background + logo uses both saved assets. "
    "Use Change logo to replace it, or Logo off to return to your previous layout.\n\n"
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
        "Send a video to add a reflective glass frame. Tap the keyboard icon beside the message field to see all options.",
        reply_markup=KEYBOARD)


async def show_commands(update, context):
    await update.effective_message.reply_text(
        "Choose a command below. Set background toggles your saved background. "
        "Use /cancel to exit setup.", reply_markup=KEYBOARD)


async def show_help(update: Update, context):
    await update.effective_message.reply_text(GUIDE, reply_markup=KEYBOARD)


async def logo_mode(update, context):
    user = update.effective_user.id
    db = context.bot_data["database"]
    text = update.effective_message.text
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
            mode = row[0]
        context.user_data["awaiting_logo"] = (update.effective_chat.id, mode)
        await update.effective_message.reply_text("Send a PNG, JPEG, animated GIF or MP4 logo (up to 20 MB). "
                                                 "For transparency, send a PNG or GIF as a file. Use /cancel to exit.", reply_markup=KEYBOARD)
        return
    with db:
        db.execute("UPDATE logos SET mode=? WHERE user_id=?", (mode, user))
    await update.effective_message.reply_text(f"{text} enabled. Send your main video.", reply_markup=KEYBOARD)


async def choose_background(update: Update, context):
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
    context.user_data.pop("awaiting_background", None)
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


async def process(update: Update, context):
    message = update.effective_message
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
                details = await asyncio.to_thread(prepare_logo, path)
                cached_logo = (item.file_id, path.read_bytes(), details)
            with db:
                db.execute("INSERT OR REPLACE INTO logos VALUES (?, ?, ?)", (user_id, item.file_id, setup[1]))
            context.bot_data["recent_logo"] = cached_logo
            context.user_data.pop("awaiting_logo", None)
            await message.reply_text("Logo saved.", reply_markup=KEYBOARD)
        except (MediaError, TelegramError):
            await message.reply_text("Could not load that logo. Send a PNG, JPEG, GIF or MP4, or use /cancel.", reply_markup=KEYBOARD)
        return
    logo = db.execute("SELECT file_id, mode FROM logos WHERE user_id=? AND mode!='off'", (user_id,)).fetchone()
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
                    details = cached[2]
                else:
                    mark = await context.bot.get_file(logo[0])
                    await mark.download_to_drive(logo_path, read_timeout=120)
                    details = await asyncio.to_thread(prepare_logo, logo_path)
                    context.bot_data["recent_logo"] = (logo[0], logo_path.read_bytes(), details)
            await asyncio.to_thread(render, source, result, background, logo_path, details)
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
        app.bot_data["database"] = db
        try:
            app.run_polling(allowed_updates=["message"])
        except BadRequest:
            raise
        except NetworkError:
            raise SystemExit("Could not connect to Telegram. Check connectivity to api.telegram.org and restart.") from None


if __name__ == "__main__":
    main()
