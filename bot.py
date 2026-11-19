import os
import re
import time
import shutil
import asyncio
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Thread, Lock as ThreadLock
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from telegram.error import Conflict

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing in .env")

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

user_bg = {}
user_circle = {}
user_locks = {}

MAX_DURATION = 60.0
# Railway-safe: only 1 heavy encode at a time (prevents OOM / disk thrash)
ENCODE_SLOTS = 1
_encode_sem = None
_executor = ThreadPoolExecutor(max_workers=2)
_ffmpeg_path = None
_ffmpeg_lock = ThreadLock()


def get_ffmpeg() -> str:
    global _ffmpeg_path
    with _ffmpeg_lock:
        if _ffmpeg_path:
            return _ffmpeg_path
        if shutil.which("ffmpeg"):
            _ffmpeg_path = "ffmpeg"
            return _ffmpeg_path
        if imageio_ffmpeg is not None:
            try:
                path = imageio_ffmpeg.get_ffmpeg_exe()
                if path and os.path.exists(path):
                    _ffmpeg_path = path
                    return _ffmpeg_path
            except Exception as e:
                print(f"imageio-ffmpeg error: {e}", flush=True)
        return ""


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 8080))

    def run():
        try:
            srv = HTTPServer(("0.0.0.0", port), HealthHandler)
            print(f"Health server on {port}", flush=True)
            srv.serve_forever()
        except Exception as e:
            print(f"Health error: {e}", flush=True)

    Thread(target=run, daemon=True).start()


def get_lock(uid: int) -> asyncio.Lock:
    if uid not in user_locks:
        user_locks[uid] = asyncio.Lock()
    return user_locks[uid]


def kb():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("Reset Background"), KeyboardButton("Circle Mode")],
            [KeyboardButton("Help")],
        ],
        resize_keyboard=True,
    )


def bg_path(uid: int, ext: str) -> str:
    return os.path.join(DATA_DIR, f"bg_{uid}{ext}")


def get_bg(uid: int):
    for ext in [".mp4", ".gif", ".png", ".jpg", ".jpeg", ".webp"]:
        p = bg_path(uid, ext)
        if os.path.exists(p) and os.path.getsize(p) > 100:
            return p
    return None


def clear_bg(uid: int):
    for ext in [".mp4", ".gif", ".png", ".jpg", ".jpeg", ".webp"]:
        p = bg_path(uid, ext)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
    user_bg.pop(uid, None)


def save_bg(uid: int, src: str) -> str:
    clear_bg(uid)
    ext = os.path.splitext(src)[1].lower() or ".mp4"
    if ext not in [".mp4", ".gif", ".png", ".jpg", ".jpeg", ".webp"]:
        ext = ".mp4"
    dest = bg_path(uid, ext)
    shutil.copy(src, dest)
    user_bg[uid] = dest
    return dest


def is_circle(uid: int) -> bool:
    return bool(user_circle.get(uid, False))


def set_circle(uid: int, on: bool):
    user_circle[uid] = on


def cleanup(*paths):
    for p in paths:
        if not p:
            continue
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def probe_duration(path: str, ff: str) -> float:
    probes = ["ffprobe"]
    if ff and ff != "ffmpeg":
        near = os.path.join(os.path.dirname(ff), "ffprobe")
        if os.path.exists(near):
            probes.insert(0, near)
    for probe in probes:
        try:
            r = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15,
            )
            if r.returncode == 0 and r.stdout.strip():
                d = float(r.stdout.strip())
                if d > 0.3:
                    return d
        except Exception:
            pass
    try:
        r = subprocess.run(
            [ff, "-i", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr or "")
        if m:
            d = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            if d > 0.3:
                return d
    except Exception:
        pass
    return 8.0


def run_ff(cmd, timeout=900):
    try:
        return subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, text=True,
        )
    except subprocess.TimeoutExpired:
        print("ffmpeg timeout", flush=True)
        return None


