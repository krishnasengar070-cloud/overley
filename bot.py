import os
import time
import shutil
import subprocess
import asyncio
import re
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


def get_ffmpeg() -> str:
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    if imageio_ffmpeg is not None:
        try:
            path = imageio_ffmpeg.get_ffmpeg_exe()
            if path and os.path.exists(path):
                return path
        except Exception as e:
            print(f"imageio-ffmpeg err: {e}", flush=True)
    return ""


# ---------- Railway keep-alive (stdlib only, no flask needed) ----------
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Overlay bot is alive!"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def keep_alive():
    port = int(os.environ.get("PORT", 8080))

    def _run():
        try:
            server = HTTPServer(("0.0.0.0", port), _HealthHandler)
            print(f"Health server on port {port}", flush=True)
            server.serve_forever()
        except Exception as e:
            print(f"Health server error: {e}", flush=True)

    t = Thread(target=_run, daemon=True)
    t.start()


def get_lock(user_id: int) -> asyncio.Lock:
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    return user_locks[user_id]


def keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("Reset Background"), KeyboardButton("Circle Mode")],
            [KeyboardButton("Help")],
        ],
        resize_keyboard=True,
    )


def bg_path_for(user_id: int, ext: str) -> str:
    return os.path.join(DATA_DIR, f"bg_{user_id}{ext}")


def get_bg(user_id: int):
    for ext in [".mp4", ".gif", ".png", ".jpg", ".jpeg", ".webp"]:
        path = bg_path_for(user_id, ext)
        if os.path.exists(path) and os.path.getsize(path) > 100:
            return path
    return None


def clear_bg(user_id: int):
    for ext in [".mp4", ".gif", ".png", ".jpg", ".jpeg", ".webp"]:
        path = bg_path_for(user_id, ext)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
    user_bg.pop(user_id, None)


def save_bg(user_id: int, src: str) -> str:
    clear_bg(user_id)
    ext = os.path.splitext(src)[1].lower() or ".mp4"
    if ext not in [".mp4", ".gif", ".png", ".jpg", ".jpeg", ".webp"]:
        ext = ".mp4"
    dest = bg_path_for(user_id, ext)
    shutil.copy(src, dest)
    user_bg[user_id] = dest
    return dest


def is_circle(user_id: int) -> bool:
    return bool(user_circle.get(user_id, False))


def set_circle(user_id: int, on: bool):
    user_circle[user_id] = on


def cleanup(*paths):
    for path in paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


def probe_duration(path: str, ff: str) -> float:
    candidates = ["ffprobe"]
    if ff and ff != "ffmpeg":
        probe = os.path.join(os.path.dirname(ff), "ffprobe")
        if os.path.exists(probe):
            candidates.insert(0, probe)
    for probe in candidates:
        try:
            r = subprocess.run(
                [
                    probe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    path,
                ],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
            )
            if r.returncode == 0 and r.stdout.strip():
                return max(float(r.stdout.strip()), 0.5)
        except Exception:
            pass
    try:
        r = subprocess.run(
            [ff, "-i", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
        )
        err = r.stderr or ""
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", err)
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return max(h * 3600 + mi * 60 + s, 0.5)
    except Exception as e:
        print(f"probe err: {e}", flush=True)
    return 5.0


def run_ff(cmd, timeout=900):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, text=True)


