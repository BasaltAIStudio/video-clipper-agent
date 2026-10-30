"""agent/pipeline.py - decision engine for Content Rewards clip submission.

The agent is a DECISION ENGINE with a closed feedback loop:

  1. Download video (cached, parallel transcribe)
  2. Transcribe (fast, cached, chunked, parallel model load)
  3. Generate candidate highlights (A/B-tested method selection)
  4. Render clips (with real video path - FIX #5)
  5. POST to Content Rewards via n8n
  6. Poll performance data from Content Rewards scraper (FIX #2)
  7. Record performance data in SQLite (the analytics loop)
  8. TRAIN model on actual performance data (retention curves + engagement) - FIX #1
  9. RETRAIN next run for better highlight selection (A/B tested) - FIX #3

The agent learns to predict RETENTION (not "virality").
Idempotency: clip IDs stored in SQLite. If already submitted, skip.
Continuous loop: run -> train -> resend, repeat.
"""
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

import yt_dlp
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
from .retention import RetentionCurvePredictor
from .transcriber import transcribe

log = logger_config('agent.pipeline')

# ---------------------------------------------------------------------------
# Idempotency Cache (SQLite)
# ---------------------------------------------------------------------------

class IdempotencyCache:
    """Durable cache of submitted clip IDs. Skips re-submission of identical clips."""

    def __init__(self, cache_dir: Path = AGENT_CACHE_DIR):
        self.cache_dir = Path(cache_dir)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.cache_dir = DATA_DIR / "cache"
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.cache_dir / "submitted_clips.db"
        self._init_db()

    def _init_db(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS submitted_clips (
                    clip_id TEXT PRIMARY KEY,
                    campaign_id TEXT,
                    campaign_name TEXT,
                    video_url TEXT,
                    youtube_url TEXT,
                    submitted_at TEXT,
                    status TEXT DEFAULT 'submitted'
                )
            """)
            conn.commit()

    def is_submitted(self, clip_id: str) -> bool:
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT status FROM submitted_clips WHERE clip_id = ?", (clip_id,)
            ).fetchone()
            return row is not None

    def record_submission(
        self,
        clip_id: str,
        campaign_id: str,
        campaign_name: str,
        video_url: str,
        youtube_url: Optional[str] = None,
    ):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO submitted_clips
                   (clip_id, campaign_id, campaign_name, video_url, youtube_url, submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (clip_id, campaign_id, campaign_name, video_url, youtube_url,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

    def get_all(self) -> List[Dict[str, Any]]:
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT clip_id, campaign_id, campaign_name, video_url, youtube_url, submitted_at, status FROM submitted_clips"
            ).fetchall()
        return [
            {
                "clip_id": r[0], "campaign_id": r[1], "campaign_name": r[2],
                "video_url": r[3], "youtube_url": r[4], "submitted_at": r[5],
                "status": r[6],
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Posted Videos Tracker - Persistent tracking of all posted videos
# ---------------------------------------------------------------------------

class PostedVideosTracker:
    """Tracks all posted videos across platforms for monitoring and payment eligibility."""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DATA_DIR / "posted_videos.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS posted_videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    clip_id TEXT UNIQUE,
                    campaign_id TEXT,
                    campaign_name TEXT,
                    platform TEXT,
                    video_url TEXT,
                    public_url TEXT,
                    posted_at TEXT DEFAULT (datetime('now')),
                    views INTEGER DEFAULT 0,
                    earnings REAL DEFAULT 0.0,
                    cpm REAL DEFAULT 0.0,
                    status TEXT DEFAULT 'posted',
                    payment_threshold_met INTEGER DEFAULT 0,
                    notified INTEGER DEFAULT 0,
                    last_checked TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_posted_videos_campaign
                ON posted_videos(campaign_id, platform)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_posted_videos_status
                ON posted_videos(status, payment_threshold_met)
            """)
            conn.commit()

    def record_posted(
        self,
        clip_id: str,
        campaign_id: str,
        campaign_name: str,
        platform: str,
        public_url: str,
        video_url: str = None,
    ):
        """Record that a video was posted to a platform."""
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO posted_videos
                   (clip_id, campaign_id, campaign_name, platform, video_url, public_url)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (clip_id, campaign_id, campaign_name, platform, video_url, public_url),
            )
            conn.commit()
        log.info(f"Recorded posted video: {platform} - {public_url}")

    def update_performance(
        self,
        clip_id: str,
        views: int = None,
        earnings: float = None,
        cpm: float = None,
        status: str = None,
    ):
        """Update performance metrics for a posted video."""
        updates = []
        params = []
        if views is not None:
            updates.append("views = ?")
            params.append(views)
        if earnings is not None:
            updates.append("earnings = ?")
            params.append(earnings)
        if cpm is not None:
            updates.append("cpm = ?")
            params.append(cpm)
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        updates.append("last_checked = datetime('now')")
        params.append(clip_id)

        if not updates:
            return

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                f"UPDATE posted_videos SET {', '.join(updates)} WHERE clip_id = ?",
                params,
            )
            conn.commit()

    def mark_payment_threshold_met(self, clip_id: str, notified: bool = False):
        """Mark that a video has met the payment threshold."""
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE posted_videos SET payment_threshold_met = 1, notified = ? WHERE clip_id = ?",
                (1 if notified else 0, clip_id),
            )
            conn.commit()

    def get_unnotified_threshold_met(self) -> List[Dict]:
        """Get videos that met payment threshold but haven't been notified."""
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                """SELECT clip_id, campaign_id, campaign_name, platform, public_url,
                          views, earnings, cpm
                   FROM posted_videos
                   WHERE payment_threshold_met = 1 AND notified = 0"""
            ).fetchall()
        return [
            {
                "clip_id": r[0], "campaign_id": r[1], "campaign_name": r[2],
                "platform": r[3], "public_url": r[4], "views": r[5],
                "earnings": r[6], "cpm": r[7],
            }
            for r in rows
        ]

    def get_all_posted(self, limit: int = 100) -> List[Dict]:
        """Get all posted videos."""
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                """SELECT clip_id, campaign_id, campaign_name, platform, public_url,
                          views, earnings, cpm, posted_at, status
                   FROM posted_videos
                   ORDER BY posted_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {
                "clip_id": r[0], "campaign_id": r[1], "campaign_name": r[2],
                "platform": r[3], "public_url": r[4], "views": r[5],
                "earnings": r[6], "cpm": r[7], "posted_at": r[8], "status": r[9],
            }
            for r in rows
        ]

    def get_needs_checking(self, hours: int = 24) -> List[Dict]:
        """Get videos that haven't been checked recently."""
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                """SELECT clip_id, campaign_id, campaign_name, platform, public_url,
                          views, earnings, cpm, last_checked
                   FROM posted_videos
                   WHERE last_checked IS NULL
                      OR datetime(last_checked) < datetime('now', ? || ' hours')
                   ORDER BY posted_at DESC""",
                (-hours,),
            ).fetchall()
        return [
            {
                "clip_id": r[0], "campaign_id": r[1], "campaign_name": r[2],
                "platform": r[3], "public_url": r[4], "views": r[5],
                "earnings": r[6], "cpm": r[7], "last_checked": r[8],
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Performance Recording (the analytics feedback loop)
# ---------------------------------------------------------------------------

class PerformanceRecorder:
    """
    Records actual performance data from Content Rewards and
    makes it available for model training.

    Core loop: submit clips -> poll Content Rewards -> record views/earnings
    -> train model -> improve highlight selection next run
    """

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DATA_DIR / "performance.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS clip_performance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    clip_id TEXT UNIQUE,
                    campaign_id TEXT,
                    campaign_name TEXT,
                    video_url TEXT,
                    submission_date TEXT,
                    views INTEGER DEFAULT 0,
                    completed_views INTEGER DEFAULT 0,
                    watch_time_seconds REAL DEFAULT 0,
                    earnings REAL DEFAULT 0.0,
                    cpm REAL DEFAULT 0.0,
                    engagement_rate REAL DEFAULT 0.0,
                    retention_score REAL DEFAULT 0.0,
                    features_json TEXT,
                    retention_curve_json TEXT,
                    notes TEXT,
                    recorded_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_clip_performance_campaign
                ON clip_performance(campaign_id, submission_date)
            """)
            conn.commit()

    def record_performance(
        self,
        clip_id: str,
        campaign_id: str,
        campaign_name: str,
        video_url: str,
        views: int,
        completion_rate: float,
        earnings: float,
        cpm: float,
        watch_time: float = 0.0,
        features: Optional[Dict[str, Any]] = None,
        retention_curve: Optional[List[float]] = None,
    ):
        """
        Record actual performance data for a submitted clip.

        Called after n8n submission returns, or when the Content Rewards
        scraper polls and returns actual metrics.
        """
        completion = max(0.0, min(1.0, completion_rate))
        engagement = views * completion_rate if completion_rate > 0 else 0.0
        retention_score = (views * cpm / 1000) * 0.7 + (engagement * 0.3)

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO clip_performance
                   (clip_id, campaign_id, campaign_name, video_url,
                    views, completed_views, watch_time_seconds, earnings,
                    cpm, engagement_rate, retention_score, features_json,
                    retention_curve_json, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    clip_id, campaign_id, campaign_name, video_url,
                    views, int(views * completion), watch_time, earnings,
                    cpm, completion, retention_score,
                    json.dumps(features) if features else None,
                    json.dumps(retention_curve) if retention_curve else None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        log.info(f"Recorded performance for {clip_id}: views={views}, "
                 f"earnings={earnings:.4f}, retention_score={retention_score:.1f}")

    def get_training_data(self, min_views: int = 5, min_earnings: float = 0.0) -> tuple:
        """Get training data for the performance model."""
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                """SELECT features_json, retention_score, views, earnings, cpm
                   FROM clip_performance
                   WHERE views >= ? AND earnings >= ?
                   ORDER BY submission_date DESC
                   LIMIT 200""",
                (min_views, min_earnings),
            ).fetchall()

        if not rows:
            log.warning("No performance data available for training")
            return [], [], []

        features = []
        labels = []
        metadata = []
        for feat_json, retention, views, earnings, cpm in rows:
            try:
                feat = json.loads(feat_json) if feat_json else {}
                features.append(feat)
                labels.append(float(retention))
                metadata.append({"views": views, "earnings": earnings, "cpm": cpm})
            except json.JSONDecodeError:
                continue

        log.info(f"Training data: {len(features)} clips, "
                 f"views={sum(m['views'] for m in metadata)}, "
                 f"earnings={sum(m['earnings'] for m in metadata):.2f}")
        return features, labels, metadata

    def get_latest_performance(self, campaign_id: str) -> dict:
        """Get the most recent performance data for a campaign."""
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                """SELECT views, completed_views, watch_time_seconds, earnings,
                          cpm, engagement_rate, retention_score
                   FROM clip_performance
                   WHERE campaign_id = ?
                   ORDER BY submission_date DESC
                   LIMIT 1""",
                (campaign_id,),
            ).fetchone()
        if row:
            return {
                "views": row[0], "completed_views": row[1], "watch_time": row[2],
                "earnings": row[3], "cpm": row[4], "engagement_rate": row[5],
                "retention_score": row[6],
            }
        return {}


# ---------------------------------------------------------------------------
# Campaign Scraper + Selector - Agent-side campaign discovery
# ---------------------------------------------------------------------------

class CampaignScraperAndSelector:
    """
    Agent-side campaign discovery and selection.
    
    Scrapes Content Rewards discover page, extracts campaign details,
    and selects campaigns worth pursuing based on payout, platforms, and CPM.
    """

    def __init__(self, scraper: ContentRewardsScraper = None):
        self.scraper = scraper or ContentRewardsScraper()
        self._login_attempted = False

    def ensure_login(self) -> bool:
        """Attempt login if credentials are available (one-time)."""
        if self._login_attempted:
            return self.scraper._logged_in
        self._login_attempted = True
        return self.scraper.login()

    def discover_and_select(
        self,
        tab: str = "All",
        min_cpm: float = 0.0,
        require_platforms: List[str] = None,
        max_campaigns: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Full campaign discovery pipeline:
        1. Optionally login
        2. Scrape discover page for campaign URLs
        3. Scrape each campaign detail page
        4. Filter by CPM, platforms, etc.
        5. Return ranked list of campaigns worth pursuing
        
        Returns list of campaign dicts ready for clip processing.
        """
        # Attempt login if credentials available
        self.ensure_login()

        # Step 1: Scrape discover page
        try:
            campaigns = self.scraper.scrape_discover_page(tab=tab)
        except ScrapingError as e:
            log.error(f"Discover page scrape failed: {e}")
            return []

        if not campaigns:
            log.warning("No campaigns found on discover page")
            return []

        log.info(f"Found {len(campaigns)} campaigns, scraping details...")

        # Step 2: Scrape details for each campaign
        detailed = []
        for campaign in campaigns:
            try:
                detail = self.scraper.scrape_campaign_details(campaign)
                detailed.append(detail)
            except Exception as e:
                log.warning(f"Failed to scrape details for {campaign.get('url')}: {e}")
                continue

        log.info(f"Successfully scraped {len(detailed)} campaign details")

        # Step 3: Filter and rank
        require_platforms = require_platforms or ["YouTube", "TikTok", "Instagram"]
        filtered = []

        for camp in detailed:
            cpm = camp.get("cpm") or 0
            platforms = camp.get("allowed_platforms", [])

            # Filter by minimum CPM
            if cpm < min_cpm:
                continue

            # Filter by required platforms (at least one match)
            if require_platforms and not any(p in platforms for p in require_platforms):
                continue

            # Skip campaigns with errors
            if camp.get("error"):
                continue

            # Score: higher CPM + more platforms = better
            score = cpm + (len(platforms) * 0.5)
            camp["selection_score"] = score
            camp["selected"] = True
            filtered.append(camp)

        # Sort by score descending
        filtered.sort(key=lambda c: c.get("selection_score", 0), reverse=True)

        selected = filtered[:max_campaigns]
        log.info(f"Selected {len(selected)} campaigns after filtering")
        for c in selected:
            log.info(f"  - {c.get('name', 'unknown')}: CPM=${c.get('cpm')}, "
                     f"platforms={c.get('allowed_platforms')}, score={c.get('selection_score'):.1f}")

        return selected


# ---------------------------------------------------------------------------
# Download + Transcribe (parallel model load)
# ---------------------------------------------------------------------------

def _download_video(url: str, work_dir: Path, max_duration: int = 600) -> Path:
    """Download video using yt-dlp with cache.

    Downloads the full video at 1080p (high quality) with speed optimizations:
      - bestvideo[height<=1080] ensures highest quality up to 1080p
      - concurrent_fragment_downloads=8 for parallel segment download
      - download_ranges capped at max_duration (600s default) as safety valve
    """
    cache_key = f"source_video_{url.split('v=')[-1].split('&')[0]}.mp4"
    cache_file = work_dir / cache_key
    if cache_file.exists():
        log.info(f"Using cached video: {cache_file}")
        return cache_file

        log.info(f"Downloading video (max {max_duration}s, 1080p): {url}")
    ydl_opts = {
        'outtmpl': str(work_dir / 'source_video.%(ext)s'),
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'quiet': True,
        'noprogress': True,
        'fragment_retries': 3,
        'retries': 5,
    }
    from .ytdlp_opts import apply_cookies
    ydl_opts = apply_cookies(ydl_opts)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            ydl.download([url])
        except Exception as e:
            raise ClipError(f"Download failed: {type(e).__name__}: {e}")

    for p in sorted(work_dir.glob("source_video.*")):
        if p.suffix.lower() in (".mp4", ".m4v"):
            if not cache_file.exists():
                p.rename(cache_file)
            return cache_file
    raise ClipError("yt-dlp produced no video file")



# ---------------------------------------------------------------------------
# Highlight Selection (A/B tested via UnifiedHighlightSelector)
# ---------------------------------------------------------------------------

def _get_highlights_ab(
    segments,
    campaign_id: str,
    campaign_name: str,
    num_clips: int = 3,
    use_llm: bool = True,
    client=None,
    model: str = None,
    llm_client: LLMClient = None,
) -> List[Dict]:
    """
    Generate highlights using A/B-tested method selection.

    Uses UnifiedHighlightSelector which:
    - Evaluates each method's historical performance (FIX #3)
    - Selects the best method dynamically per campaign
    - Logs method selection for later A/B analysis
    - Falls back through methods when the best fails
    """
    selector = UnifiedHighlightSelector()

    best_method = selector.get_best_method(campaign_id, fallback_method="heuristic")

    if use_llm and llm_client is None and USE_LLM:
        providers = []
        for pc in PROVIDER_ORDER:
            if pc["api_key"]:
                providers.append(LLMProvider(
                    name=pc.get("provider", "openrouter"),
                    api_key=pc["api_key"],
                    model=pc["model"],
                    base_url=pc["base_url"],
                ))
        if providers:
            llm_client = LLMClient(
                providers,
                RetryConfig(max_retries=3, base_delay=2.0, max_delay=30.0),
            )

    highlights = selector.select(
        segments,
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        num_clips=num_clips,
        use_llm=use_llm and llm_client is not None,
        client=llm_client,
        model=model,
    )

    selector.log_method_selection(
        campaign_id, campaign_name,
        best_method, num_clips, highlights,
        performance_summary=None,
    )
    return highlights


def _deduplicate(candidates: List[Dict], num_clips: int) -> List[Dict]:
    """Greedily select non-overlapping highlights (max 50% overlap)."""
    sorted_candidates = sorted(candidates, key=lambda h: h.get('score', 0), reverse=True)
    selected = []
    for hl in sorted_candidates:
        if len(selected) >= num_clips:
            break
        overlap = False
        for k in selected:
            ov = min(hl["end"], k["end"]) - max(hl["start"], k["start"])
            min_dur = min(hl.get("end", 0) - hl.get("start", 0),
                          k.get("end", 0) - k.get("start", 0))
            if min_dur > 0 and ov / min_dur > 0.5:
                overlap = True
                break
        if not overlap:
            selected.append(hl)
    return selected


def _render_clips(
    video_path: Path,
    segments,
    highlights,
    work_dir: Path,
    num_clips: int = 3,
    min_duration: float = 15.0,
    max_duration: float = 180.0,
):
    """Render clips from highlights using ffmpeg (parallelized)."""
    log.info(f"Rendering {num_clips} clips from {video_path}...")
    clips = []

    sorted_highlights = sorted(highlights, key=lambda h: h.get('score', 0), reverse=True)

    def _render_one(i, hl):
        start = hl.get('start', 0)
        end = hl.get('end', start + 30)
        clip_duration = min(end - start, max_duration)
        clip_duration = max(clip_duration, min_duration)

        prefix = f"{int(time.time())}_{i}_"
        work_file = work_dir / f"{prefix}clip.mp4"
        ass_file = work_dir / f"{prefix}clip.ass"

        seg = None
        try:
            seg_dicts = [x if isinstance(x, dict) else {"start": x.start, "end": x.end, "text": x.text}
                         for x in segments]
            ass_text = build_ass_subtitles(seg_dicts, start, start + clip_duration, font_size=64)
            if "Dialogue:" in ass_text:
                ass_file.write_text(ass_text, encoding="utf-8")
                seg = True
        except Exception as e:
            log.warning("caption generation failed for clip %s: %s", i, e)

        render_clip(
            source_path=video_path,
            start=start,
            end=start + clip_duration,
            output_path=work_file,
            aspect="9:16",
            captions_ass=ass_file if seg else None,
        )
        return {
            "clip_id": f"{prefix}clip",
            "start": start,
            "end": start + clip_duration,
            "duration": clip_duration,
            "video_url": f"file://{work_file}",
            "file_name": work_file.name,
            "public_path": f"/clips/{work_file.name}",
            "ass_url": f"file://{ass_file}",
            "score": hl.get('score', 0),
            "text": hl.get('text', ''),
            "source_method": hl.get('source_method', 'unknown'),
            "source_video": str(video_path),
        }

    with ThreadPoolExecutor(max_workers=min(num_clips, 4)) as pool:
        futures = [pool.submit(_render_one, i, hl) for i, hl in enumerate(sorted_highlights[:num_clips])]
        for f in futures:
            try:
                clips.append(f.result())
            except ClipError as e:
                log.warning(f"Failed to render clip: {e}")

    log.info(f"Rendered {len(clips)} clips")
    return clips


def _post_to_n8n(manifest: dict, n8n_url: str, cache: IdempotencyCache = None):
    """POST manifest to n8n for YouTube upload and Content Rewards submission."""
    import requests
    log.info(f"POSTing manifest to n8n: {n8n_url}")
    response = requests.post(
        n8n_url,
        json=manifest,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    log.info(f"n8n response: {result}")
    return result


class PipelineStats:
    """Track pipeline execution statistics."""

    def __init__(self):
        self.total_runs = 0
        self.successful_runs = 0
        self.failed_runs = 0
        self.total_duration = 0.0
        self.last_run_duration = 0.0
        self.last_campaign_id = None
        self.last_campaign_name = None
        self.last_llm_provider = None
        self.last_num_clips = 0
        self.last_clip_count = 0
        self.errors = []

    def record_run(self, success, duration, campaign_id=None,
                   campaign_name=None, llm_provider=None,
                   num_clips=0, clip_count=0, error=None):
        self.total_runs += 1
        self.last_run_duration = duration
        self.last_campaign_id = campaign_id
        self.last_campaign_name = campaign_name
        self.last_llm_provider = llm_provider
        self.last_num_clips = num_clips
        self.last_clip_count = clip_count
        if error:
            self.errors.append(error)
        if success:
            self.successful_runs += 1
            self.total_duration += duration
        else:
            self.failed_runs += 1

    def get_summary(self):
        return {
            "total_runs": self.total_runs,
            "successful_runs": self.successful_runs,
            "failed_runs": self.failed_runs,
            "success_rate": round(self.successful_runs / max(self.total_runs, 1) * 100, 1),
            "avg_duration": round(self.total_duration / max(self.successful_runs, 1) / 1, 2),
            "last_run_duration": self.last_run_duration,
            "last_campaign_id": self.last_campaign_id,
            "last_campaign_name": self.last_campaign_name,
            "last_llm_provider": self.last_llm_provider,
            "last_num_clips": self.last_num_clips,
            "last_clip_count": self.last_clip_count,
            "error_count": len(self.errors),
        }


class DecisionEngine:
    """
    The agent is a DECISION ENGINE with a closed feedback loop.

    Core loop:
      1. Get video URL + campaign info
      2. Download + transcribe (fast, cached, chunked, parallel)
      3. Generate candidate highlights (A/B-tested method selection)
      4. Render clips (with real video path)
      5. Submit to Content Rewards via n8n
      6. Poll for performance data (views, completion, earnings)
      7. Record performance data (the analytics loop)
      8. TRAIN the model on actual performance data
      9. Use the model to select better highlights next run

    The agent is self-improving: every run produces data that makes
    the next run smarter about which clips retain viewers.
    """

    def __init__(
        self,
        n8n_url: str = "",
        cache: IdempotencyCache = None,
        perf_recorder: PerformanceRecorder = None,
        posted_videos_tracker: PostedVideosTracker = None,
        highlight_selector: UnifiedHighlightSelector = None,
        learning_loop: LearningLoop = None,
        llm_client: LLMClient = None,
        llm_model=None,
        output_dir: Path = None,
        webhook_url: str = None,
    ):
        self.n8n_url = n8n_url
        self.webhook_url = webhook_url or CAMPAIGN_WEBHOOK_URL
        self.cache = cache or IdempotencyCache()
        self.perf_recorder = perf_recorder or PerformanceRecorder()
        self.posted_videos = posted_videos_tracker or PostedVideosTracker()
        self.highlight_selector = highlight_selector or UnifiedHighlightSelector()
        self.learning_loop = learning_loop or LearningLoop()
        self.llm_client = llm_client
        self.llm_model = llm_model
        self.output_dir = output_dir or OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.stats = PipelineStats()
        self.profiler = PipelineProfiler()
        self.retention_predictor = RetentionCurvePredictor()
        self.training_data = []
        self.campaign_scraper = CampaignScraperAndSelector()

    def run(self, url: str, campaign_id: str, campaign_name: str,
            num_clips: int = DEFAULT_NUM_CLIPS,
            use_llm: bool = True,
            use_jev: bool = False,
            use_heuristic: bool = True,
            n8n_url: str = None,
            model_size: str = "tiny",
            chunked: bool = True,
            work_dir: Path = None,
            poll_performance: bool = True,
            skip_download: bool = False,
            max_download_seconds: int = DEFAULT_MAX_DOWNLOAD_SECONDS,
            **kwargs) -> dict:
        """
        Run the decision engine cycle with full profiling and feedback loop.

        Returns manifest with clips and performance tracking data.

        If skip_download is True, the url parameter is treated as a local
        file path and the yt-dlp download step is skipped entirely.
        """
        self.profiler.start()
        stats = PipelineStats()
        start_time = time.time()

        log.info(f"DecisionEngine: url={url}, campaign={campaign_name}, "
                 f"num_clips={num_clips}, use_llm={use_llm}, use_jev={use_jev}, "
                 f"skip_download={skip_download}, max_download_seconds={max_download_seconds}")

        if work_dir is None:
            work_dir = Path("/tmp/video_agent") / f"campaign_{campaign_id}" / \
                datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        work_dir.mkdir(parents=True, exist_ok=True)

        campaign_output = self.output_dir
        campaign_output.mkdir(parents=True, exist_ok=True)

        video_path = None
        download_dir = None
        try:
            if skip_download:
                log.info("Step 1: Skipping download (using local file)...")
                video_path = Path(url)
                if not video_path.exists():
                    raise ClipError(f"Local video file not found: {url}")
            else:
                log.info("Step 1: Downloading video...")
                download_dir = work_dir / "download"
                download_dir.mkdir(parents=True, exist_ok=True)
                video_path = self.profiler.profile(
                    _download_video, url, download_dir, max_duration=max_download_seconds,
                    step_name="download",
                )

            try:
                log.info("Step 2: Transcribing (parallel model load)...")
                info = self.profiler.profile(
                    transcribe, video_path,
                    language="en", word_timestamps=True,
                    model_size=model_size, use_cache=True, chunked=chunked,
                    step_name="transcribe",
                )
                segments = info["segments"]
            except Exception as e:
                raise ClipError(f"transcribe step failed: {type(e).__name__}: {e}")

            try:
                log.info("Step 3: Generating candidate highlights (A/B tested)...")
                candidates = self.profiler.profile(
                    _get_highlights_ab, segments, campaign_id, campaign_name,
                    num_clips=num_clips, use_llm=use_llm,
                    client=self.llm_client, model=self.llm_model,
                    llm_client=self.llm_client,
                    step_name="highlight",
                )
            except Exception as e:
                raise ClipError(f"highlight step failed: {type(e).__name__}: {e}")

            candidates = _deduplicate(candidates, num_clips)
            log.info(f"Generated {len(candidates)} candidate highlights")

            try:
                log.info("Step 4: Rendering clips...")
                clips = self.profiler.profile(
                    _render_clips, video_path, segments, candidates,
                    campaign_output, num_clips=num_clips,
                    step_name="render",
                )
            except Exception as e:
                raise ClipError(f"render step failed: {type(e).__name__}: {e}")

            log.info("Step 5: Submitting to Content Rewards via n8n...")
            manifest = {
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
                "url": url,
                "transcript_duration": info.get("duration", 0),
                "num_clips": len(clips),
                "clips": clips,
                "cpm": kwargs.get("cpm", 0.0),
                "submission_method": "decision_engine",
                "submission_at": datetime.now(timezone.utc).isoformat(),
                "manifest_version": "1.0",
            }

            n8n_url = n8n_url or self.n8n_url
            n8n_result = None
            if n8n_url:
                n8n_result = self.profiler.profile(
                    _post_to_n8n, manifest, n8n_url, self.cache,
                    step_name="n8n",
                )
                self.cache.record_submission(
                    clip_id=str(manifest["campaign_id"]),
                    campaign_id=manifest["campaign_id"],
                    campaign_name=manifest["campaign_name"],
                    video_url=url,
                    youtube_url=n8n_result.get("youtube_url") if n8n_result else None,
                )

            log.info("Step 6: Recording performance data...")
            for clip in clips:
                features = {
                    "start": clip["start"],
                    "end": clip["end"],
                    "duration": clip["duration"],
                    "text_length": len(clip.get("text", "")),
                    "source_method": clip.get("source_method", "unknown"),
                    "score": clip.get("score", 0),
                }
                self.learning_loop.log_clip(
                    clip,
                    cpm=kwargs.get("cpm", 0.0),
                    features=list(features.values()),
                    selected_method=clip.get("source_method", "unknown"),
                )
                self.perf_recorder.record_performance(
                    clip_id=clip["clip_id"],
                    campaign_id=campaign_id,
                    campaign_name=campaign_name,
                    video_url=url,
                    views=0,
                    completion_rate=0.0,
                    earnings=0.0,
                    cpm=kwargs.get("cpm", 0.0),
                    watch_time=0.0,
                    features=features,
                    retention_curve=None,
                )

            if poll_performance:
                log.info("Step 6b: Polling Content Rewards scraper for performance...")
                self._poll_content_rewards(campaign_id, campaign_name, url, clips)

            log.info("Step 7: Training model on performance data...")
            self.update_model()

            duration = time.time() - start_time
            stats.record_run(
                success=True, duration=duration,
                campaign_id=campaign_id, campaign_name=campaign_name,
                llm_provider=("llm" if use_llm and self.llm_client else "heuristic"),
                num_clips=num_clips, clip_count=len(clips),
            )

            self.profiler.stop()
            report = self.profiler.report()
            log.info(f"Pipeline bottleneck: {report.get('bottleneck', 'unknown')} "
                     f"({report.get('bottleneck_percent', 0):.1f}% of step time)")

            log.info(f"DecisionEngine complete in {duration:.1f}s: {len(clips)} clips")
            return {
                "manifest": manifest,
                "stats": stats,
                "candidates": candidates,
                "performance_recorder": self.perf_recorder,
                "profiler_report": report,
                "clips_dir": str(campaign_output),
            }

        except ClipError:
            raise
        except Exception as e:
            stats.record_run(success=False, duration=time.time() - start_time, error=str(e))
            log.error(f"DecisionEngine failed: {type(e).__name__}: {e}")
            raise ClipError(f"pipeline failed at {type(e).__name__}: {e}")
        finally:
            if download_dir and download_dir.exists():
                shutil.rmtree(download_dir, ignore_errors=True)

    def _poll_content_rewards(self, campaign_id: str, campaign_name: str,
                              video_url: str, clips: List[Dict]):
        try:
            scraper = ContentRewardsScraper()
            performance = scraper.get_campaign_performance(campaign_id)
            if performance:
                for clip in clips:
                    views = performance.get("views", 0)
                    watch_time = performance.get("watch_time", 0.0)
                    earnings = performance.get("earnings", 0.0)
                    cpm = performance.get("cpm", 0.0)
                    completion_rate = performance.get("completion_rate", 0.0)
                    retention_curve = performance.get("retention_curve", None)

                    self.perf_recorder.record_performance(
                        clip_id=clip["clip_id"],
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        video_url=video_url,
                        views=views,
                        completion_rate=completion_rate,
                        earnings=earnings,
                        cpm=cpm,
                        watch_time=watch_time,
                        features={
                            "start": clip["start"],
                            "end": clip["end"],
                            "duration": clip["duration"],
                            "text_length": len(clip.get("text", "")),
                            "source_method": clip.get("source_method", "unknown"),
                            "score": clip.get("score", 0),
                        },
                        retention_curve=retention_curve,
                    )

                    self.learning_loop.record_performance(
                        clip["clip_id"],
                        views=views,
                        watch_time=watch_time,
                        earnings=earnings,
                        cpm=cpm,
                    )

                log.info(f"Polled Content Rewards performance for campaign {campaign_id}: "
                         f"{len(clips)} clips updated")
            else:
                log.info(f"No performance data yet from Content Rewards for {campaign_id}")
        except ImportError:
            log.debug("ContentRewardsScraper not available, skipping performance poll")
        except Exception as e:
            log.warning(f"Failed to poll Content Rewards performance: {e}")

    # ---------------------------------------------------------------------------
    # Video Monitoring - Check posted videos for payment eligibility
    # ---------------------------------------------------------------------------

    def monitor_posted_videos(self, hours: int = 24) -> dict:
        """
        Check all posted videos for performance/payment criteria.
        
        For videos that meet payment thresholds (earnings > 0 or views > threshold),
        marks them for notification and returns details.
        
        Returns dict with:
          - videos_checked: int
          - threshold_met: list of videos meeting payment criteria
          - notifications_sent: int
        """
        log.info(f"Starting video monitoring check (last {hours}h)...")
        needs_checking = self.posted_videos.get_needs_checking(hours=hours)
        threshold_met = []
        notified = 0

        for video in needs_checking:
            clip_id = video["clip_id"]
            platform = video["platform"]
            public_url = video["public_url"]

            try:
                # Check if we have webhook/n8n to get fresh analytics
                # For now, check if we have any stored performance data
                perf = self.perf_recorder.get_latest_performance(video["campaign_id"])
                
                views = perf.get("views", video.get("views", 0))
                earnings = perf.get("earnings", video.get("earnings", 0))
                cpm = perf.get("cpm", video.get("cpm", 0))

                # Update posted_videos with latest performance
                self.posted_videos.update_performance(
                    clip_id=clip_id,
                    views=views,
                    earnings=earnings,
                    cpm=cpm,
                    status="checked",
                )

                # Payment threshold: earnings > 0 OR views > 1000
                if earnings > 0 or views > 1000:
                    self.posted_videos.mark_payment_threshold_met(clip_id, notified=False)
                    threshold_met.append({
                        "clip_id": clip_id,
                        "campaign_id": video["campaign_id"],
                        "campaign_name": video["campaign_name"],
                        "platform": platform,
                        "public_url": public_url,
                        "views": views,
                        "earnings": earnings,
                        "cpm": cpm,
                    })
                    log.info(f"PAYMENT THRESHOLD MET: {platform} - {public_url} "
                             f"(views={views}, earnings=${earnings:.2f})")

            except Exception as e:
                log.warning(f"Failed to check video {clip_id}: {e}")

        # Get all unnotified threshold-met videos
        unnotified = self.posted_videos.get_unnotified_threshold_met()

        # Send notifications via webhook if configured
        if unnotified and self.webhook_url:
            try:
                self._post_to_webhook({
                    "event": "payment_threshold_met",
                    "videos": unnotified,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                for v in unnotified:
                    self.posted_videos.mark_payment_threshold_met(v["clip_id"], notified=True)
                    notified += 1
                log.info(f"Sent {notified} payment threshold notifications via webhook")
            except Exception as e:
                log.warning(f"Failed to send webhook notifications: {e}")

        result = {
            "videos_checked": len(needs_checking),
            "threshold_met": threshold_met,
            "notifications_sent": notified,
            "total_posted": len(self.posted_videos.get_all_posted()),
        }
        log.info(f"Monitoring complete: {result['videos_checked']} checked, "
                 f"{len(threshold_met)} met threshold, {notified} notified")
        return result

    # ---------------------------------------------------------------------------
    # Full Cycle - Agent runs everything internally
    # ---------------------------------------------------------------------------

    def run_full_cycle(
        self,
        tab: str = "All",
        min_cpm: float = 0.0,
        require_platforms: List[str] = None,
        max_campaigns: int = 5,
        num_clips: int = DEFAULT_NUM_CLIPS,
        use_llm: bool = True,
    ) -> dict:
        """
        Complete autonomous cycle:
        1. Scrape Content Rewards campaigns
        2. Select campaigns worth pursuing
        3. For each campaign: download, transcribe, highlight, render, post
        4. Track posted videos
        5. Monitor for payment eligibility
        
        Returns results dict with all cycle data.
        """
        cycle_start = time.time()
        results = {
            "campaigns_found": 0,
            "campaigns_selected": 0,
            "campaigns_processed": 0,
            "clips_created": 0,
            "clips_posted": 0,
            "errors": [],
            "videos_posted": [],
            "monitoring": None,
        }

        # Step 1: Discover and select campaigns
        log.info("=" * 60)
        log.info("FULL CYCLE: Campaign Discovery & Selection")
        log.info("=" * 60)
        try:
            selected = self.campaign_scraper.discover_and_select(
                tab=tab,
                min_cpm=min_cpm,
                require_platforms=require_platforms,
                max_campaigns=max_campaigns,
            )
            results["campaigns_found"] = len(selected)
            results["campaigns_selected"] = len(selected)
        except Exception as e:
            log.error(f"Campaign discovery failed: {e}")
            results["errors"].append(f"discovery: {e}")
            selected = []

        # Step 2: Process each selected campaign
        for i, campaign in enumerate(selected):
            campaign_id = campaign.get("campaign_id", campaign.get("url", f"campaign_{i}"))
            campaign_name = campaign.get("name", "unknown")
            source_video_url = campaign.get("source_video_url") or campaign.get("url")
            cpm = campaign.get("cpm", 0.0)

            if not source_video_url:
                log.warning(f"Campaign {campaign_name} has no video URL, skipping")
                results["errors"].append(f"{campaign_name}: no video URL")
                continue

            log.info(f"\n{'='*60}")
            log.info(f"Processing campaign {i+1}/{len(selected)}: {campaign_name}")
            log.info(f"{'='*60}")

            try:
                result = self.run(
                    url=source_video_url,
                    campaign_id=campaign_id,
                    campaign_name=campaign_name,
                    num_clips=num_clips,
                    use_llm=use_llm,
                    cpm=cpm,
                    poll_performance=False,  # We'll monitor separately
                )

                clips = result["manifest"].get("clips", [])
                results["campaigns_processed"] += 1
                results["clips_created"] += len(clips)

                # Step 3: Post clips to platforms
                posted = self._post_to_platforms(clips, campaign_id, campaign_name, cpm)
                results["clips_posted"] += len(posted)
                results["videos_posted"].extend(posted)

            except Exception as e:
                log.error(f"Campaign {campaign_name} failed: {e}")
                results["errors"].append(f"{campaign_name}: {e}")
                continue

        # Step 4: Monitor posted videos for payment
        log.info("\n" + "=" * 60)
        log.info("Video Monitoring & Payment Check")
        log.info("=" * 60)
        try:
            monitoring = self.monitor_posted_videos(hours=24)
            results["monitoring"] = monitoring
        except Exception as e:
            log.error(f"Monitoring failed: {e}")
            results["errors"].append(f"monitoring: {e}")

        # Step 5: Train model
        try:
            self.update_model()
        except Exception as e:
            log.warning(f"Model training skipped: {e}")

        duration = time.time() - cycle_start
        results["duration_seconds"] = round(duration, 1)
        results["success"] = len(results["errors"]) == 0

        log.info(f"\n{'='*60}")
        log.info(f"FULL CYCLE COMPLETE in {duration:.1f}s")
        log.info(f"  Campaigns: {results['campaigns_processed']}/{results['campaigns_selected']}")
        log.info(f"  Clips: {results['clips_created']} created, {results['clips_posted']} posted")
        log.info(f"  Errors: {len(results['errors'])}")
        log.info(f"{'='*60}")

        return results

    # ---------------------------------------------------------------------------
    # Platform Posting - YouTube (Composio), Instagram (Composio), TikTok (Zernio)
    # ---------------------------------------------------------------------------

    def _post_to_platforms(
        self,
        clips: List[Dict],
        campaign_id: str,
        campaign_name: str,
        cpm: float = 0.0,
    ) -> List[Dict]:
        """
        Post rendered clips to platforms:
        - YouTube: Composio API
        - Instagram: Composio API  
        - TikTok: Zernio API
        
        Returns list of posted video records.
        """
        posted = []
        for clip in clips:
            clip_id = clip.get("clip_id", "unknown")
            public_url = clip.get("public_path", "")
            hook_title = clip.get("hook_title", clip.get("text", "")[:80])
            caption = clip.get("caption", "")
            tags = clip.get("tags", [])

            if not public_url:
                log.warning(f"Clip {clip_id} has no public URL, skipping post")
                continue

            # YouTube via Composio
            try:
                yt_result = self._post_youtube_composio(
                    video_url=public_url,
                    title=hook_title,
                    description=caption,
                    tags=tags,
                    campaign_id=campaign_id,
                )
                if yt_result.get("success"):
                    self.posted_videos.record_posted(
                        clip_id=clip_id,
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        platform="youtube",
                        public_url=yt_result.get("video_url", public_url),
                        video_url=public_url,
                    )
                    posted.append({
                        "clip_id": clip_id,
                        "platform": "youtube",
                        "public_url": yt_result.get("video_url", public_url),
                        "campaign_id": campaign_id,
                    })
                    log.info(f"Posted to YouTube: {clip_id}")
            except Exception as e:
                log.warning(f"YouTube post failed for {clip_id}: {e}")

            # Instagram via Composio
            try:
                ig_result = self._post_instagram_composio(
                    video_url=public_url,
                    caption=caption,
                    title=hook_title,
                    hashtags=tags,
                    campaign_id=campaign_id,
                )
                if ig_result.get("success"):
                    self.posted_videos.record_posted(
                        clip_id=clip_id,
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        platform="instagram",
                        public_url=ig_result.get("video_url", public_url),
                        video_url=public_url,
                    )
                    posted.append({
                        "clip_id": clip_id,
                        "platform": "instagram",
                        "public_url": ig_result.get("video_url", public_url),
                        "campaign_id": campaign_id,
                    })
                    log.info(f"Posted to Instagram: {clip_id}")
            except Exception as e:
                log.warning(f"Instagram post failed for {clip_id}: {e}")

            # TikTok via Zernio (Composio doesn't support TikTok)
            try:
                tt_result = self._post_tiktok_zernio(
                    video_url=public_url,
                    caption=caption,
                    title=hook_title,
                    hashtags=tags,
                    campaign_id=campaign_id,
                )
                if tt_result.get("success"):
                    self.posted_videos.record_posted(
                        clip_id=clip_id,
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        platform="tiktok",
                        public_url=tt_result.get("video_url", public_url),
                        video_url=public_url,
                    )
                    posted.append({
                        "clip_id": clip_id,
                        "platform": "tiktok",
                        "public_url": tt_result.get("video_url", public_url),
                        "campaign_id": campaign_id,
                    })
                    log.info(f"Posted to TikTok: {clip_id}")
            except Exception as e:
                log.warning(f"TikTok post failed for {clip_id}: {e}")

        return posted

    def _post_youtube_composio(
        self,
        video_url: str,
        title: str,
        description: str,
        tags: List[str],
        campaign_id: str,
    ) -> dict:
        """Post to YouTube via Composio API."""
        import requests
        url = "https://backend.composio.dev/api/v3.1/tools/execute/YOUTUBE_UPLOAD_VIDEO"
        payload = {
            "user_id": "default",
            "arguments": {
                "title": title,
                "description": description,
                "tags": [t.replace("#", "") for t in tags],
                "categoryId": "22",
                "privacyStatus": "public",
                "videoFilePath": {
                    "name": f"{campaign_id}.mp4",
                    "mimetype": "video/mp4",
                    "url": video_url,
                },
            },
        }
        resp = requests.post(
            url,
            json=payload,
            headers={
                "x-api-key": f"{{ $env.COMPOSIO_API_KEY }}",
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        resp.raise_for_status()
        return {"success": True, "video_url": video_url, "response": resp.json()}

    def _post_instagram_composio(
        self,
        video_url: str,
        caption: str,
        title: str,
        hashtags: List[str],
        campaign_id: str,
    ) -> dict:
        """Post to Instagram via Composio API."""
        import requests
        url = "https://backend.composio.dev/api/v3.1/tools/execute/INSTAGRAM_POST_IG_USER_MEDIA"
        payload = {
            "user_id": "default",
            "arguments": {
                "caption": caption,
                "media_url": video_url,
                "title": title,
            },
        }
        resp = requests.post(
            url,
            json=payload,
            headers={
                "x-api-key": f"{{ $env.COMPOSIO_API_KEY }}",
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        resp.raise_for_status()
        return {"success": True, "video_url": video_url, "response": resp.json()}

    def _post_tiktok_zernio(
        self,
        video_url: str,
        caption: str,
        title: str,
        hashtags: List[str],
        campaign_id: str,
    ) -> dict:
        """Post to TikTok via Zernio API."""
        import requests
        url = "https://zernio.com/api/v1/posts"
        payload = {
            "platform": "tiktok",
            "videoUrl": video_url,
            "caption": caption,
            "title": title,
            "hashtags": hashtags,
            "apiKey": "$env.ZERONIO_API_KEY",
        }
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        resp.raise_for_status()
        return {"success": True, "video_url": video_url, "response": resp.json()}

    def _post_to_webhook(self, payload: dict):
        """Post results to configured webhook (n8n or other)."""
        import requests
        if not self.webhook_url:
            log.warning("No webhook URL configured, skipping webhook post")
            return
        resp = requests.post(
            self.webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        log.info(f"Webhook posted: {resp.status_code}")

    def update_model(self) -> bool:
        """
        Update the model with the latest performance data. (FIX #1)

        Trains a real neural network on actual clip features -> retention_score
        using LearningLoop.train_performance_model(). Also trains the
        retention curve predictor on accumulated retention curve data (FIX #4).

        Returns True if training completed, False if insufficient data.
        """
        features, labels, metadata = self.perf_recorder.get_training_data(
            min_views=5, min_earnings=0.0
        )

        if len(features) < 10:
            log.info(f"Not enough performance data yet ({len(features)} clips, need 10+)")
            return False

        log.info(f"Training performance model on {len(features)} clips with actual data...")
        model_path = self.learning_loop.train_performance_model(epochs=50, lr=1e-3)
        if model_path:
            log.info(f"Performance model saved to {model_path}")
        else:
            log.warning("Performance model training skipped (insufficient data)")

        self.retention_predictor.train_from_performance_db(self.perf_recorder.db_path)

        log.info(f"Trained on {len(features)} clips, "
                 f"avg retention_score={sum(labels)/len(labels):.1f}")
        return True

    def get_model_summary(self) -> dict:
        """Get a summary of the learned model."""
        data = self.perf_recorder.get_training_data(min_views=1)
        return {
            "num_clips_recorded": len(data[0]),
            "total_views": sum(m['views'] for m in data[2]),
            "total_earnings": sum(m['earnings'] for m in data[2]),
            "avg_retention_score": sum(data[1]) / len(data[1]) if data[1] else 0,
        }

    def get_ab_test_results(self, campaign_id: str) -> dict:
        """
        Get A/B test results comparing highlight selection methods. (FIX #3)

        Returns performance comparison across methods (llm, heuristic, jev, learned).
        """
        return self.highlight_selector.evaluate_methods(campaign_id)


class DecisionLoop:
    """
    Continuous decision loop that runs the agent autonomously.

    Cycle:
      1. Pull next campaign from queue
      2. Run DecisionEngine (submit clips)
      3. Wait for performance data (poll Content Rewards)
      4. Train model on actual performance
      5. Repeat with improved model

    Runs until explicitly stopped or the queue is exhausted.
    """

    def __init__(
        self,
        engine: DecisionEngine,
        campaign_queue: List[Dict],
        poll_interval: int = 3600,
        max_cycles: Optional[int] = None,
    ):
        self.engine = engine
        self.campaign_queue = campaign_queue
        self.poll_interval = poll_interval
        self.max_cycles = max_cycles
        self.cycle_count = 0
        self.running = False

    def run_continuous(self):
        """Run the decision loop continuously."""
        self.running = True
        log.info(f"DecisionLoop starting with {len(self.campaign_queue)} campaigns")

        while self.running:
            if self.max_cycles and self.cycle_count >= self.max_cycles:
                log.info(f"Max cycles ({self.max_cycles}) reached, stopping")
                break

            if not self.campaign_queue:
                log.info("Campaign queue empty, waiting...")
                time.sleep(self.poll_interval)
                continue

            campaign = self.campaign_queue.pop(0)
            self.cycle_count += 1

            log.info(f"=== Cycle {self.cycle_count}: "
                     f"{campaign.get('name', campaign.get('campaign_id', 'unknown'))} ===")

            try:
                result = self.engine.run(
                    url=campaign["url"],
                    campaign_id=campaign["campaign_id"],
                    campaign_name=campaign.get("campaign_name", campaign["campaign_id"]),
                    num_clips=campaign.get("num_clips", DEFAULT_NUM_CLIPS),
                    use_llm=campaign.get("use_llm", True),
                    n8n_url=self.engine.n8n_url,
                    model_size=campaign.get("model_size", "tiny"),
                    chunked=campaign.get("chunked", True),
                    poll_performance=True,
                )

                log.info(f"Cycle {self.cycle_count} complete: {result['stats'].get_summary()}")

            except Exception as e:
                log.error(f"Cycle {self.cycle_count} failed: {e}")

            if self.running:
                time.sleep(self.poll_interval)

    def stop(self):
        """Stop the decision loop."""
        self.running = False
        log.info("DecisionLoop stopped")


def run_decision_engine(
    url: str,
    n8n_url: str,
    campaign_id: str,
    campaign_name: str,
    cache: IdempotencyCache = None,
    num_clips: int = DEFAULT_NUM_CLIPS,
    use_llm: bool = True,
    use_jev: bool = False,
    use_heuristic: bool = True,
    model_size: str = "tiny",
    chunked: bool = True,
    max_download_seconds: int = DEFAULT_MAX_DOWNLOAD_SECONDS,
    webhook_url: str = None,
    posted_videos_tracker: PostedVideosTracker = None,
    **kwargs,
) -> dict:
    engine = DecisionEngine(
        n8n_url=n8n_url,
        cache=cache,
        perf_recorder=PerformanceRecorder(),
        posted_videos_tracker=posted_videos_tracker,
        highlight_selector=UnifiedHighlightSelector(),
        llm_client=kwargs.get("client"),
        llm_model=kwargs.get("model"),
        output_dir=kwargs.get("output_dir"),
        webhook_url=webhook_url,
    )
    return engine.run(
        url=url,
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        num_clips=num_clips,
        use_llm=use_llm,
        use_jev=use_jev,
        use_heuristic=use_heuristic,
        model_size=model_size,
        chunked=chunked,
        max_download_seconds=max_download_seconds,
        **kwargs,
    )


def run_full_cycle(
    tab: str = "All",
    min_cpm: float = 0.0,
    require_platforms: List[str] = None,
    max_campaigns: int = 5,
    num_clips: int = DEFAULT_NUM_CLIPS,
    use_llm: bool = True,
    webhook_url: str = None,
) -> dict:
    """
    Standalone full cycle entry point.
    Scrape campaigns -> select -> clip -> post -> monitor -> train.
    """
    engine = DecisionEngine(
        n8n_url="",
        cache=IdempotencyCache(),
        perf_recorder=PerformanceRecorder(),
        posted_videos_tracker=PostedVideosTracker(),
        highlight_selector=UnifiedHighlightSelector(),
        webhook_url=webhook_url,
    )
    return engine.run_full_cycle(
        tab=tab,
        min_cpm=min_cpm,
        require_platforms=require_platforms,
        max_campaigns=max_campaigns,
        num_clips=num_clips,
        use_llm=use_llm,
    )


def monitor_posted_videos(hours: int = 24) -> dict:
    """
    Standalone monitoring entry point.
    Check posted videos for payment eligibility and send notifications.
    """
    engine = DecisionEngine(
        n8n_url="",
        cache=IdempotencyCache(),
        perf_recorder=PerformanceRecorder(),
        posted_videos_tracker=PostedVideosTracker(),
        webhook_url=CAMPAIGN_WEBHOOK_URL,
    )
    return engine.monitor_posted_videos(hours=hours)


def process_local_video(
    video_path: Path,
    work_dir: Path,
    campaign_id: str,
    campaign_name: str,
    num_clips: int = DEFAULT_NUM_CLIPS,
    model_size: str = "tiny",
    chunked: bool = True,
    use_llm: bool = False,
    **kwargs,
) -> dict:
    """Process a local video file (no download step).

    Same pipeline as run_decision_engine, but accepts a pre-downloaded video
    instead of a YouTube URL. This is used by the /api/upload endpoint to test
    the pipeline with local files without needing YouTube access.
    """
    engine = DecisionEngine(
        n8n_url=kwargs.get("n8n_url", ""),
        cache=kwargs.get("cache"),
        perf_recorder=kwargs.get("perf_recorder"),
        highlight_selector=UnifiedHighlightSelector(),
        learning_loop=LearningLoop(),
        llm_client=kwargs.get("llm_client"),
        llm_model=kwargs.get("model"),
        output_dir=work_dir,
    )
    return engine.run(
        url=str(video_path),
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        num_clips=min(max(num_clips, 1), MAX_NUM_CLIPS),
        use_llm=use_llm,
        use_jev=False,
        use_heuristic=not use_llm,
        model_size=model_size,
        chunked=chunked,
        poll_performance=False,
        skip_download=True,
        **kwargs,
    )
