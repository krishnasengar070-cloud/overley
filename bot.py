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

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
except ImportError:
    Image = None

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing in .env")

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

user_bg = {}
user_circle = {}
user_wm = {}
user_await_wm = {}
user_locks = {}

MAX_DURATION = 45.0


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
            [KeyboardButton("Watermark"), KeyboardButton("Help")],
        ],
        resize_keyboard=True,
    )


def bg_path(uid: int, ext: str) -> str:
    return os.path.join(DATA_DIR, f"bg_{uid}{ext}")


def wm_path(uid: int) -> str:
    return os.path.join(DATA_DIR, f"wm_{uid}.png")


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


def get_wm(uid: int):
    p = wm_path(uid)
    if os.path.exists(p) and os.path.getsize(p) > 50:
        return p
    return None


def clear_wm(uid: int):
    p = wm_path(uid)
    if os.path.exists(p):
        try:
            os.remove(p)
        except Exception:
            pass
    user_wm.pop(uid, None)


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
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20,
            )
            if r.returncode == 0 and r.stdout.strip():
                return max(float(r.stdout.strip()), 0.5)
        except Exception:
            pass
    try:
        r = subprocess.run(
            [ff, "-i", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr or "")
        if m:
            return max(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)), 0.5)
    except Exception:
        pass
    return 5.0


def run_ff(cmd, timeout=900):
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, text=True
    )


def make_watermark_png(src: str, out_png: str) -> bool:
    """Make transparent watermark PNG with soft shadow (pvnuo style)."""
    if Image is None:
        return False
    try:
        img = Image.open(src).convert("RGBA")
        # max width for watermark
        max_w = 420
        if img.width > max_w:
            ratio = max_w / img.width
            img = img.resize((max_w, max(1, int(img.height * ratio))), Image.Resampling.LANCZOS)

        # if mostly opaque photo, try to keep as-is with alpha
        # boost contrast a bit for logo look
        r, g, b, a = img.split()
        rgb = Image.merge("RGB", (r, g, b))
        rgb = ImageEnhance.Contrast(rgb).enhance(1.15)
        rgb = ImageEnhance.Brightness(rgb).enhance(1.05)
        r, g, b = rgb.split()

        # soft outer shadow
        shadow = Image.new("RGBA", (img.width + 24, img.height + 24), (0, 0, 0, 0))
        # alpha as mask for shadow
        mask = a.point(lambda x: 200 if x > 20 else 0)
        sh = Image.new("RGBA", img.size, (0, 0, 0, 160))
        shadow.paste(sh, (14, 14), mask)
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=6))

        # paste main on top, slightly transparent
        main_a = a.point(lambda x: int(x * 0.88) if x > 10 else 0)
        main = Image.merge("RGBA", (r, g, b, main_a))
        canvas = Image.new("RGBA", shadow.size, (0, 0, 0, 0))
        canvas = Image.alpha_composite(canvas, shadow)
        canvas.paste(main, (8, 8), main)
        canvas.save(out_png, "PNG")
        return os.path.exists(out_png) and os.path.getsize(out_png) > 50
    except Exception as e:
        print(f"wm make error: {e}", flush=True)
        return False


