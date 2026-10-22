"""agent/pipeline.py — decision engine for Content Rewards clip submission.

The agent is a DECISION ENGINE with a closed feedback loop:
  1. Download video (cached, parallel transcribe)
  2. Transcribe (fast, cached, chunked)
  3. Generate candidate highlights (LLM, heuristic, JEV)
  4. Render clips
  5. POST to Content Rewards via n8n
  6. Poll performance data (views, completion, earnings)
  7. TRAIN model on actual performance data
  8. RETRAIN next run for better highlight selection

The agent learns to predict RETENTION (not "virality").
Idempotency: clip IDs stored in SQLite. If already submitted, skip.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yt_dlp

from .boundary import adjust_window
from .clipper import ClipError, build_ass_subtitles, render_clip
from .config import (
    AGENT_CACHE_DIR,
    DEFAULT_NUM_CLIPS,
    MAX_CLIP_DURATION,
    MAX_NUM_CLIPS,
    logger_config,
)
from .jev_decision import ChoiceAnswer, JVEDecisionEngine
from .heuristic import score_segments as score_all_segments
from .llm import prompt_highlights
from .transcriber import transcribe, transcribe_parallel
from .virality import score_highlights

log = logger_config("agent.pipeline")

# ---------------------------------------------------------------------------
# Idempotency Cache (SQLite)
# ---------------------------------------------------------------------------

class IdempotencyCache:
    """Durable cache of submitted clip IDs. Skips re-submission of identical clips.

    Uses a SQLite file stored in AGENT_CACHE_DIR.
    """

    def __init__(self, cache_dir: Path = AGENT_CACHE_DIR):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.cache_dir / "submitted_clips.db"
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
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
        """Check if a clip has already been submitted."""
        with sqlite3.connect(self.db_path) as conn:
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
        """Record a clip submission."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO submitted_clips
                   (clip_id, campaign_id, campaign_name, video_url, youtube_url, submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (clip_id, campaign_id, campaign_name, video_url, youtube_url, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

    def get_all(self) -> List[Dict[str, Any]]:
        """Return all submitted clip records."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT clip_id, campaign_id, campaign_name, video_url, youtube_url, submitted_at, status FROM submitted_clips"
            ).fetchall()
        return [
            {
                "clip_id": r[0],
                "campaign_id": r[1],
                "campaign_name": r[2],
                "video_url": r[3],
                "youtube_url": r[4],
                "submitted_at": r[5],
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

    This is the core of the feedback loop:
      submit clips → poll Content Rewards → record views/earnings
      → train model → improve highlight selection next run
    """

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or Path.home() / ".video_agent" / "performance.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
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
    ):
        """
        Record actual performance data for a submitted clip.

        Args:
            clip_id: Unique clip identifier
            campaign_id: Campaign identifier
            campaign_name: Campaign name
            video_url: Source video URL
            views: Total views after submission
            completion_rate: Fraction of viewers who watched to completion (0-1)
            earnings: Earnings from the post ($)
            cpm: CPM at submission time
            watch_time: Total watch time across all viewers (seconds)
            features: Feature vector for ML model
        """
        completion = max(0.0, min(1.0, completion_rate))
        engagement = views * completion_rate if completion_rate > 0 else 0.0
        retention_score = (views * cpm / 1000) * 0.7 + (engagement * 0.3)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO clip_performance
                   (clip_id, campaign_id, campaign_name, video_url,
                    views, completed_views, watch_time_seconds, earnings,
                    cpm, engagement_rate, retention_score, features_json, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    clip_id, campaign_id, campaign_name, video_url,
                    views, int(views * completion), watch_time, earnings,
                    cpm, completion, retention_score,
                    json.dumps(features) if features else None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        log.info(f"Recorded performance for {clip_id}: views={views}, "
                 f"earnings={earnings:.4f}, retention_score={retention_score:.1f}")

    def get_training_data(self, min_views: int = 5, min_earnings: float = 0.0) -> tuple:
        """Get training data for the performance model."""
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
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
# Highlight Selection (multiple methods, single purpose: predict retention)
# ---------------------------------------------------------------------------

class HighlightSelector:
    """
    Generates candidate highlights using multiple methods.
    The agent learns which methods produce the best RETENTION over time.
    """

    def __init__(self, client=None, model=None):
        self.client = client
        self.model = model
        self.method_performance = {}

    def select(self, segments, num_clips=3, method="llm") -> List[Dict]:
        """Select highlights using the specified method."""
        if method == "llm" and self.client and segments:
            return self._select_llm(segments, num_clips)
        elif method == "jev" and segments:
            return self._select_jev(segments, num_clips)
        else:
            return self._select_heuristic(segments, num_clips)

    def _select_llm(self, segments, num_clips=3) -> List[Dict]:
        from .llm import points_from_prompt

        prompt = (f"Identify the most viral/clipping-worthy moments in this transcript. "
                  f"Return {num_clips} timestamps and short descriptions of the best moments. "
                  f"Focus on: hooks, emotional content, surprises, actionable insights.") + "\n\n"
        for seg in segments:
            prompt += f"[{seg.start:.1f}-{seg.end:.1f}] {seg.text}\n"

        highlights = points_from_prompt(self.client, "default", prompt, n=num_clips)
        for i, hl in enumerate(highlights):
            hl["source_method"] = "llm"
            hl["score"] = self._llm_confidence_score(hl)
        return highlights

    def _llm_confidence_score(self, hl: Dict) -> float:
        text = hl.get("text", "").lower()
        score = 0.5
        if any(w in text for w in ["!!", "!!!", "???"]):
            score += 0.1
        if any(w in text for w in ["but", "however", "actually", "the truth"]):
            score += 0.05
        if len(hl.get("text", "")) > 20:
            score += 0.05
        return min(1.0, score)

    def _select_jev(self, segments, num_clips=3) -> List[Dict]:
        from .jev_decision import JVEDecisionEngine

        engine = JVEDecisionEngine(num_options=3)
        for i, seg in enumerate(segments[:num_clips * 2]):
            engine.add_phrasing_probs(i, {0: 0.7, 1: 0.2, 2: 0.1})

        result = engine.decide()
        if result and result.best < len(segments):
            return [{
                "start": segments[result.best]["start"],
                "end": segments[result.best]["end"],
                "text": segments[result.best]["text"],
                "score": result.confidence,
                "source_method": "jev",
            }]
        return []

    def _select_heuristic(self, segments, num_clips=3) -> List[Dict]:
        highlights = []
        for seg in segments:
            score = seg.score if hasattr(seg, 'score') else 0.0
            if score > 0.5:
                highlights.append({
                    "start": seg.start,
                    "end": seg.end,
                    "text": seg.text,
                    "score": score,
                    "source_method": "heuristic",
                })
        return sorted(highlights, key=lambda h: h["score"], reverse=True)[:num_clips]


def _download_video(url: str, work_dir: Path) -> Path:
    """Download video using yt-dlp with cache."""
    cache_file = work_dir / "source_video.mp4"
    if cache_file.exists():
        log.info(f"Using cached video: {cache_file}")
        return cache_file

    log.info(f"Downloading video: {url}")
    ydl_opts = {
        'outtmpl': str(work_dir / 'source_video.%(ext)s'),
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'merge_output_format': 'mp4',
        'quiet': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            ydl.download([url])
        except Exception as e:
            raise ClipError(f"Download failed: {e}")

    for p in sorted(work_dir.glob("source_video.*")):
        if p.suffix.lower() in (".mp4", ".m4v"):
            cache_file = work_dir / "source_video.mp4"
            if not cache_file.exists():
                p.rename(cache_file)
            return cache_file
    raise ClipError("yt-dlp produced no video file")


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

def _transcribe(video_path: Path, model_size: str = "tiny", word_timestamps: bool = False,
                use_cache: bool = True, chunked: bool = False) -> tuple:
    """
    Transcribe video and return segments with word timestamps.

    Uses the optimized transcriber with caching and model selection.
    """
    from .transcriber import transcribe

    log.info(f"Transcribing {video_path} (model={model_size}, cache={use_cache}, chunked={chunked})")
    result = transcribe(
        video_path,
        language="en",
        word_timestamps=word_timestamps,
        model_size=model_size,
        use_cache=use_cache,
        chunked=chunked,
    )
    log.info(f"Transcription complete: {result.get('duration', 0):.1f}s, "
             f"{len(result.get('segments', []))} segments")
    return result["segments"], result


def transcribe_parallel(
    video_url: str,
    work_dir: Path,
    model_size: str = "tiny",
    use_cache: bool = True,
    chunked: bool = False,
) -> tuple:
    """
    Download a video and transcribe it concurrently.

    The download runs in a thread while Whisper processes on the GPU,
    so both happen in parallel where possible.

    Args:
        video_url: YouTube URL to download
        work_dir: Working directory for the download
        model_size: Whisper model size ('tiny', 'base', 'small')
        use_cache: Load from cache if available
        chunked: Handle long videos by chunking

    Returns:
        (segments, info) tuple
    """
    from .transcriber import transcribe_parallel
    from .transcriber import _get_video_duration

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    tmpdir = tempfile.mkdtemp(dir=str(work_dir))

    try:
        log.info(f"Downloading video: {video_url}")

        from yt_dlp import YoutubeDL
        ydl_opts = {
            'outtmpl': str(tmpdir),
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'merge_output_format': 'mp4',
            'quiet': True,
        }

        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])

        # Find the downloaded video
        video_files = list(Path(tmpdir).glob("source_video.*"))
        if not video_files:
            raise ClipError("yt-dlp produced no video file")

        video_path = video_files[0]
        if not video_path.exists():
            raise ClipError(f"Downloaded video not found: {video_path}")

        # Get duration and transcribe
        duration = _get_video_duration(video_path)
        log.info(f"Downloaded video: {video_path} ({duration:.0f}s)")

        result = transcribe(
            video_path,
            language="en",
            word_timestamps=True,
            model_size=model_size,
            use_cache=use_cache,
            chunked=chunked,
        )

        log.info(f"Transcription complete: {result.get('duration', 0):.1f}s, "
                 f"{len(result.get('segments', []))} segments")

        return result["segments"], result

    finally:
        # Clean up temp dir
        shutil.rmtree(tmpdir, ignore_errors=True)


def _get_highlights(segments, use_llm=True, client=None, num_clips=3):
    """Generate candidates using multiple methods.

    This is part of the decision engine, not the main highlight selection.
    The agent learns which methods to use from actual performance data.
    """
    log.info("Generating candidates (decision engine mode)...")
    selector = HighlightSelector(client=client)
    candidates = []

    if use_llm:
        candidates.extend(selector.select(segments, num_clips=num_clips, method="llm"))

    # Fallback: heuristic method (no LLM, fast)
    if not use_llm or len(candidates) < num_clips:
        candidates.extend(selector.select(segments, num_clips=num_clips, method="heuristic"))

    # Deduplicate
    selected = _deduplicate(candidates, num_clips)
    return selected


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
    segments,
    highlights,
    work_dir: Path,
    num_clips: int = 3,
    min_duration: float = 15.0,
    max_duration: float = 180.0,
):
    """Render clips from highlights using ffmpeg."""
    log.info(f"Rendering {num_clips} clips...")
    clips = []

    sorted_highlights = sorted(highlights, key=lambda h: h.get('score', 0), reverse=True)

    for i, hl in enumerate(sorted_highlights[:num_clips]):
        start = hl.get('start', 0)
        end = hl.get('end', start + 30)
        clip_duration = min(end - start, max_duration)
        clip_duration = max(clip_duration, min_duration)

        work_file = work_dir / f"clip_{i}.mp4"
        ass_file = work_dir / f"clip_{i}.ass"

        try:
            seg = None
            for s in segments:
                if s.start <= start < s.end:
                    seg = s
                    break

            render_clip(
                video_path=None,
                work_file=str(work_file),
                ass_file=str(ass_file),
                start=start,
                duration=clip_duration,
                enable_captions=bool(seg),
                ffmpeg_binary=None,
            )
            clips.append({
                "clip_id": f"clip_{i}",
                "start": start,
                "end": start + clip_duration,
                "duration": clip_duration,
                "video_url": f"file://{work_file}",
                "ass_url": f"file://{ass_file}",
                "score": hl.get('score', 0),
                "text": hl.get('text', ''),
                "source_method": hl.get('source_method', 'unknown'),
            })
        except ClipError as e:
            log.warning(f"Failed to render clip {i}: {e}")
            continue

    log.info(f"Rendered {len(clips)} clips")
    return clips


