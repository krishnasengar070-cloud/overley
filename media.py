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


def render(source: Path, output: Path, background: Path | None = None):
    info, video, w, h = inspect_video(source)
    local = ["-protocol_whitelist", "file", "-format_whitelist", FORMATS]
    mask = output.with_suffix(".png")
    short_side = min(w, h)
    rim = max(2, round(short_side * GLASS_WIDTH))
    gap = max(1, round(short_side * GAP_RATIO))
    inset = rim + gap
    width, height = w + 2 * inset, h + 2 * inset
    radius = inset + max(1, round(short_side * 0.047))
    make_rim((width, height), rim, inset, radius, mask)
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    # Reflect each live edge into the added margin, including both axes at corners.
    graph = (f"[0:{video['index']}]format=yuv444p,pad={width}:{height}:{inset}:{inset},"
             f"fillborders=left={inset}:right={inset}:top={inset}:bottom={inset}:mode=mirror[reflected];"
             "[reflected][1:v]overlay=0:0:format=yuv444,"
             "pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p[v]")
    extra_inputs = []
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
        box_h = max(2, 2 * int(bh * 0.72 / 2))
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
    base = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-filter_complex_threads", "3",
            *local, "-threads", "2", "-i", str(source), "-i", str(mask),
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
