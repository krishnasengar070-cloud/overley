import os
import re
import time
import shutil
import asyncio
import subprocess
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

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


def get_ffmpeg() -> str:
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    if imageio_ffmpeg is not None:
        try:
            path = imageio_ffmpeg.get_ffmpeg_exe()
            if path and os.path.exists(path):
                return path
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
            print(f"Health server listening on {port}", flush=True)
            srv.serve_forever()
        except Exception as e:
            print(f"Health server error: {e}", flush=True)

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
        if p and os.path.exists(p):
            try:
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
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=25,
            )
            if r.returncode == 0 and r.stdout.strip():
                d = float(r.stdout.strip())
                if d > 0.4:
                    return d
        except Exception:
            pass

    try:
        r = subprocess.run(
            [ff, "-i", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=25,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr or "")
        if m:
            d = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            if d > 0.4:
                return d
    except Exception:
        pass

    return 10.0


def run_ff(cmd, timeout=1200):
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, text=True
    )


def compose(bg_path_in: str, main_path: str, output_path: str, circle: bool = False) -> bool:
    ff = get_ffmpeg()
    if not ff:
        print("ERROR: ffmpeg not found", flush=True)
        return False
    if not os.path.exists(bg_path_in) or not os.path.exists(main_path):
        print("missing input files", flush=True)
        return False

    raw = probe_duration(main_path, ff)
    duration = min(max(raw, 0.5), MAX_DURATION)
    print(f"compose raw={raw:.1f}s use={duration:.1f}s circle={circle}", flush=True)

    # quality settings
    if duration <= 12:
        crf = 18
    elif duration <= 30:
        crf = 22
    else:
        crf = 25

    uid = str(int(time.time() * 1000))
    bg_mp4 = f"tmp_bg_{uid}.mp4"
    main_prep = f"tmp_main_{uid}.mp4"
    out_tmp = f"tmp_out_{uid}.mp4"
    W, H = 1080, 1920
    border = 16

    if circle:
        inner = 680
        mw = mh = inner + border * 2
    else:
        inner_w, inner_h = 700, 1244
        mw = inner_w + border * 2
        mh = inner_h + border * 2

    try:
        bg_ext = os.path.splitext(bg_path_in)[1].lower()
        is_image = bg_ext in [".png", ".jpg", ".jpeg", ".webp"]
        tlim = max(400, int(duration * 12) + 180)

        # ========== 1. BACKGROUND ==========
        if is_image:
            cmd_bg = [
                ff, "-y", "-loop", "1", "-i", bg_path_in,
                "-t", f"{duration:.2f}",
                "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps=30,format=yuv420p",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-pix_fmt", "yuv420p", "-an", bg_mp4,
            ]
        else:
            cmd_bg = [
                ff, "-y", "-stream_loop", "-1", "-i", bg_path_in,
                "-t", f"{duration:.2f}",
                "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps=30,format=yuv420p",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-pix_fmt", "yuv420p", "-an", bg_mp4,
            ]

        r = run_ff(cmd_bg, timeout=tlim)
        if r.returncode != 0 or not os.path.exists(bg_mp4) or os.path.getsize(bg_mp4) < 2000:
            print("BG FAIL:", (r.stderr or "")[-800:], flush=True)
            return False
        print("bg ok", flush=True)

        # ========== 2. MAIN + WHITE BORDER ==========
        if circle:
            vf_main = (
                f"scale={inner}:{inner}:force_original_aspect_ratio=increase,"
                f"crop={inner}:{inner},"
                f"pad={mw}:{mh}:{border}:{border}:color=white,"
                f"setsar=1,fps=30,format=yuv420p"
            )
        else:
            vf_main = (
                f"scale={inner_w}:{inner_h}:force_original_aspect_ratio=decrease,"
                f"pad={inner_w}:{inner_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"pad={mw}:{mh}:{border}:{border}:color=white,"
                f"setsar=1,fps=30,format=yuv420p"
            )

        r = run_ff([
            ff, "-y", "-i", main_path,
            "-t", f"{duration:.2f}",
            "-vf", vf_main,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-an", main_prep,
        ], timeout=tlim)

        if r.returncode != 0 or not os.path.exists(main_prep) or os.path.getsize(main_prep) < 2000:
            print("MAIN FAIL:", (r.stderr or "")[-800:], flush=True)
            return False
        print("main ok", flush=True)

        # ========== 3. SIMPLE OVERLAY (video only first) ==========
        # This is the most reliable way on limited Railway ffmpeg
        r = run_ff([
            ff, "-y",
            "-i", bg_mp4,
            "-i", main_prep,
            "-filter_complex", "[0:v][1:v]overlay=(W-w)/2:(H-h)/2[v]",
            "-map", "[v]",
            "-t", f"{duration:.2f}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", str(crf),
            "-an",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            out_tmp,
        ], timeout=max(500, int(duration * 15)))

        if r.returncode != 0 or not os.path.exists(out_tmp) or os.path.getsize(out_tmp) < 3000:
            print("OVERLAY FAIL:", (r.stderr or "")[-800:], flush=True)
            return False
        print("overlay ok", flush=True)

        # ========== 4. ADD AUDIO (optional second pass) ==========
        final = f"tmp_final_{uid}.mp4"
        r_audio = run_ff([
            ff, "-y",
            "-i", out_tmp,
            "-i", main_path,
            "-c:v", "copy",
            "-map", "0:v:0",
            "-map", "1:a:0?",
            "-c:a", "aac", "-b:a", "96k",
            "-shortest",
            "-movflags", "+faststart",
            final,
        ], timeout=max(300, int(duration * 8)))

        if r_audio.returncode == 0 and os.path.exists(final) and os.path.getsize(final) > 3000:
            cleanup(out_tmp)
            out_tmp = final
            print("audio added", flush=True)
        else:
            print("audio skip (using video only)", flush=True)
            # keep out_tmp as is (no audio)

        # ========== 5. FINAL COMPRESS IF NEEDED ==========
        size = os.path.getsize(out_tmp)
        if size > 40 * 1024 * 1024 or duration > 25:
            print(f"compressing {size // (1024*1024)} MB ...", flush=True)
            comp = f"tmp_comp_{uid}.mp4"
            r3 = run_ff([
                ff, "-y", "-i", out_tmp,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "27",
                "-c:a", "aac", "-b:a", "80k",
                "-movflags", "+faststart",
                "-pix_fmt", "yuv420p",
                comp,
            ], timeout=max(300, int(duration * 10)))
            if r3.returncode == 0 and os.path.exists(comp) and os.path.getsize(comp) > 3000:
                cleanup(out_tmp)
                out_tmp = comp
                print("compressed", flush=True)

        shutil.move(out_tmp, output_path)
        print(f"SUCCESS {os.path.getsize(output_path)} bytes", flush=True)
        return True

    except subprocess.TimeoutExpired:
        print("TIMEOUT", flush=True)
        return False
    except Exception as e:
        print(f"compose error: {e}", flush=True)
        return False
    finally:
        cleanup(bg_mp4, main_prep, out_tmp)
        for f in list(os.listdir(".")):
            if uid in f and f.startswith("tmp_"):
                cleanup(f)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Overlay Bot\n\n"
        "1. Send image / GIF / short video → background\n"
        "2. Send any video → overlay + white border\n"
        "3. Circle Mode / Reset Background\n\n"
        "Supports up to 60 seconds.\n"
        "Long clips are auto-compressed.\n"
        "DM only.",
        reply_markup=kb(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    bg = get_bg(uid)
    mode = "Circle" if is_circle(uid) else "Rectangle"
    status = "Background set" if bg else "No background yet"
    await update.message.reply_text(
        "How to use\n\n"
        f"Status: {status}\n"
        f"Shape: {mode}\n\n"
        "Send media for background, then a video to overlay.\n"
        "Max 60 seconds supported.",
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
        msg = "Circle Mode ON" if on else "Circle Mode OFF (Rectangle)"
        await update.message.reply_text(msg, reply_markup=kb())
        return

    if text == "Help":
        await help_cmd(update, context)
        return

    await update.message.reply_text(
        "Send image/GIF/video for background, or a video to overlay.",
        reply_markup=kb(),
    )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or message.chat.type != "private":
        return

    uid = update.effective_user.id
    lock = get_lock(uid)

    async with lock:
        status = await message.reply_text("Processing...")
        stamp = str(int(time.time() * 1000))
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
                await status.edit_text(
                    "File too large (50+ MB).\n"
                    "Telegram bot limit is ~20 MB. Compress and retry."
                )
                return

            input_path = f"in_{stamp}{ext}"
            try:
                tg = await context.bot.get_file(file_obj.file_id)
                await tg.download_to_drive(input_path)
            except Exception as e:
                es = str(e).lower()
                if "too big" in es or "too large" in es or "20" in es:
                    await status.edit_text(
                        "Telegram rejected download (~20 MB max).\n"
                        "Compress the file and send again."
                    )
                else:
                    await status.edit_text(f"Download failed: {str(e)[:120]}")
                return

            bg = get_bg(uid)

            if prefer_bg or bg is None:
                save_bg(uid, input_path)
                await status.edit_text(
                    "Background saved.\n\n"
                    "Now send any video to overlay.\n"
                    "Circle Mode toggles shape.\n"
                    "Reset Background clears it."
                )
                return

            output_path = f"out_{stamp}.mp4"
            circle = is_circle(uid)

            ok = await asyncio.get_event_loop().run_in_executor(
                None, compose, bg, input_path, output_path, circle
            )

            if not ok or not os.path.exists(output_path):
                await status.edit_text("Overlay failed. Check Railway logs.")
                return

            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            with open(output_path, "rb") as f:
                if size_mb > 48:
                    await message.reply_document(
                        document=f,
                        caption="Done",
                        filename="overlay.mp4",
                        read_timeout=300,
                        write_timeout=300,
                    )
                else:
                    await message.reply_video(
                        video=f,
                        caption="Done",
                        supports_streaming=True,
                        read_timeout=300,
                        write_timeout=300,
                    )
            try:
                await status.delete()
            except Exception:
                pass

        except Exception as e:
            print(f"handle_media: {e}", flush=True)
            try:
                await status.edit_text(f"Failed: {str(e)[:150]}")
            except Exception:
                pass
        finally:
            cleanup(input_path, output_path)
            for f in os.listdir("."):
                if stamp in f and (
                    f.startswith("tmp_") or f.startswith("in_") or f.startswith("out_")
                ):
                    cleanup(f)


def main():
    start_health_server()

    ff = get_ffmpeg()
    if not ff:
        print("WARNING: ffmpeg missing — add imageio-ffmpeg to requirements.txt", flush=True)
    else:
        print(f"Using ffmpeg: {ff}", flush=True)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(60.0)
        .read_timeout(300.0)
        .write_timeout(300.0)
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
        except Exception as e:
            print(f"polling died: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