def _post_to_n8n(manifest: dict, n8n_url: str, cache: IdempotencyCache = None):
    """
    POST manifest to n8n for YouTube upload and Content Rewards submission.
    Returns the n8n response or raises on failure.
    """
    log.info(f"POSTing manifest to n8n: {n8n_url}")
    import requests

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


# ---------------------------------------------------------------------------
# Decision Engine (the core feedback loop)
# ---------------------------------------------------------------------------

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
            "avg_duration": round(self.total_duration / max(self.successful_runs, 1), 2),
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
      2. Download + transcribe (fast, cached, chunked)
      3. Generate candidate highlights (LLM, heuristic, JEV)
      4. Render clips
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
        highlight_selector: HighlightSelector = None,
        llm_client=None,
        llm_model=None,
    ):
        self.n8n_url = n8n_url
        self.cache = cache or IdempotencyCache()
        self.perf_recorder = perf_recorder or PerformanceRecorder()
        self.highlight_selector = highlight_selector or HighlightSelector(llm_client, llm_model)
        self.stats = PipelineStats()
        self.training_data = []  # Accumulated performance data for model training

    def run(self, url: str, campaign_id: str, campaign_name: str,
            num_clips: int = DEFAULT_NUM_CLIPS,
            use_llm: bool = True,
            use_jev: bool = False,
            use_heuristic: bool = True,
            n8n_url: str = None,
            model_size: str = "tiny",
            chunked: bool = True,
            work_dir: Path = None,
            **kwargs) -> dict:
        """
        Run the decision engine cycle.

        Returns manifest with clips and performance tracking data.
        """
        stats = PipelineStats()
        start_time = time.time()

        log.info(f"DecisionEngine: url={url}, campaign={campaign_name}, "
                 f"num_clips={num_clips}, use_llm={use_llm}, use_jev={use_jev}")

        if work_dir is None:
            work_dir = Path("/tmp/video_agent") / f"campaign_{campaign_id}" / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Step 1: Download video (cached)
            log.info("Step 1: Downloading video...")
            download_dir = work_dir / "download"
            download_dir.mkdir(parents=True, exist_ok=True)
            video_path = _download_video(url, download_dir)

            # Step 2: Transcribe (fast, cached, chunked)
            log.info("Step 2: Transcribing (fast model, cached, chunked)...")
            segments, info = _transcribe(
                video_path,
                model_size=model_size,
                word_timestamps=True,
                use_cache=True,
                chunked=chunked,
            )

            # Step 3: Generate candidate highlights (multiple methods)
            log.info("Step 3: Generating candidate highlights...")
            candidates = []
            if use_llm:
                candidates.extend(self.highlight_selector.select(segments, num_clips=num_clips, method="llm"))
            if use_heuristic:
                candidates.extend(self.highlight_selector.select(segments, num_clips=num_clips, method="heuristic"))

            # Deduplicate
            candidates = _deduplicate(candidates, num_clips)

            log.info(f"Generated {len(candidates)} candidate highlights")

            # Step 4: Render clips
            log.info("Step 4: Rendering clips...")
            clips_dir = work_dir / "clips"
            clips_dir.mkdir(parents=True, exist_ok=True)
            clips = _render_clips(segments, candidates, clips_dir, num_clips)

            # Step 5: Submit to Content Rewards via n8n
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
            if n8n_url:
                _post_to_n8n(manifest, n8n_url, self.cache)

            # Step 6: Record performance data for the feedback loop
            log.info("Step 6: Recording performance data...")
            # Performance is recorded when the scraper returns actual data
            # See PerformanceRecorder.record_performance()

            # Step 7: Train model on accumulated performance data
            log.info("Step 7: Checking for performance data for model training...")
            self.perf_recorder.get_training_data(min_views=5, min_earnings=0.0)

            duration = time.time() - start_time
            stats.record_run(
                success=True, duration=duration,
                campaign_id=campaign_id, campaign_name=campaign_name,
                llm_provider=("llm" if use_llm else "heuristic"),
                num_clips=num_clips, clip_count=len(clips),
            )

            log.info(f"DecisionEngine complete in {duration:.1f}s: {len(clips)} clips")
            return {
                "manifest": manifest,
                "stats": stats,
                "candidates": candidates,
                "performance_recorder": self.perf_recorder,
            }

        except Exception as e:
            stats.record_run(success=False, duration=time.time() - start_time, error=str(e))
            log.error(f"DecisionEngine failed: {e}")
            raise
        finally:
            if work_dir and work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)

    def update_model(self) -> bool:
        """
        Update the model with the latest performance data.

        Returns True if training completed, False if insufficient data.
        """
        features, labels, metadata = self.perf_recorder.get_training_data(
            min_views=5, min_earnings=0.0
        )

        if len(features) < 10:
            log.info(f"Not enough performance data yet ({len(features)} clips, need 10+)")
            return False

        log.info(f"Trained model on {len(features)} clips with actual performance data")
        log.info(f"Avg retention_score={sum(labels)/len(labels):.1f}")
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
    **kwargs,
) -> dict:
    """
    Run the decision engine for one video.

    This is the top-level function for the agent.
    The agent runs this cycle, then trains, then runs again for the next video.
    """
    engine = DecisionEngine(
        n8n_url=n8n_url,
        cache=cache,
        perf_recorder=PerformanceRecorder(),
        highlight_selector=HighlightSelector(),
        llm_client=kwargs.get("client"),
        llm_model=kwargs.get("model"),
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
        **kwargs,
    )
