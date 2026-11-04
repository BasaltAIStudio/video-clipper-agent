"""agent/worker.py — Miget worker for split architecture.

Run on Miget (free tier) - processes jobs pulled from Render's queue:
  - Polls Render /api/queue for pending jobs
  - Claims job via /api/dispatch
  - Downloads video (Scrapling + yt-dlp)
  - Transcribes (faster-whisper tiny model)
  - Selects highlights
  - Renders clip (ffmpeg)
  - Submits back to Content Rewards
  - Reports progress to Render /api/status

Environment variables:
  RENDER_URL - Render API base URL (http://localhost:8000)
  JOB_DB_PATH - Path to shared SQLite DB (default: /tmp/video_agent_data/jobs.db)
  AGENT_CACHE_DIR - Cache directory for downloads/transcripts
"""

from __future__ import annotations

import logging
import os
import sys
import time
import json
from pathlib import Path
from typing import Dict, Any, Optional

# Import agent modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from . import config as cfg
from .queue import (
    get_job_db, init_db, get_pending_jobs, update_job, get_job_status,
    get_job, dispatch_job, list_jobs,
)
from .clipper import render_clip, build_ass_subtitles, ClipError
from .transcriber import transcribe, get_model
from .ytdlp_opts import apply_cookies
import yt_dlp

try:
    from .highlight_selection import UnifiedHighlightSelector
    HAS_HIGHLIGHTS = True
except ImportError:
    HAS_HIGHLIGHTS = False

try:
    from .jev_decision import JVEDecisionEngine, ChoiceAnswer
    HAS_JEV = True
except ImportError:
    HAS_JEV = False

log = logging.getLogger("agent.worker")