def compose(bg_src: str, main_src: str, output_path: str, circle: bool = False) -> bool:
    """
    Fast reliable overlay:
    - isolated temp dir (no cross-job cleanup races)
    - 2 ffmpeg steps max
    - adaptive quality for long clips (up to 60s)
    - white border (stable on Railway)
    """
    ff = get_ffmpeg()
    if not ff:
        print("ERROR: no ffmpeg", flush=True)
        return False
    if not os.path.exists(bg_src) or not os.path.exists(main_src):
        return False

    raw = probe_duration(main_src, ff)
    duration = min(max(raw, 0.5), MAX_DURATION)
    print(f"compose raw={raw:.1f} use={duration:.1f} circle={circle}", flush=True)

    # adaptive quality for speed + 60s stability
    if duration <= 12:
        fps, crf, scale_w = 30, 20, 720
    elif duration <= 30:
        fps, crf, scale_w = 25, 23, 680
    else:
        fps, crf, scale_w = 24, 26, 640

    W, H = 1080, 1920
    border = 12
    if circle:
        inner = min(scale_w, 640)
        mw = mh = inner + border * 2
        inner_w = inner_h = inner
    else:
        inner_w = scale_w
        inner_h = int(scale_w * 1244 / 700)
        # even dims
        inner_h -= inner_h % 2
        mw = inner_w + border * 2
        mh = inner_h + border * 2

    work = tempfile.mkdtemp(prefix="ov_")
    bg_mp4 = os.path.join(work, "bg.mp4")
    out_tmp = os.path.join(work, "out.mp4")
    tlim = max(240, int(duration * 8) + 60)

    try:
        bg_ext = os.path.splitext(bg_src)[1].lower()
        is_image = bg_ext in [".png", ".jpg", ".jpeg", ".webp"]

        # --- step 1: background ---
        if is_image:
            cmd_bg = [
                ff, "-y", "-loop", "1", "-i", bg_src,
                "-t", f"{duration:.2f}",
                "-vf", (
                    f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{H},setsar=1,fps={fps},format=yuv420p"
                ),
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-pix_fmt", "yuv420p", "-an", bg_mp4,
            ]
        else:
            cmd_bg = [
                ff, "-y", "-stream_loop", "-1", "-i", bg_src,
                "-t", f"{duration:.2f}",
                "-vf", (
                    f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{H},setsar=1,fps={fps},format=yuv420p"
                ),
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-pix_fmt", "yuv420p", "-an", bg_mp4,
            ]
        r = run_ff(cmd_bg, timeout=tlim)
        if not r or r.returncode != 0 or not os.path.exists(bg_mp4) or os.path.getsize(bg_mp4) < 1500:
            print("bg fail:", (r.stderr if r else "")[-400:], flush=True)
            return False

        # --- step 2: main scale+border + overlay + audio in ONE graph ---
        if circle:
            main_chain = (
                f"[1:v]scale={inner_w}:{inner_h}:force_original_aspect_ratio=increase,"
                f"crop={inner_w}:{inner_h},"
                f"pad={mw}:{mh}:{border}:{border}:color=white,"
                f"setsar=1,fps={fps},format=yuv420p[fg];"
            )
        else:
            main_chain = (
                f"[1:v]scale={inner_w}:{inner_h}:force_original_aspect_ratio=decrease,"
                f"pad={inner_w}:{inner_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"pad={mw}:{mh}:{border}:{border}:color=white,"
                f"setsar=1,fps={fps},format=yuv420p[fg];"
            )

        fc = (
            f"{main_chain}"
            f"[0:v][fg]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
        )

        r = run_ff([
            ff, "-y",
            "-i", bg_mp4,
            "-i", main_src,
            "-filter_complex", fc,
            "-map", "[v]",
            "-map", "1:a?",
            "-t", f"{duration:.2f}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", str(crf),
            "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-ac", "2",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            out_tmp,
        ], timeout=max(300, int(duration * 10)))

        if not r or r.returncode != 0 or not os.path.exists(out_tmp) or os.path.getsize(out_tmp) < 3000:
            print("overlay fail, retry no-audio:", (r.stderr if r else "")[-400:], flush=True)
            r = run_ff([
                ff, "-y",
                "-i", bg_mp4,
                "-i", main_src,
                "-filter_complex", fc,
                "-map", "[v]",
                "-t", f"{duration:.2f}",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", str(min(crf + 2, 28)),
                "-an",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                out_tmp,
            ], timeout=max(280, int(duration * 9)))
            if not r or r.returncode != 0 or not os.path.exists(out_tmp) or os.path.getsize(out_tmp) < 3000:
                print("overlay fail final:", (r.stderr if r else "")[-400:], flush=True)
                return False

        # compress if too big for Telegram send
        size = os.path.getsize(out_tmp)
        if size > 40 * 1024 * 1024:
            comp = os.path.join(work, "comp.mp4")
            r2 = run_ff([
                ff, "-y", "-i", out_tmp,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-c:a", "aac", "-b:a", "80k",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                comp,
            ], timeout=max(180, int(duration * 6)))
            if r2 and r2.returncode == 0 and os.path.exists(comp) and os.path.getsize(comp) > 3000:
                out_tmp = comp

        shutil.copy(out_tmp, output_path)
        print(f"OK {os.path.getsize(output_path)} bytes", flush=True)
        return True
    except Exception as e:
        print(f"compose error: {e}", flush=True)
        return False
    finally:
        cleanup(work)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Overlay Bot\n\n"
        "1. Send image / GIF / short video → background\n"
        "2. Send videos → overlay + white border (queue safe)\n"
        "3. Circle Mode / Reset Background\n\n"
        "Up to 60s. Bulk videos OK (processed one-by-one, no drop).\n"
        "DM only. Telegram download ~20 MB max per file.",
        reply_markup=kb(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    bg = get_bg(uid)
    mode = "Circle" if is_circle(uid) else "Rectangle"
    await update.message.reply_text(
        "How to use\n\n"
        f"Background: {'set' if bg else 'not set'}\n"
        f"Shape: {mode}\n\n"
        "Send media for background, then videos to overlay.\n"
        "You can send multiple videos — they queue safely.",
        reply_markup=kb(),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if update.message.chat.type != "private":
        return
    text = update.message.text.strip()
    uid = update.effective_user.id

    if text == "Reset Background":
        clear_bg(uid)
        await update.message.reply_text(
            "Background cleared.\nSend a new image, GIF, or video.",
            reply_markup=kb(),
        )
        return

    if text == "Circle Mode":
        on = not is_circle(uid)
        set_circle(uid, on)
        await update.message.reply_text(
            "Circle Mode ON" if on else "Circle Mode OFF (Rectangle)",
            reply_markup=kb(),
        )
        return

    if text == "Help":
        await help_cmd(update, context)
        return

    await update.message.reply_text(
        "Send image/GIF/video for background, or videos to overlay.",
        reply_markup=kb(),
    )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _encode_sem
    message = update.message
    if not message or message.chat.type != "private":
        return

    uid = update.effective_user.id
    lock = get_lock(uid)

    async with lock:
        status = await message.reply_text("Queued...")
        stamp = str(int(time.time() * 1000)) + f"_{uid}"
        input_path = None
        output_path = None

        try:
            prefer_bg = False
            ext = ".mp4"
            file_obj = None

            if message.photo:
                file_obj = message.photo[-1]
                prefer_bg = True
                ext = ".jpg"
            elif message.animation:
                file_obj = message.animation
                prefer_bg = True
                ext = ".mp4"
            elif message.video:
                file_obj = message.video
                ext = ".mp4"
            elif message.document:
                file_obj = message.document
                mime = (message.document.mime_type or "").lower()
                name = (message.document.file_name or "").lower()
                if "image" in mime or name.endswith((".jpg", ".jpeg", ".png", ".webp")):
                    prefer_bg = True
                    ext = ".jpg"
                elif "gif" in mime or name.endswith(".gif"):
                    prefer_bg = True
                    ext = ".mp4"
                elif "video" in mime or name.endswith((".mp4", ".mov", ".mkv", ".webm")):
                    ext = ".mp4"
                else:
                    await status.edit_text("Unsupported file.")
                    return
            else:
                await status.edit_text("Unsupported media.")
                return

            size = getattr(file_obj, "file_size", None) or 0
            if size > 50 * 1024 * 1024:
                await status.edit_text("File too large. Keep under ~20 MB for Telegram bots.")
                return

            input_path = f"in_{stamp}{ext}"
            try:
                tg = await context.bot.get_file(file_obj.file_id)
                await tg.download_to_drive(input_path)
            except Exception as e:
                es = str(e).lower()
                if "too big" in es or "too large" in es:
                    await status.edit_text("Telegram ~20 MB limit. Compress and retry.")
                else:
                    await status.edit_text(f"Download failed: {str(e)[:100]}")
                return

            bg = get_bg(uid)

            if prefer_bg or bg is None:
                save_bg(uid, input_path)
                await status.edit_text(
                    "Background saved.\nSend videos to overlay (bulk OK)."
                )
                return

            await status.edit_text("Processing...")

            if _encode_sem is None:
                _encode_sem = asyncio.Semaphore(ENCODE_SLOTS)

            output_path = f"out_{stamp}.mp4"
            circle = is_circle(uid)
            loop = asyncio.get_event_loop()

            async with _encode_sem:
                ok = await loop.run_in_executor(
                    _executor, compose, bg, input_path, output_path, circle
                )

            if not ok or not os.path.exists(output_path):
                await status.edit_text("Overlay failed. Retry once — if long, compress a bit.")
                return

            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            with open(output_path, "rb") as f:
                if size_mb > 48:
                    await message.reply_document(
                        document=f, caption="Done", filename="overlay.mp4",
                        read_timeout=300, write_timeout=300,
                    )
                else:
                    await message.reply_video(
                        video=f, caption="Done", supports_streaming=True,
                        read_timeout=300, write_timeout=300,
                    )
            try:
                await status.delete()
            except Exception:
                pass

        except Exception as e:
            print(f"handle_media: {e}", flush=True)
            try:
                await status.edit_text(f"Failed: {str(e)[:120]}")
            except Exception:
                pass
        finally:
            cleanup(input_path, output_path)


def main():
    start_health_server()
    ff = get_ffmpeg()
    if not ff:
        print("WARNING: ffmpeg missing", flush=True)
    else:
        print(f"Using ffmpeg: {ff}", flush=True)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(60.0)
        .read_timeout(300.0)
        .write_timeout(300.0)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL,
            handle_media,
        )
    )
    print("Overlay bot started", flush=True)

    while True:
        try:
            app.run_polling(drop_pending_updates=True, close_loop=False)
        except Conflict:
            print("Conflict: another instance. Wait 15s...", flush=True)
            time.sleep(15)
        except Exception as e:
            print(f"polling error: {e}", flush=True)
            time.sleep(8)


if __name__ == "__main__":
    main()