def compose(bg_path_in: str, main_path: str, output_path: str, circle: bool = False, wm: str = None) -> bool:
    ff = get_ffmpeg()
    if not ff:
        print("ERROR: ffmpeg not found", flush=True)
        return False
    if not os.path.exists(bg_path_in) or not os.path.exists(main_path):
        return False

    raw_dur = probe_duration(main_path, ff)
    duration = min(max(raw_dur, 0.5), MAX_DURATION)
    print(f"compose d={duration:.1f}s (raw={raw_dur:.1f}) circle={circle} wm={bool(wm)}", flush=True)

    uid = str(int(time.time() * 1000))
    bg_mp4 = f"tmp_bg_{uid}.mp4"
    main_prep = f"tmp_main_{uid}.mp4"
    out_tmp = f"tmp_out_{uid}.mp4"
    W, H = 1080, 1920

    if circle:
        mw = mh = 680
    else:
        mw, mh = 700, 1244

    try:
        bg_ext = os.path.splitext(bg_path_in)[1].lower()
        is_image = bg_ext in [".png", ".jpg", ".jpeg", ".webp"]
        tlim = max(300, int(duration * 8) + 120)

        # background
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
        if r.returncode != 0 or not os.path.exists(bg_mp4) or os.path.getsize(bg_mp4) < 1000:
            print("bg fail:", (r.stderr or "")[-500:], flush=True)
            return False

        # main panel
        if circle:
            vf_main = (
                f"scale={mw}:{mh}:force_original_aspect_ratio=increase,"
                f"crop={mw}:{mh},setsar=1,fps=30,format=yuv420p"
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
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-an", main_prep,
        ], timeout=tlim)
        if r.returncode != 0 or not os.path.exists(main_prep) or os.path.getsize(main_prep) < 1000:
            print("main fail:", (r.stderr or "")[-500:], flush=True)
            return False

        # stronger soft lightning glow (dual blur layers)
        pad = 56
        has_wm = bool(wm and os.path.exists(wm))

        # animated watermark: gentle horizontal sway + slight vertical bob at bottom center
        # x = (W-w)/2 + 18*sin(2*PI*t/3)
        # y = H - h - 70 + 10*sin(2*PI*t/2.2)
        if has_wm:
            fc = (
                f"[1:v]pad=iw+{pad*2}:ih+{pad*2}:{pad}:{pad}:color=white,"
                f"boxblur=30:12[glow_soft];"
                f"[1:v]pad=iw+{pad}:ih+{pad}:{pad//2}:{pad//2}:color=white,"
                f"boxblur=12:5[glow_mid];"
                f"[1:v]pad=iw+12:ih+12:6:6:color=white[edge];"
                f"[0:v][glow_soft]overlay=(W-w)/2:(H-h)/2[a];"
                f"[a][glow_mid]overlay=(W-w)/2:(H-h)/2[b];"
                f"[b][edge]overlay=(W-w)/2:(H-h)/2[c];"
                f"[c][1:v]overlay=(W-w)/2:(H-h)/2:shortest=1[base];"
                f"[2:v]format=rgba,colorchannelmixer=aa=0.82[wm];"
                f"[base][wm]overlay=x='(W-w)/2+18*sin(2*PI*t/3)':y='H-h-70+10*sin(2*PI*t/2.2)':shortest=1[v]"
            )
            inputs = ["-i", bg_mp4, "-i", main_prep, "-i", wm, "-i", main_path]
            map_a = "3:a?"
        else:
            fc = (
                f"[1:v]pad=iw+{pad*2}:ih+{pad*2}:{pad}:{pad}:color=white,"
                f"boxblur=30:12[glow_soft];"
                f"[1:v]pad=iw+{pad}:ih+{pad}:{pad//2}:{pad//2}:color=white,"
                f"boxblur=12:5[glow_mid];"
                f"[1:v]pad=iw+12:ih+12:6:6:color=white[edge];"
                f"[0:v][glow_soft]overlay=(W-w)/2:(H-h)/2[a];"
                f"[a][glow_mid]overlay=(W-w)/2:(H-h)/2[b];"
                f"[b][edge]overlay=(W-w)/2:(H-h)/2[c];"
                f"[c][1:v]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
            )
            inputs = ["-i", bg_mp4, "-i", main_prep, "-i", main_path]
            map_a = "2:a?"

        r = run_ff([
            ff, "-y",
            *inputs,
            "-filter_complex", fc,
            "-map", "[v]",
            "-map", map_a,
            "-t", f"{duration:.2f}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            out_tmp,
        ], timeout=max(300, int(duration * 10)))

        if r.returncode != 0 or not os.path.exists(out_tmp) or os.path.getsize(out_tmp) < 3000:
            print("glow fail, simple:", (r.stderr or "")[-500:], flush=True)
            if has_wm:
                fc2 = (
                    f"[1:v]pad={mw+16}:{mh+16}:8:8:color=white[m];"
                    f"[0:v][m]overlay=(W-w)/2:(H-h)/2[base];"
                    f"[2:v]format=rgba,colorchannelmixer=aa=0.82[wm];"
                    f"[base][wm]overlay=(W-w)/2:H-h-70:shortest=1[v]"
                )
                inputs2 = ["-i", bg_mp4, "-i", main_prep, "-i", wm, "-i", main_path]
                map_a2 = "3:a?"
            else:
                fc2 = (
                    f"[1:v]pad={mw+16}:{mh+16}:8:8:color=white[m];"
                    f"[0:v][m]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
                )
                inputs2 = ["-i", bg_mp4, "-i", main_prep, "-i", main_path]
                map_a2 = "2:a?"
            r2 = run_ff([
                ff, "-y",
                *inputs2,
                "-filter_complex", fc2,
                "-map", "[v]", "-map", map_a2,
                "-t", f"{duration:.2f}",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
                "-c:a", "aac", "-b:a", "128k",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                out_tmp,
            ], timeout=max(240, int(duration * 7)))
            if r2.returncode != 0 or not os.path.exists(out_tmp) or os.path.getsize(out_tmp) < 3000:
                print("fallback fail:", (r2.stderr or "")[-400:], flush=True)
                return False

        # compress if huge for telegram send
        size = os.path.getsize(out_tmp)
        if size > 45 * 1024 * 1024:
            comp = f"tmp_comp_{uid}.mp4"
            r3 = run_ff([
                ff, "-y", "-i", out_tmp,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-c:a", "aac", "-b:a", "96k",
                "-movflags", "+faststart", "-pix_fmt", "yuv420p",
                comp,
            ], timeout=max(180, int(duration * 5)))
            if r3.returncode == 0 and os.path.exists(comp) and os.path.getsize(comp) > 3000:
                cleanup(out_tmp)
                out_tmp = comp

        shutil.move(out_tmp, output_path)
        print(f"OK {os.path.getsize(output_path)} bytes", flush=True)
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
        "2. Send any video → overlay + lightning glow\n"
        "3. Watermark → send logo image after pressing button\n"
        "4. Circle Mode / Reset Background\n\n"
        "Output 1080x1920. Up to ~40s videos.\n"
        "DM only. File size limit ~20 MB (Telegram).\n\n"
        "Tip: Save result to device for real quality.",
        reply_markup=kb(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    bg = get_bg(uid)
    wm = get_wm(uid)
    mode = "Circle" if is_circle(uid) else "Rectangle"
    await update.message.reply_text(
        "How to use\n\n"
        f"Background: {'set' if bg else 'not set'}\n"
        f"Watermark: {'set' if wm else 'not set'}\n"
        f"Shape: {mode}\n\n"
        "• Media → background (if none) or overlay\n"
        "• Watermark button → then send logo image\n"
        "• Logo becomes transparent animated mark at bottom\n"
        "• Videos up to ~40 seconds supported",
        reply_markup=kb(),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if update.message.chat.type != "private":
        return
    text = update.message.text.strip()
    uid = update.effective_user.id
    low = text.lower()

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

    if text == "Watermark" or low == "watermark":
        user_await_wm[uid] = True
        await update.message.reply_text(
            "Watermark mode ON.\n\n"
            "Now send your logo / text image (@pvnuo style).\n"
            "It will be transparent + slightly animated at the bottom.\n\n"
            "Send another Watermark message after setting to clear it.",
            reply_markup=kb(),
        )
        return

    if text == "Help":
        await help_cmd(update, context)
        return

    await update.message.reply_text(
        "Send image/GIF/video for background, or a video to overlay.\n"
        "Press Watermark then send logo image.",
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
                    "File too large (50+ MB).\nTelegram bot limit ~20 MB. Compress and retry."
                )
                return

            input_path = f"in_{stamp}{ext}"
            try:
                tg = await context.bot.get_file(file_obj.file_id)
                await tg.download_to_drive(input_path)
            except Exception as e:
                es = str(e).lower()
                if "too big" in es or "too large" in es:
                    await status.edit_text(
                        "Telegram rejected download (~20 MB max).\nCompress and send again."
                    )
                else:
                    await status.edit_text(f"Download failed: {str(e)[:120]}")
                return

            # watermark set flow
            if user_await_wm.get(uid) and prefer_bg:
                user_await_wm[uid] = False
                dest = wm_path(uid)
                ok = make_watermark_png(input_path, dest)
                if not ok:
                    # fallback: just copy as png-ish
                    try:
                        if Image:
                            Image.open(input_path).convert("RGBA").save(dest, "PNG")
                            ok = True
                    except Exception:
                        ok = False
                if ok:
                    user_wm[uid] = dest
                    await status.edit_text(
                        "Watermark saved.\n"
                        "It will appear transparent + animated at the bottom of overlays.\n"
                        "Press Watermark again anytime to replace / set new."
                    )
                else:
                    await status.edit_text("Could not process watermark image. Try PNG/JPG logo.")
                return

            # if caption is watermark
            cap = (message.caption or "").strip().lower()
            if prefer_bg and cap == "watermark":
                dest = wm_path(uid)
                ok = make_watermark_png(input_path, dest)
                if not ok and Image:
                    try:
                        Image.open(input_path).convert("RGBA").save(dest, "PNG")
                        ok = True
                    except Exception:
                        ok = False
                if ok:
                    user_wm[uid] = dest
                    await status.edit_text("Watermark saved (from caption).")
                else:
                    await status.edit_text("Watermark failed.")
                return

            bg = get_bg(uid)

            if prefer_bg or bg is None:
                save_bg(uid, input_path)
                await status.edit_text(
                    "Background saved.\n\n"
                    "Send a video to overlay with lightning glow.\n"
                    "Watermark button → set logo.\n"
                    "Circle Mode / Reset Background available."
                )
                return

            output_path = f"out_{stamp}.mp4"
            circle = is_circle(uid)
            wm = get_wm(uid)
            ok = await asyncio.get_event_loop().run_in_executor(
                None, compose, bg, input_path, output_path, circle, wm
            )
            if not ok or not os.path.exists(output_path):
                await status.edit_text(
                    "Overlay failed.\n"
                    "Try a shorter clip or compress under 20 MB.\n"
                    "Check logs if it keeps failing."
                )
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
        print("WARNING: ffmpeg missing — imageio-ffmpeg required", flush=True)
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
