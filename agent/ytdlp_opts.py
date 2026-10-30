"""yt-dlp option helpers: YouTube cookie support.

YouTube blocks anonymous downloads from datacenter IPs ("Sign in to confirm
you are not a bot"). Provide a Netscape-format cookies file through the
YOUTUBE_COOKIES environment variable. Store it as a secret and never commit it.
Newlines may be real newlines or the two characters backslash + n.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("agent.ytdlp_opts")

_COOKIE_FILE = Path(os.getenv("AGENT_DATA_DIR", "/tmp/video_agent_data")) / "yt_cookies.txt"


def apply_cookies(ydl_opts: dict) -> dict:
    """Return ydl_opts with a cookiefile set when YOUTUBE_COOKIES is provided."""
    raw = os.getenv("YOUTUBE_COOKIES", "").strip()
    if not raw:
        return ydl_opts
    escaped_nl = chr(92) + "n"
    if escaped_nl in raw and chr(10) not in raw:
        raw = raw.replace(escaped_nl, chr(10))
    try:
        _COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _COOKIE_FILE.write_text(raw + chr(10), encoding="utf-8")
        _COOKIE_FILE.chmod(0o600)
        ydl_opts["cookiefile"] = str(_COOKIE_FILE)
    except OSError as exc:
        log.warning("Could not write YouTube cookies file: %s", exc)
    return ydl_opts
