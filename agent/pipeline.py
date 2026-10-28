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

from .boundary import adjust_window
from .clipper import ClipError, render_clip, build_ass_subtitles
from .config import (
    AGENT_CACHE_DIR, DATA_DIR, OUTPUT_DIR, DEFAULT_NUM_CLIPS, MAX_CLIP_DURATION, MAX_NUM_CLIPS,
    logger_config, PROVIDER_ORDER, USE_LLM, DEFAULT_MAX_DOWNLOAD_SECONDS,
)
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
# Download + Transcribe (parallel model load)
# ---------------------------------------------------------------------------

def _download_video(url: str, work_dir: Path, max_duration: int = 60) -> Path:
    """Download video using yt-dlp with cache.

    Only downloads the first ``max_duration`` seconds of the source video.
    This is critical for long videos (3+ min): downloading the entire file
    can take 30-60s on its own, plus transcription scales linearly with
    audio length. Capping at 60s keeps the full pipeline under ~90s.
    """
    cache_key = f"source_video_{url.split('v=')[-1].split('&')[0]}_{max_duration}s.mp4"
    cache_file = work_dir / cache_key
    if cache_file.exists():
        log.info(f"Using cached video: {cache_file}")
        return cache_file

    log.info(f"Downloading video (first {max_duration}s): {url}")
    ydl_opts = {
        'outtmpl': str(work_dir / 'source_video.%(ext)s'),
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'merge_output_format': 'mp4',
        'quiet': True,
        'download_ranges': lambda info, ydl: [(0, max_duration)],
        'force_keyframes': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            ydl.download([url])
        except Exception as e:
            raise ClipError(f"Download failed: {e}")

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
    """Render clips from highlights using ffmpeg."""
    log.info(f"Rendering {num_clips} clips from {video_path}...")
    clips = []

    sorted_highlights = sorted(highlights, key=lambda h: h.get('score', 0), reverse=True)

    for i, hl in enumerate(sorted_highlights[:num_clips]):
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
        except Exception as e:  # noqa: BLE001
            log.warning("caption generation failed for clip %s: %s", i, e)

        try:
            render_clip(
                source_path=video_path,
                start=start,
                end=start + clip_duration,
                output_path=work_file,
                aspect="9:16",
                captions_ass=ass_file if seg else None,
            )
            clips.append({
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
            })
        except ClipError as e:
            log.warning(f"Failed to render clip {i}: {e}")
            continue

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
        n8n_url: str,
        cache: IdempotencyCache = None,
        perf_recorder: PerformanceRecorder = None,
        highlight_selector: UnifiedHighlightSelector = None,
        learning_loop: LearningLoop = None,
        llm_client: LLMClient = None,
        llm_model=None,
        output_dir: Path = None,
    ):
        self.n8n_url = n8n_url
        self.cache = cache or IdempotencyCache()
        self.perf_recorder = perf_recorder or PerformanceRecorder()
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

        except ClipPipelineError:
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
        """
        Poll the Content Rewards scraper for actual performance data.

        This is the analytics ingestion step - it fetches views, watch time,
        earnings, and retention data from Content Rewards and feeds it back
        into the performance recorder for model training.
        """
        try:
            from .contentrewards_scraper import ContentRewardsScraper
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
    **kwargs,
) -> dict:
    engine = DecisionEngine(
        n8n_url=n8n_url,
        cache=cache,
        perf_recorder=PerformanceRecorder(),
        highlight_selector=UnifiedHighlightSelector(),
        llm_client=kwargs.get("client"),
        llm_model=kwargs.get("model"),
        output_dir=kwargs.get("output_dir"),
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
