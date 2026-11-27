"""Render a translucent glass rim over the original video."""
import io
import logging
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
        logging.warning("%s failed: %s", args[0], exc.stderr.decode(errors="replace")[-1600:])
        raise MediaError("The media export failed. Try an H.264 MP4 or a transparent PNG logo.") from exc


# Fractions of the shorter video side, so the same style works at any resolution.
LOGO_CENTER_Y = 0.12
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


LOGO_FORMATS = "png_pipe,jpeg_pipe,mjpeg,gif,mov,webp_pipe,matroska,webm"


def inspect_logo(path):
    info = json.loads(run(["ffprobe", "-v", "error", "-protocol_whitelist", "file",
                           "-format_whitelist", LOGO_FORMATS, "-show_format",
                           "-show_streams", "-of", "json", str(path)]))
    stream = next((s for s in info["streams"] if s["codec_type"] == "video"
                   and not s.get("disposition", {}).get("attached_pic")), None)
    if not stream or stream["width"] * stream["height"] > 4096 * 2160:
        raise MediaError("Send a PNG, JPEG, GIF or MP4 logo up to 4K.")
    return stream, info["format"]["format_name"] in {"png_pipe", "jpeg_pipe", "mjpeg", "webp_pipe"}


def logo_filters(path, index):
    """Inspect small RGBA previews, keeping a single crop for the whole animation."""
    from statistics import median
    from PIL import ImageFilter
    side = 96
    args = ["ffmpeg", "-v", "error", "-protocol_whitelist", "file", "-format_whitelist",
            LOGO_FORMATS, "-threads", "2", "-filter_threads", "1", "-i", str(path), "-map", f"0:{index}", "-vf",
            "select='isnan(prev_selected_t)+gte(t-prev_selected_t,0.125)',scale=96:96",
            "-fps_mode", "vfr", "-frames:v", "960", "-pix_fmt", "rgba", "-threads", "1", "-f", "rawvideo", "-"]
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
    # Use the majority colour along the perimeter. A lower majority threshold
    # also handles a solid screen occupying only one section of a clip.
    opaque = [pixel[:3] for pixel in edges if pixel[3] > 240]
    key = None
    prefix = ""
    if len(opaque) > 0.97 * len(edges) and all(frame.getchannel("A").getextrema()[0] == 255 for frame in frames):
        candidate = tuple(round(median(channel)) for channel in zip(*opaque))
        matching = sum(max(abs(a-b) for a,b in zip(rgb,candidate)) <= 20 for rgb in opaque)
        if matching >= 0.52 * len(opaque):
            key = candidate
            tolerance = 0.11 if key[1] > max(key[0],key[2])+45 else 0.035
            prefix = ("colorkey=0x%02x%02x%02x:" % key) + f"{tolerance}:0.015,"
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
    if not box:
        return "", 1, 1
    if box != (0,0,side,side) and len(frames) < 960:
        l,t,r,b = box
        prefix += f"crop=ceil(iw*{r-l}/96):ceil(ih*{b-t}/96):floor(iw*{l}/96):floor(ih*{t}/96),"
        return prefix, (r-l)/side, (b-t)/side
    return prefix, 1, 1


def prepare_logo(path, remove_bg=True):
    stream, still = inspect_logo(path)
    prefix, x_fraction, y_fraction = logo_filters(path, stream["index"]) if remove_bg else ("", 1, 1)
    w, h = stream["width"], stream["height"]
    rotation = next((x["rotation"] for x in stream.get("side_data_list", [])
                     if "rotation" in x), 0)
    if abs(rotation) % 180 == 90:
        w, h = h, w
    return stream["index"], still, prefix, math.ceil(w*x_fraction), math.ceil(h*y_fraction)


def logo_layout(width, height, size, x, y):
    """Use the same fit and edge clamps in the editor and the video."""
    width_limit = max(1, round(width * size / 100))
    height_limit = max(1, round(height * size / 100))
    fit = (f"scale={width_limit}:{height_limit}:force_original_aspect_ratio=decrease:"
           "flags=lanczos,")
    return fit, f"clip({x}/100*W-w/2,0,W-w)", f"clip({y}/100*H-h/2,0,H-h)"


