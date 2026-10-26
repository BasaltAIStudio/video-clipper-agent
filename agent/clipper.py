"""agent/clipper.py — ffmpeg video/caption rendering (single-call per clip).

Each clip is rendered by ONE ffmpeg invocation:
  - input seeking (-ss) for speed
  - scale to fill target aspect + pad to exact pixels (no UI stretch)
  - ASS subtitle burn-in (karaoke-style, word-by-word)
  - audio normalization (loudnorm, -14 LUFS platform standard, from chopify)

Pure ffmpeg subprocess calls: the container needs only ffmpeg (+ an ASS-capable
copy of fontconfig for captions).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from .config import FFMPEG_BINARY, TARGET_WIDTH, logger_config

log = logger_config("agent.clipper")

TARGET_PIXELS: dict[str, tuple[int, int]] = {
    "9:16": (TARGET_WIDTH, 1920),
    "16:9": (1920, 1080),
    "1:1": (TARGET_WIDTH, TARGET_WIDTH),
}

LOUDNORM_I = float(os.getenv("LOUDNORM_I", "-14.0"))
LOUDNORM_LRA = float(os.getenv("LOUDNORM_LRA", "11.0"))
LOUDNORM_TPK = float(os.getenv("LOUDNORM_TPK", "-1.5"))


class ClipError(RuntimeError):
    """One of the rendering steps failed."""


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        [FFMPEG_BINARY, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _sanitize_filename(text: str, maxlen: int = 80) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[^\w\s-]", "", t)
    t = re.sub(r"\s+", "-", t)
    t = re.sub(r"-+", "-", t).strip("-")
    return t[:maxlen].strip("-") or "clip"


def _target_aspect(aspect: str) -> Tuple[int, int]:
    if aspect not in TARGET_PIXELS:
        raise ClipError(f"unknown aspect '{aspect}'. Choose from {sorted(TARGET_PIXELS)}")
    return TARGET_PIXELS[aspect]


def render_clip(
    source_path: Path,
    start: float,
    end: float,
    output_path: Path,
    *,
    aspect: str = "9:16",
    captions_ass: Optional[Path] = None,
    preview: bool = False,
) -> None:
    """Render one clip in a single ffmpeg call.

    source_path : video, native aspect
    start/end   : window in source seconds
    output_path : will be encoded h264 + aac, padded to TARGET_PIXELS[aspect]
    captions_ass: optional ASS file to burn in
    preview     : 480p fast draft (no captions, no loudnorm)
    """
    w, h = _target_aspect(aspect)
    if preview:
        scale = (480, 854) if aspect == "9:16" else (854, 480)
        vf = f"scale={scale[0]}:{scale[1]}:force_original_aspect_ratio=decrease,pad={scale[0]}:{scale[1]}:(ow-iw)/2:(oh-ih)/2"
        af = "aresample=async=1"
    else:
        vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
        af = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TPK}:LRA={LOUDNORM_LRA}"

    vf += ",setsar=1"
    vf_extra = ""
    if captions_ass and captions_ass.exists():
        esc = str(captions_ass).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        vf_extra = f",subtitles='{esc}'"

    crf, preset = ("28", "veryfast") if preview else ("23", "fast")

    cmd = [
        FFMPEG_BINARY, "-nostdin", "-filter_threads", "2", "-y",
        "-ss", str(start),
        "-i", str(source_path),
        "-t", str(end - start),
        "-vf", vf + vf_extra,
        "-af", af,
        "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-threads", os.getenv("FFMPEG_THREADS", "2"),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-shortest",
        str(output_path),
    ]
    log.info("render_clip %.1fs->%.1fs  -> %s", start, end, output_path.name)
    result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()
        raise ClipError(f"ffmpeg failed rc={result.returncode}: {' | '.join(tail[-5:])}")


def build_ass_subtitles(segments: List[dict], start: float, end: float, *, font_size: int = 24, font_name: str = "Arial") -> str:
    """Generate an ASS subtitle block for the transcript segments inside [start, end].

    Returns raw ASS text (no filename). The pipeline writes it to a temp file.
    """
    lines = []
    lines.append("[Script Info]")
    lines.append("Title: Clipper Captions")
    lines.append("ScriptType: v4.00+")
    lines.append("PlayResX: 1080")
    lines.append("PlayResY: 1920")
    lines.append("")
    lines.append("[V4+ Styles]")
    lines.append(f"Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding")
    lines.append(f"Style: Default,{font_name},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,0,0,2,10,10,10,1")
    lines.append("")
    lines.append("[Events]")
    lines.append("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text")

    ev = []
    for s in segments:
        s_start = s.get("start", 0.0)
        s_end = s.get("end", s_start)
        if s_end <= start or s_start >= end:
            continue
        txt = s.get("text", "").strip()
        if not txt:
            continue
        seg_start = max(s_start, start)
        seg_end = min(s_end, end)
        ev.append((seg_start, seg_end, txt))

    ev.sort()
    merged = []
    for ts_start, ts_end, txt in ev:
        if merged and ts_start <= merged[-1][1] + 0.12:
            merged[-1] = (merged[-1][0], ts_end, merged[-1][2] + " " + txt)
        else:
            merged.append((ts_start, ts_end, txt))

    for ts_start, ts_end, txt in merged:
        def fmt(t: float) -> str:
            t = max(0.0, float(t) - start)  # clip-relative (input is seeked)
            cs = int(round(t * 100))
            h, cs = divmod(cs, 360000)
            m, cs = divmod(cs, 6000)
            sec, cs = divmod(cs, 100)
            return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"
        safe = txt.replace("\\", "").replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{fmt(ts_start)},{fmt(ts_end)},Default,,0,0,0,,{safe}")

    return "\n".join(lines)