def compose(bg_path: str, main_path: str, output_path: str, circle: bool = False) -> bool:
    """
    9:16 overlay + soft white glow border.
    Simplified for Railway / imageio-ffmpeg reliability.
    """
    ff = get_ffmpeg()
    if not ff:
        print("ffmpeg not found - install imageio-ffmpeg", flush=True)
        return False
    if not os.path.exists(bg_path) or not os.path.exists(main_path):
        return False

    duration = min(max(probe_duration(main_path, ff), 0.5), 180.0)
    print(f"compose duration={duration:.1f}s circle={circle} ff={ff}", flush=True)

    uid = str(int(time.time() * 1000))
    bg_mp4 = f"tmp_bg_{uid}.mp4"
    main_prep = f"tmp_main_{uid}.mp4"
    out_tmp = f"tmp_out_{uid}.mp4"
    W, H = 1080, 1920

    if circle:
        S = 700
        mw = mh = S
    else:
        mw, mh = 700, 1244

    try:
        bg_ext = os.path.splitext(bg_path)[1].lower()
        is_image = bg_ext in [".png", ".jpg", ".jpeg", ".webp"]
        t_limit = max(180, int(duration * 5) + 60)

        # 1) background
        if is_image:
            cmd_bg = [
                ff, "-y", "-loop", "1", "-i", bg_path,
                "-t", f"{duration:.2f}",
                "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps=30,format=yuv420p",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-pix_fmt", "yuv420p", "-an", bg_mp4,
            ]
        else:
            cmd_bg = [
                ff, "-y", "-stream_loop", "-1", "-i", bg_path,
                "-t", f"{duration:.2f}",
                "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps=30,format=yuv420p",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-pix_fmt", "yuv420p", "-an", bg_mp4,
            ]
        r = run_ff(cmd_bg, timeout=t_limit)
        if r.returncode != 0 or not os.path.exists(bg_mp4) or os.path.getsize(bg_mp4) < 1000:
            print("bg fail:", (r.stderr or "")[-500:], flush=True)
            return False

        # 2) main panel (simple scale/pad — no fragile geq)
        if circle:
            # square first; circular look via vignette-ish pad later if needed
            vf_main = (
                f"scale={S}:{S}:force_original_aspect_ratio=increase,"
                f"crop={S}:{S},setsar=1,fps=30,format=yuv420p"
            )
        else:
            vf_main = (
                f"scale={mw}:{mh}:force_original_aspect_ratio=decrease,"
                f"pad={mw}:{mh}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1,fps=30,format=yuv420p"
            )
        r = run_ff([
            ff, "-y", "-i", main_path,
            "-t", f"{duration:.2f}",
            "-vf", vf_main,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-an", main_prep,
        ], timeout=t_limit)
        if r.returncode != 0 or not os.path.exists(main_prep) or os.path.getsize(main_prep) < 1000:
            print("main fail:", (r.stderr or "")[-500:], flush=True)
            return False

        # 3) final: soft white outer glow via pad + boxblur underlay (reliable)
        # glow plate = white rect/square blurred larger than main
        pad_px = 48
        if circle:
            # approximate circle glow: pad white then blur, overlay main square centered
            fc = (
                f"[1:v]pad=iw+{pad_px*2}:ih+{pad_px*2}:{pad_px}:{pad_px}:color=white,"
                f"boxblur=24:10[glow];"
                f"[1:v]pad=iw+8:ih+8:4:4:color=white[rim];"
                f"[0:v][glow]overlay=(W-w)/2:(H-h)/2[t1];"
                f"[t1][rim]overlay=(W-w)/2:(H-h)/2[t2];"
                f"[t2][1:v]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
            )
        else:
            fc = (
                f"[1:v]pad=iw+{pad_px*2}:ih+{pad_px*2}:{pad_px}:{pad_px}:color=white,"
                f"boxblur=28:12[glow];"
                f"[1:v]drawbox=0:0:iw:ih:color=white@0.85:t=5[rim];"
                f"[0:v][glow]overlay=(W-w)/2:(H-h)/2[t1];"
                f"[t1][rim]overlay=(W-w)/2:(H-h)/2[t2];"
                f"[t2][1:v]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
            )

        r = run_ff([
            ff, "-y",
            "-i", bg_mp4,
            "-i", main_prep,
            "-i", main_path,
            "-filter_complex", fc,
            "-map", "[v]",
            "-map", "2:a?",
            "-t", f"{duration:.2f}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "19",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            out_tmp,
        ], timeout=max(240, int(duration * 6)))

        if r.returncode != 0 or not os.path.exists(out_tmp) or os.path.getsize(out_tmp) < 3000:
            print("glow compose fail, simple border:", (r.stderr or "")[-500:], flush=True)
            # simple white pad border fallback
            if circle:
                fc2 = (
                    f"[1:v]pad={S+16}:{S+16}:8:8:color=white[main];"
                    f"[0:v][main]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
                )
            else:
                fc2 = (
                    f"[1:v]pad={mw+16}:{mh+16}:8:8:color=white[main];"
                    f"[0:v][main]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
                )
            r2 = run_ff([
                ff, "-y",
                "-i", bg_mp4, "-i", main_prep, "-i", main_path,
                "-filter_complex", fc2,
                "-map", "[v]", "-map", "2:a?",
                "-t", f"{duration:.2f}",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
                "-c:a", "aac", "-b:a", "128k",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                out_tmp,
            ], timeout=max(180, int(duration * 5)))
            if r2.returncode != 0 or not os.path.exists(out_tmp) or os.path.getsize(out_tmp) < 3000:
                print("fallback fail:", (r2.stderr or "")[-400:], flush=True)
                return False

        shutil.move(out_tmp, output_path)
        print(f"OK size={os.path.getsize(output_path)}", flush=True)
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
        "2. Send any video → overlay + soft lightning glow\n"
        "3. Circle Mode → circular-style frame + same glow\n"
        "4. Reset Background → clear background\n\n"
        "Output 1080×1920. Long videos supported.\n"
        "DM only.\n\n"
        "Tip: Save the result to your device to see real quality.\n"
        "Telegram chat preview can look different.",
        reply_markup=keyboard(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    bg = get_bg(uid)
    mode = "Circle" if is_circle(uid) else "Rectangle"
    status = "Background set ✅" if bg else "No background yet"
    await update.message.reply_text(
        "How to use\n\n"
        f"Status: {status}\n"
        f"Shape: {mode}\n\n"
        "• Image / GIF / video → set background\n"
        "• Another video → overlay + soft lightning glow\n"
        "• Circle Mode → toggle circle\n"
        "• Reset Background → clear\n\n"
        "Tip: Save the result video to your device first.\n"
        "In Telegram chat the glow can look different than the real file.",
        reply_markup=keyboard(),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if update.message.chat.type != "private":
        return
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if text == "Reset Background":
        clear_bg(user_id)
        await update.message.reply_text(
            "Background cleared.\nSend a new image, GIF, or video.",
            reply_markup=keyboard(),
        )
        return

    if text == "Circle Mode":
        on = not is_circle(user_id)
        set_circle(user_id, on)
        msg = (
            "Circle Mode ON ✅\nFrame + soft lightning glow."
            if on else
            "Circle Mode OFF\nRectangle frame + soft lightning glow."
        )
        await update.message.reply_text(msg, reply_markup=keyboard())
        return

    if text == "Help":
        await help_cmd(update, context)
        return

    await update.message.reply_text(
        "Send image/GIF/video for background, or a video to overlay.",
        reply_markup=keyboard(),
    )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or message.chat.type != "private":
        return

    user_id = update.effective_user.id
    lock = get_lock(user_id)

    async with lock:
        status = await message.reply_text("Processing... (long videos take more time)")
        uid = str(int(time.time() * 1000))
        input_path = None
        output_path = None

        try:
            is_photo = bool(message.photo)
            is_animation = bool(message.animation)
            is_video = bool(message.video)
            is_doc = bool(message.document)

            file_obj = None
            prefer_bg = False
            ext = ".mp4"

            if is_photo:
                file_obj = message.photo[-1]
                prefer_bg = True
                ext = ".jpg"
            elif is_animation:
                file_obj = message.animation
                prefer_bg = True
                ext = ".mp4"
            elif is_video:
                file_obj = message.video
                ext = ".mp4"
            elif is_doc:
                mime = (message.document.mime_type or "").lower()
                name = (message.document.file_name or "").lower()
                file_obj = message.document
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
                    "File is very large (50+ MB).\n"
                    "Telegram bots can usually download only up to ~20 MB.\n"
                    "Compress the video a bit and send again."
                )
                return

            input_path = f"in_{uid}{ext}"
            try:
                tg_file = await context.bot.get_file(file_obj.file_id)
                await tg_file.download_to_drive(input_path)
            except Exception as dl_err:
                err_s = str(dl_err).lower()
                if "file is too big" in err_s or "too large" in err_s or "20" in err_s:
                    await status.edit_text(
                        "Telegram rejected download (file too large for bots, ~20 MB max).\n"
                        "Compress / lower quality, then send again."
                    )
                else:
                    await status.edit_text(f"Download failed: {str(dl_err)[:120]}")
                return

            bg = get_bg(user_id)

            if prefer_bg or bg is None:
                save_bg(user_id, input_path)
                shape = "circle" if is_circle(user_id) else "rectangle"
                await status.edit_text(
                    "Background saved.\n\n"
                    f"Send any video → overlay ({shape}) + soft lightning glow.\n"
                    "Circle Mode toggles circle.\n"
                    "Reset Background clears it."
                )
                return

            output_path = f"out_{uid}.mp4"
            circle = is_circle(user_id)
            ok = await asyncio.get_event_loop().run_in_executor(
                None, compose, bg, input_path, output_path, circle
            )
            if not ok or not os.path.exists(output_path):
                await status.edit_text(
                    "Overlay failed.\n"
                    "Check Railway logs for: Using ffmpeg: ...\n"
                    "If ffmpeg missing, requirements.txt must include imageio-ffmpeg."
                )
                return

            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            with open(output_path, "rb") as f:
                if size_mb > 48:
                    await message.reply_document(
                        document=f,
                        caption="Done — soft lightning glow",
                        filename="overlay.mp4",
                        read_timeout=300,
                        write_timeout=300,
                    )
                else:
                    await message.reply_video(
                        video=f,
                        caption="Done — soft lightning glow",
                        supports_streaming=True,
                        read_timeout=300,
                        write_timeout=300,
                    )
            try:
                await status.delete()
            except Exception:
                pass
        except Exception as e:
            print(f"handle_media error: {e}", flush=True)
            try:
                await status.edit_text(f"Failed: {str(e)[:150]}")
            except Exception:
                pass
        finally:
            cleanup(input_path, output_path)
            for f in os.listdir("."):
                if uid in f and (f.startswith("tmp_") or f.startswith("in_") or f.startswith("out_")):
                    cleanup(f)


def main():
    keep_alive()  # Railway PORT health

    ff = get_ffmpeg()
    if not ff:
        print("WARNING: ffmpeg not found. requirements.txt must have imageio-ffmpeg", flush=True)
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
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL,
        handle_media,
    ))
    print("Overlay bot started...", flush=True)

    while True:
        try:
            app.run_polling(drop_pending_updates=True, close_loop=False)
        except Exception as e:
            print(f"[RESTART] {e}", flush=True)
            time.sleep(5)
            continue
        break


if __name__ == "__main__":
    main()
