import os
import time
import shutil
import subprocess
import asyncio
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

def get_ffmpeg() -> str:
    """Prefer system ffmpeg, else bundled imageio-ffmpeg binary (Railway-safe)."""
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


FFMPEG = get_ffmpeg()

user_bg = {}
user_circle = {}
user_locks = {}


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


def probe_duration(path: str) -> float:
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
        )
        if r.stdout.strip():
            return max(float(r.stdout.strip()), 0.5)
    except Exception as e:
        print(f"probe err: {e}", flush=True)
    return 5.0


def run_ff(cmd, timeout=600):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, text=True)


def compose(bg_path: str, main_path: str, output_path: str, circle: bool = False) -> bool:
    """
    Reference-style soft white/cyan lightning bloom around main video.
    Rectangle + Circle. Longer videos supported.
    """
    ff = get_ffmpeg()
    if not ff:
        print("ffmpeg not found - install imageio-ffmpeg or system ffmpeg", flush=True)
        return False
    if not os.path.exists(bg_path) or not os.path.exists(main_path):
        return False

    duration = min(max(probe_duration(main_path), 0.5), 180.0)
    print(f"compose duration={duration:.1f}s circle={circle}", flush=True)

    uid = str(int(time.time() * 1000))
    bg_mp4 = f"tmp_bg_{uid}.mp4"
    main_prep = f"tmp_main_{uid}.mp4"
    glow_soft = f"tmp_glow_s_{uid}.mp4"
    glow_mid = f"tmp_glow_m_{uid}.mp4"
    out_tmp = f"tmp_out_{uid}.mp4"
    W, H = 1080, 1920

    # medium size (reference-ish panel)
    if circle:
        S = 700
        mw = mh = S
    else:
        mw, mh = 700, 1244

    try:
        bg_ext = os.path.splitext(bg_path)[1].lower()
        is_image = bg_ext in [".png", ".jpg", ".jpeg", ".webp"]
        t_limit = max(150, int(duration * 5))

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
            print("bg fail:", (r.stderr or "")[-400:], flush=True)
            return False

        # 2) main panel scaled
        if circle:
            vf_main = f"scale={S}:{S}:force_original_aspect_ratio=increase,crop={S}:{S},setsar=1,fps=30,format=yuv420p"
        else:
            vf_main = (
                f"scale={mw}:{mh}:force_original_aspect_ratio=decrease,"
                f"pad={mw}:{mh}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,format=yuv420p"
            )
        r = run_ff([
            ff, "-y", "-i", main_path,
            "-t", f"{duration:.2f}",
            "-vf", vf_main,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-an", main_prep,
        ], timeout=t_limit)
        if r.returncode != 0 or not os.path.exists(main_prep) or os.path.getsize(main_prep) < 1000:
            print("main fail:", (r.stderr or "")[-400:], flush=True)
            return False

        # 3) multi-layer soft lightning glow (reference style)
        # Layer A: wide soft bloom (cyan-white)
        # Layer B: tighter brighter rim
        if circle:
            # white filled circle then blur
            glow_base = (
                f"format=rgba,"
                f"geq=r='255':g='255':b='255':"
                f"a='if(lte((X-W/2)*(X-W/2)+(Y-H/2)*(Y-H/2)\\,(W/2)*(W/2))\\,255\\,0)'"
            )
        else:
            glow_base = "format=rgba,geq=r='255':g='255':b='255':a='255'"

        # IMPORTANT: pad first so blur can expand OUTSIDE the video frame
        # (without pad, glow gets clipped and looks like a thin line / missing)
        pad_px = 48
        if circle:
            glow_vf_soft = (
                f"format=rgba,"
                f"geq=r='255':g='255':b='255':"
                f"a='if(lte(pow(X-W/2,2)+pow(Y-H/2,2),pow(W/2,2)),255,0)',"
                f"pad=iw+{pad_px*2}:ih+{pad_px*2}:{pad_px}:{pad_px}:color=black@0,"
                f"boxblur=30:14,format=yuv420p"
            )
            glow_vf_mid = (
                f"format=rgba,"
                f"geq=r='255':g='255':b='255':"
                f"a='if(lte(pow(X-W/2,2)+pow(Y-H/2,2),pow(W/2,2)),255,0)',"
                f"pad=iw+{pad_px*2}:ih+{pad_px*2}:{pad_px}:{pad_px}:color=black@0,"
                f"boxblur=14:6,format=yuv420p"
            )
        else:
            glow_vf_soft = (
                f"format=rgba,geq=r='255':g='255':b='255':a='255',"
                f"pad=iw+{pad_px*2}:ih+{pad_px*2}:{pad_px}:{pad_px}:color=black@0,"
                f"boxblur=30:14,format=yuv420p"
            )
            glow_vf_mid = (
                f"format=rgba,geq=r='255':g='255':b='255':a='255',"
                f"pad=iw+{pad_px*2}:ih+{pad_px*2}:{pad_px}:{pad_px}:color=black@0,"
                f"boxblur=14:6,format=yuv420p"
            )

        r = run_ff([
            ff, "-y", "-i", main_prep,
            "-t", f"{duration:.2f}",
            "-vf", glow_vf_soft,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-pix_fmt", "yuv420p", "-an", glow_soft,
        ], timeout=t_limit)
        soft_ok = r.returncode == 0 and os.path.exists(glow_soft) and os.path.getsize(glow_soft) > 500
        if not soft_ok:
            print("soft glow fail:", (r.stderr or "")[-300:], flush=True)

        r = run_ff([
            ff, "-y", "-i", main_prep,
            "-t", f"{duration:.2f}",
            "-vf", glow_vf_mid,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-pix_fmt", "yuv420p", "-an", glow_mid,
        ], timeout=t_limit)
        mid_ok = r.returncode == 0 and os.path.exists(glow_mid) and os.path.getsize(glow_mid) > 500
        if not mid_ok:
            print("mid glow fail:", (r.stderr or "")[-300:], flush=True)

        print(f"glow soft={soft_ok} mid={mid_ok}", flush=True)

        # 4) final stack: bg + soft glow + mid glow + sharp main (+ circle mask)
        if circle:
            # circular alpha on main
            main_masked = (
                f"[1:v]format=rgba,"
                f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
                f"a='if(lte((X-W/2)*(X-W/2)+(Y-H/2)*(Y-H/2)\\,(W/2)*(W/2))\\,255\\,0)'[main]"
            )
            if soft_ok and mid_ok:
                fc = (
                    f"{main_masked};"
                    f"[0:v][2:v]overlay=(W-w)/2:(H-h)/2[t1];"
                    f"[t1][3:v]overlay=(W-w)/2:(H-h)/2[t2];"
                    f"[t2][main]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
                )
                inputs = ["-i", bg_mp4, "-i", main_prep, "-i", glow_soft, "-i", glow_mid, "-i", main_path]
                map_a = "4:a?"
            elif soft_ok:
                fc = (
                    f"{main_masked};"
                    f"[0:v][2:v]overlay=(W-w)/2:(H-h)/2[t1];"
                    f"[t1][main]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
                )
                inputs = ["-i", bg_mp4, "-i", main_prep, "-i", glow_soft, "-i", main_path]
                map_a = "3:a?"
            else:
                fc = (
                    f"{main_masked};"
                    f"[0:v][main]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
                )
                inputs = ["-i", bg_mp4, "-i", main_prep, "-i", main_path]
                map_a = "2:a?"
        else:
            # rectangle: thin bright edge + dual glow under it
            if soft_ok and mid_ok:
                fc = (
                    f"[1:v]drawbox=0:0:iw:ih:color=white@0.75:t=4[main];"
                    f"[0:v][2:v]overlay=(W-w)/2:(H-h)/2[t1];"
                    f"[t1][3:v]overlay=(W-w)/2:(H-h)/2[t2];"
                    f"[t2][main]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
                )
                inputs = ["-i", bg_mp4, "-i", main_prep, "-i", glow_soft, "-i", glow_mid, "-i", main_path]
                map_a = "4:a?"
            elif soft_ok:
                fc = (
                    f"[1:v]drawbox=0:0:iw:ih:color=white@0.75:t=4[main];"
                    f"[0:v][2:v]overlay=(W-w)/2:(H-h)/2[t1];"
                    f"[t1][main]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
                )
                inputs = ["-i", bg_mp4, "-i", main_prep, "-i", glow_soft, "-i", main_path]
                map_a = "3:a?"
            else:
                fc = (
                    f"[1:v]pad={mw+18}:{mh+18}:9:9:color=0xE8F4FF[main];"
                    f"[0:v][main]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
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
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "19",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            out_tmp,
        ], timeout=max(200, int(duration * 6)))

        if r.returncode != 0 or not os.path.exists(out_tmp) or os.path.getsize(out_tmp) < 3000:
            print("final fail:", (r.stderr or "")[-600:], flush=True)
            # simple white pad fallback
            if circle:
                fc2 = (
                    f"[1:v]format=rgba,"
                    f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
                    f"a='if(lte((X-W/2)*(X-W/2)+(Y-H/2)*(Y-H/2)\\,(W/2)*(W/2))\\,255\\,0)'[main];"
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
        cleanup(bg_mp4, main_prep, glow_soft, glow_mid, out_tmp)
        for f in list(os.listdir(".")):
            if uid in f and f.startswith("tmp_"):
                cleanup(f)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Overlay Bot\n\n"
        "1. Send image / GIF / short video → background\n"
        "2. Send any video → overlay + soft lightning glow (reference style)\n"
        "3. Circle Mode → circular frame + same glow\n"
        "4. Reset Background → clear background\n\n"
        "Output 1080×1920. Longer videos supported.\n"
        "DM only. Max download ~20 MB (Telegram limit).\n\n"
        "Tip: Save the result to your device to see the real quality.\n"
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
            "Circle Mode ON ✅\nCircular frame + soft lightning glow."
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
        status = await message.reply_text("Processing... (long videos take time)")
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

            if file_obj.file_size and file_obj.file_size > 20 * 1024 * 1024:
                await status.edit_text(
                    "File too large (Telegram bot max ~20 MB).\nCompress / trim then send again."
                )
                return

            input_path = f"in_{uid}{ext}"
            tg_file = await context.bot.get_file(file_obj.file_id)
            await tg_file.download_to_drive(input_path)

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
                    "Overlay failed.\nLong video? Wait and retry, or keep under 20 MB."
                )
                return

            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            with open(output_path, "rb") as f:
                if size_mb > 48:
                    await message.reply_document(
                        document=f,
                        caption="Done — soft lightning glow",
                        filename="overlay.mp4",
                        read_timeout=180,
                        write_timeout=180,
                    )
                else:
                    await message.reply_video(
                        video=f,
                        caption="Done — soft lightning glow",
                        supports_streaming=True,
                        read_timeout=180,
                        write_timeout=180,
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
    global FFMPEG
    FFMPEG = get_ffmpeg()
    if not FFMPEG:
        print("WARNING: ffmpeg not found. Install imageio-ffmpeg package.")
    else:
        print(f"Using ffmpeg: {FFMPEG}", flush=True)
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(60.0)
        .read_timeout(180.0)
        .write_timeout(180.0)
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
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
