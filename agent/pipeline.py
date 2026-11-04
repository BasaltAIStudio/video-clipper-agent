from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import contextlib
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .boundary import adjust_window
from .clipper import ClipError, render_clip, build_ass_subtitles
from .config import (
    AGENT_CACHE_DIR, DATA_DIR, OUTPUT_DIR, DEFAULT_NUM_CLIPS, MAX_CLIP_DURATION, MAX_NUM_CLIPS,
    logger_config, PROVIDER_ORDER, USE_LLM, DEFAULT_MAX_DOWNLOAD_SECONDS,
    CAMPAIGN_WEBHOOK_URL,
)
from .contentrewards_scraper import ContentRewardsScraper
from .jev_decision import ChoiceAnswer, JVEDecisionEngine
from .heuristic import score_segments as score_all_segments
from .highlight_selection import UnifiedHighlightSelector
from .learning_loop import LearningLoop
from .llm import prompt_highlights
from .llm_retry import LLMClient, LLMProvider, RetryConfig
from .profiler import PipelineProfiler
from .controller import get_candidate_highlights
from .transcriber import transcribe, get_model
from .ytdlp_opts import apply_cookies, YTDLP_COOKIES_B64


# Imports for Scrapling - only used if available (lazy import to avoid startup issues)
def _get_scrapling():
    """Lazy import Scrapling to avoid startup failures."""
    try:
        from scrapling.fetchers import StealthySession
        return StealthySession, True
    except ImportError:
        return None, False

StealthySession, SCRAPLING_AVAILABLE = _get_scrapling()


# Imports for Scrapling - only used if available
try:
    from scrapling.fetchers import StealthySession
    SCRAPLING_AVAILABLE = True
except ImportError:
    SCRAPLING_AVAILABLE = False
    StealthySession = None

# Optional Scrapling-specific imports
try:
    from scrapling.parser import Selector
    SCRAPLING_PARSER_AVAILABLE = True
except ImportError:
    SCRAPLING_PARSER_AVAILABLE = False
    Selector = None

# ---------------------------------------------------------------------------
# Download helper using Scrapling for anti-bot bypass
# ---------------------------------------------------------------------------
def _download_video(url: str, work_dir: Path, max_duration: int = 600) -> Path:
    """Download video using Scrapling's StealthySession for anti-bot bypass.
    
    Uses browser automation + cookies to avoid YouTube's bot detection.
    Falls back to yt-dlp if Scrapling is not available.
    """
    cache_key = f"source_video_{url.split('v=')[-1].split('&')[0]}.mp4"
    cache_file = work_dir / cache_key
    if cache_file.exists():
        log.info(f"Using cached video: {cache_file}")
        return cache_file

    log.info(f"Downloading video (max {max_duration}s, 1080p): {url}")

    # Try Scrapling first if available
    if SCRAPLING_AVAILABLE and StealthySession:
        try:
            # First, fetch the YouTube page with Scrapling to get cookies
            with StealthySession(headless=True, solve_cloudflare=False) as session:
                page = session.fetch(url, network_idle=True)
                log.info(f"Scrapling fetched page: {page.status}")

                # Get the video info using yt-dlp (with Scrapling's browser fingerprint)
                ydl_opts = {
                    'outtmpl': str(work_dir / 'source_video.%(ext)s'),
                    'format': 'bestvideo+bestaudio/best',
                    'merge_output_format': 'mp4',
                    'quiet': True,
                    'noprogress': True,
                    'fragment_retries': 3,
                    'retries': 5,
                    'writesubtitles': True,
                    'writeautomaticsub': True,
                    'subtitleslangs': ['en', 'en-US', 'en-GB'],
                    'subtitlesformat': 'vtt',
                }

                # Apply cookies from ytdlp_opts
                from .ytdlp_opts import apply_cookies
                ydl_opts = apply_cookies(ydl_opts)

                # Use the cookies for the download
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    try:
                        ydl.download([url])
                    except Exception as e:
                        raise ClipError(f"Download failed: {type(e).__name__}: {e}")

                # Copy subtitles
                for sub in work_dir.glob("source_video*.vtt"):
                    sub_dest = cache_file.parent / f"{cache_file.stem}.en.vtt"
                    try:
                        import shutil as _shutil
                        _shutil.copy2(sub, sub_dest)
                    except Exception:
                        pass

                # Move video file
                for p in sorted(work_dir.glob("source_video.*")):
                    if p.suffix.lower() in (".mp4", ".m4v"):
                        if not cache_file.exists():
                            p.rename(cache_file)
                        return cache_file

                raise ClipError("yt-dlp produced no video file")

        except Exception as e:
            log.warning(f"Scrapling download failed: {e}, falling back to yt-dlp")
            # Fall through to yt-dlp

    # Fall back to yt-dlp (original method)
    ydl_opts = {
        'outtmpl': str(work_dir / 'source_video.%(ext)s'),
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'quiet': True,
        'noprogress': True,
        'fragment_retries': 3,
        'retries': 5,
    }
    ydl_opts['writesubtitles'] = True
    ydl_opts['writeautomaticsub'] = True
    ydl_opts['subtitleslangs'] = ['en', 'en-US', 'en-GB']
    ydl_opts['subtitlesformat'] = 'vtt'
    from .ytdlp_opts import apply_cookies
    ydl_opts = apply_cookies(ydl_opts)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            ydl.download([url])
        except Exception as e:
            raise ClipError(f"Download failed: {type(e).__name__}: {e}")

    for sub in work_dir.glob("source_video*.vtt"):
        sub_dest = cache_file.parent / f"{cache_file.stem}.en.vtt"
        try:
            import shutil as _shutil
            _shutil.copy2(sub, sub_dest)
        except Exception:
            pass

    for p in sorted(work_dir.glob("source_video.*")):
        if p.suffix.lower() in (".mp4", ".m4v"):
            if not cache_file.exists():
                p.rename(cache_file)
            return cache_file

    raise ClipError("yt-dlp produced no video file")