def preview_mark(path, remove_bg):
    index, still, prep, mw, mh = prepare_logo(path, remove_bg)
    # Sample several points so an animation starting transparent has a visible poster.
    args = ["ffmpeg", "-v", "error", "-protocol_whitelist", "file", "-format_whitelist",
            LOGO_FORMATS, "-threads", "2", "-filter_threads", "1", "-i", str(path),
            "-map", f"0:{index}", "-vf", "format=rgba," + prep +
            "select='isnan(prev_selected_t)+gte(t-prev_selected_t,0.5)',"
            "scale=360:640:force_original_aspect_ratio=decrease,"
            "format=rgba,pad=360:640:0:0:color=black@0",
            "-frames:v", "1" if still else "12", "-fps_mode", "vfr",
            "-threads", "1", "-f", "rawvideo", "-pix_fmt", "rgba", "-"]
    raw = run(args)
    stride = 360 * 640 * 4
    factor = min(360 / mw, 640 / mh)
    extent = (0, 0, max(1, round(mw * factor)), max(1, round(mh * factor)))
    frames = [Image.frombytes("RGBA", (360, 640), raw[i:i+stride]).crop(extent)
              for i in range(0, len(raw), stride)]
    if not frames:
        raise MediaError("The logo has no readable preview frame.")
    return max(frames, key=lambda im: sum(im.getchannel("A").resize((32, 32)).getdata()))


def placement_preview(mark, size, x, y):
    canvas = Image.new("RGB", (360, 640), "#343940")
    pen = ImageDraw.Draw(canvas)
    for xx in range(0, 360, 36):
        pen.line((xx, 0, xx, 639), fill="#454b55")
    for yy in range(0, 640, 64):
        pen.line((0, yy, 359, yy), fill="#454b55")
    scale = min(360 * size / (100 * mark.width), 640 * size / (100 * mark.height))
    mark = mark.resize((max(1, round(mark.width * scale)), max(1, round(mark.height * scale))),
                       Image.Resampling.LANCZOS)
    left = min(360-mark.width, max(0, round(x*3.6-mark.width/2)))
    top = min(640-mark.height, max(0, round(y*6.4-mark.height/2)))
    canvas.paste(mark, (left, top), mark)
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=91)
    return buf.getvalue()


def render(source: Path, output: Path, background: Path | None = None, logo: Path | None = None, logo_details=None, position="top", size=95, x=50, y=None):
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
        scene_fit = f"scale={bw}:{bh},setsar=1"
        if logo is not None:
            # Fit a nonportrait template in a 9:16 canvas without stretching.
            unit = max(1, min(120, round(max(bw, bh) / 32)))
            cw, ch = 18 * unit, 32 * unit
            ratio = min(cw / bw, ch / bh)
            sw, sh = max(2, 2 * round(bw * ratio / 2)), max(2, 2 * round(bh * ratio / 2))
            scene_fit = f"scale={sw}:{sh},setsar=1,pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2"
            bw, bh = cw, ch
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
                      f"{scene_fit}[scene];")
        else:
            # Use the foreground clock to retain variable frame timestamps.
            graph += (f",split[clock][front];[clock]pad={bw}:{bh}:0:0[canvas];"
                      f"[2:{bg['index']}]{scene_fit},setpts=PTS-STARTPTS[bg];"
                      "[canvas][bg]overlay=0:0:shortest=1[scene];")
        graph += f"[scene][front]overlay=(W-w)/2:{top}:shortest=1[composite]"
    video_label = "[composite]" if background is not None else "[v]"
    if logo is not None:
        input_number = 3 if background is not None else 1
        if not still:
            seconds = float(video.get("duration") or info["format"]["duration"])
            extra_inputs += ["-stream_loop", "-1", "-t", str(seconds + 1)]
        extra_inputs += ["-protocol_whitelist", "file", "-format_whitelist", LOGO_FORMATS,
                         "-threads", "2", "-i", str(logo)]
        eof = "" if still else ":shortest=1"
        overlay_format = ":format=yuv444" if logo_only and (w % 2 or h % 2) else ""
        canvas_w, canvas_h = (w, h) if logo_only else (bw, bh)
        if y is None:
            y = 88 if position == "bottom" else 12
        fit, x_position, y_position = logo_layout(canvas_w, canvas_h, size, x, y)
        graph += (f";[{input_number}:{index}]format=rgba,{prep}{fit}setsar=1,"
                  f"setpts=PTS-STARTPTS[mark];{video_label}[mark]overlay="
                  f"'{x_position}':'{y_position}'{eof}{overlay_format}[branded]")
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
