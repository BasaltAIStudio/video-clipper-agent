"""yt-dlp option helpers: YouTube cookie support.

YouTube blocks anonymous downloads from datacenter IPs. Supply the contents of a
Netscape cookies.txt through environment variables (store them as secrets):

  1. YOUTUBE_COOKIES_B64_1, YOUTUBE_COOKIES_B64_2, ...  base64 of cookies.txt split
     into chunks, joined in numeric order. Use this when the host limits value size
     or its bulk importer mangles raw cookie lines.
  2. YOUTUBE_COOKIES_B64   one base64 value.
  3. YOUTUBE_COOKIES       raw cookies.txt text.
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

log = logging.getLogger("agent.ytdlp_opts")

_COOKIE_FILE = Path(os.getenv("AGENT_DATA_DIR", "/tmp/video_agent_data")) / "yt_cookies.txt"
_CHUNK_PREFIX = "YOUTUBE_COOKIES_B64_"


def _decode_b64(value: str) -> str:
    compact = "".join(value.split())
    compact += "=" * (-len(compact) % 4)
    return base64.b64decode(compact).decode("utf-8", "replace")


def _load_cookie_text() -> str:
    chunks = []
    for key, value in os.environ.items():
        suffix = key[len(_CHUNK_PREFIX):]
        if key.startswith(_CHUNK_PREFIX) and suffix.isdigit() and value.strip():
            chunks.append((int(suffix), value))
    if chunks:
        chunks.sort()
        return _decode_b64("".join(v for _, v in chunks))
    single = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
    if single:
        return _decode_b64(single)
    raw = os.getenv("YOUTUBE_COOKIES", "").strip()
    escaped_nl = chr(92) + "n"
    if escaped_nl in raw and chr(10) not in raw:
        raw = raw.replace(escaped_nl, chr(10))
    return raw


def apply_cookies(ydl_opts: dict) -> dict:
    """Return ydl_opts with a cookiefile set when cookie env vars are provided."""
    try:
        text = _load_cookie_text().strip()
    except Exception as exc:  # bad base64, etc.
        log.warning("Could not decode YouTube cookies from env: %s", exc)
        return ydl_opts
    if not text:
        if any(k.lower().startswith("youtube_com_") for k in os.environ):
            log.warning(
                "Found youtube_com_* variables (cookies.txt split at = signs). "
                "Replace them with YOUTUBE_COOKIES_B64_1..N; see .env.example."
            )
        else:
            log.info("No YouTube cookies configured (set YOUTUBE_COOKIES_B64_1..N).")
        return ydl_opts
    try:
        _COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _COOKIE_FILE.write_text(text + chr(10), encoding="utf-8")
        _COOKIE_FILE.chmod(0o600)
        ydl_opts["cookiefile"] = str(_COOKIE_FILE)
        log.info("YouTube cookies loaded: %d lines", len(text.splitlines()))
    except OSError as exc:
        log.warning("Could not write YouTube cookies file: %s", exc)
    return ydl_opts