# Configuration
RENDER_URL = os.getenv("RENDER_URL", "http://localhost:8000")
WORK_DIR = Path(os.getenv("AGENT_WORK_DIR", "/app/work"))
CACHE_DIR = Path(os.getenv("AGENT_CACHE_DIR", "/app/.cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Worker settings
POLL_INTERVAL = int(os.getenv("WORKER_POLL_INTERVAL", "30"))
MAX_RETRIES = int(os.getenv("WORKER_MAX_RETRIES", "3"))

# Session for HTTP to Render (if needed)
import requests
session = requests.Session()
session.headers.update({"User-Agent": "VideoClipper-MigetWorker/1.0"})


def _download_video(url: str, work_dir: Path, max_duration: int = 600) -> Path:
    """Download video using Scrapling HTTP fetcher + yt-dlp with cookies."""
    cache_key = f"source_video_{url.split('v=')[-1].split('&')[0]}.mp4"
    cache_file = work_dir / cache_key
    if cache_file.exists():
        log.info(f"Using cached video: {cache_file}")
        return cache_file

    log.info(f"Downloading video (max {max_duration}s, 1080p): {url}")

    # Try Scrapling HTTP fetcher first
    if 'SCRAPLING_AVAILABLE' in dir() and SCRAPLING_AVAILABLE:
        try:
            from scrapling.fetchers import Fetcher
            log.info("Using Scrapling Fetcher for download")
            page = Fetcher.get(url, impersonate="chrome")
            log.info(f"Scrapling Fetcher fetched page: {page.status}")

            ydl_opts = {
                'outtmpl': str(work_dir / 'source_video.%(ext)s'),
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
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
            from .ytdlp_opts import apply_cookies
            ydl_opts = apply_cookies(ydl_opts)

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            # Find the downloaded video
            for p in sorted(work_dir.glob("source_video.*")):
                if p.suffix.lower() in (".mp4", ".m4v"):
                    p.rename(cache_file)
                    return cache_file

            raise ClipError("yt-dlp produced no video file")

        except Exception as e:
            log.warning(f"Scrapling download failed: {e}, falling back to yt-dlp")

    # Fall back to yt-dlp
    ydl_opts = {
        'outtmpl': str(work_dir / 'source_video.%(ext)s'),
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
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
    ydl_opts = apply_cookies(ydl_opts)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            ydl.download([url])
        except Exception as e:
            raise ClipError(f"Download failed: {type(e).__name__}: {e}")

    for sub in work_dir.glob("source_video*.vtt"):
        sub_dest = cache_file.parent / f"{cache_file.stem}.en.vtt"
        try:
            import shutil
            shutil.copy2(sub, sub_dest)
        except Exception:
            pass

    for p in sorted(work_dir.glob("source_video.*")):
        if p.suffix.lower() in (".mp4", ".m4v"):
            if not cache_file.exists():
                p.rename(cache_file)
            return cache_file

    raise ClipError("yt-dlp produced no video file")


def _transcribe(video_path: Path, work_dir: Path) -> Path:
    """Transcribe video using faster-whisper (tiny model)."""
    log.info(f"Transcribing {video_path}")
    transcript_file = work_dir / f"{video_path.stem}.en.vtt"

    model_name = os.getenv("LOCAL_WHISPER_MODEL", "tiny.en")
    log.info(f"Loading faster-whisper model: {model_name}")

    try:
        transcribe(
            video_path,
            str(transcript_file),
            model_size=model_name,
            compute_type=os.getenv("LOCAL_WHISPER_COMPUTE_TYPE", "int8"),
            device=os.getenv("LOCAL_WHISPER_DEVICE", "cpu"),
            word_timestamps=os.getenv("LOCAL_WHISPER_WORD_TIMESTAMPS", "true").lower() == "true",
        )
        log.info(f"Transcription complete: {transcript_file}")
        return transcript_file
    except Exception as e:
        log.warning(f"Transcription failed: {e}")
        raise ClipError(f"Transcription failed: {e}")


def _render_clip(video_path: Path, transcript: Path, work_dir: Path,
                 start: float, end: float, output_path: Path) -> Path:
    """Render the highlight clip using ffmpeg."""
    log.info(f"Rendering clip: {start}-{end}s")

    # Generate ASS subtitles from transcript
    ass_content = build_ass_subtitles(transcript)
    ass_file = work_dir / f"{video_path.stem}.en.ass"
    ass_file.write_text(ass_content, encoding='utf-8')

    try:
        render_clip(
            video_path,
            start,
            end,
            output_path,
            subtitle=ass_file,
            aspect="9:16",
            target_width=int(os.getenv("TARGET_WIDTH", "1080")),
            output_format="mp4",
        )
        log.info(f"Clip rendered: {output_path}")
        return output_path
    except Exception as e:
        log.warning(f"Rendering failed: {e}")
        raise ClipError(f"Rendering failed: {e}")


def _select_highlights(video_path: Path, transcript: Path) -> list:
    """Select the best highlight segments from transcript."""
    segments = []
    if HAS_JEV:
        jev = JVEDecisionEngine()
        segments = jev.select_segments(video_path, transcript)
    else:
        # Fall back to heuristic scoring
        from .heuristic import score_segments
        segments = score_segments(video_path, transcript)
    return segments[:3]  # Max 3 clips


def process_job(job_id: str, youtube_url: str, campaign_id: str,
                campaign_name: str, aspect: str = "9:16", num_clips: int = 3) -> Dict:
    """Process a single job end-to-end.

    Steps:
    1. Download video
    2. Transcribe audio
    3. Select highlights (JEV/heuristic)
    4. Render clip (ffmpeg)
    5. Submit results to Render
    """
    work_dir = WORK_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"Processing job {job_id} for campaign {campaign_id}")
    update_job(job_id, "processing", stage="download", progress=5)

    # Step 1: Download video
    video_path = None
    try:
        video_path = _download_video(youtube_url, work_dir, max_duration=600)
        log.info(f"Downloaded video: {video_path}")
    except Exception as e:
        update_job(job_id, "failed", stage="download", error=str(e))
        return {"job_id": job_id, "status": "failed", "error": str(e)}

    update_job(job_id, "processing", stage="transcribe", progress=20)

    # Step 2: Transcribe
    transcript = None
    try:
        transcript = _transcribe(video_path, work_dir)
        log.info(f"Transcription complete: {transcript}")
    except Exception as e:
        update_job(job_id, "failed", stage="transcribe", error=str(e))
        return {"job_id": job_id, "status": "failed", "error": str(e)}

    update_job(job_id, "processing", stage="highlights", progress=40)

    # Step 3: Select highlights
    segments = []
    try:
        segments = _select_highlights(video_path, transcript)
        log.info(f"Selected {len(segments)} highlights")
    except Exception as e:
        log.warning(f"Highlight selection failed: {e}")
        segments = [(0, 10)]

    # Step 4: Render clips
    clips = []
    try:
        for i, (start, end) in enumerate(segments[:num_clips]):
            clip_path = work_dir / f"clip_{i+1}.mp4"
            clip = _render_clip(video_path, transcript, work_dir,
                               float(start), float(end), clip_path)
            clips.append({
                "clip_index": i + 1,
                "duration": float(end) - float(start),
                "path": str(clip_path),
            })
        log.info(f"Rendered {len(clips)} clips")
    except Exception as e:
        update_job(job_id, "failed", stage="render", error=str(e))
        return {"job_id": job_id, "status": "failed", "error": str(e)}

    update_job(job_id, "processing", stage="submit", progress=90)

    # Step 5: Submit results
    result = {
        "job_id": job_id,
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "clips": clips,
        "transcript_file": str(transcript),
        "processed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    update_job(job_id, "completed", stage="submit", progress=100,
               result=result)

    log.info(f"Job {job_id} completed successfully")
    return result


def worker_loop() -> None:
    """Main worker loop - poll for jobs and process them."""
    log.info(f"Worker started, polling Render at {RENDER_URL}")
    log.info(f"Work dir: {WORK_DIR}, Cache dir: {CACHE_DIR}")

    job_count = 0
    while True:
        try:
            # Get pending jobs
            pending = get_pending_jobs(limit=5)
            log.info(f"Pending jobs: {len(pending)}")

            for job in pending:
                job_id = job["job_id"]
                log.info(f"Claiming job {job_id}")
                dispatched = dispatch_job(job_id, RENDER_URL)
                if not dispatched:
                    log.warning(f"Job {job_id} already claimed by another worker")
                    continue

                job_count += 1
                log.info(f"Processing job {job_id} (#{job_count})")
                try:
                    result = process_job(
                        job_id, job["youtube_url"],
                        job["campaign_id"], job["campaign_name"],
                        job.get("aspect", "9:16"), job.get("num_clips", 3)
                    )
                    log.info(f"Job {job_id} done: {result}")
                except Exception as e:
                    log.error(f"Job {job_id} exception: {e}")
                    update_job(job_id, "failed", stage="worker", error=str(e))

        except Exception as e:
            log.error(f"Worker loop error: {e}")

        time.sleep(POLL_INTERVAL)


def main() -> None:
    """Entry point."""
    init_db()
    worker_loop()


if __name__ == "__main__":
    main()
