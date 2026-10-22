"""Apply improvements to the video clipper agent."""
import os
import subprocess
import sys

# Use the venv python to run the script
VENV_PY = "/c/Users/lenovo/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe"

# Read current pipeline.py
pipeline_path = "C:/Users/lenovo/video-agent/agent/pipeline.py"
with open(pipeline_path, "r") as f:
    content = f.read()

# 1. Add imports (time, random, functools.wraps) after "import logging"
content = content.replace(
    "import logging\nimport tempfile\n",
    "import logging\nimport random\nimport tempfile\nimport time\nfrom functools import wraps\n",
)

# 2. Add PipelineStats class and retry decorator before "def _download_video"
pipeline_stats = '''

class PipelineStats:
    """Track pipeline execution statistics for monitoring and observability.

    Provides run counts, success rates, average duration, and the last
    executed campaign details so downstream systems (n8n, dashboards) can
    observe the agent's health without reading logs.
    """

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
            log.info(
                "Pipeline run completed: campaign_id=%s, success=true, duration=%.1fs, "
                "clips=%d, llm_provider=%s",
                campaign_id, duration, clip_count, llm_provider,
            )
        else:
            self.failed_runs += 1
            log.error(
                "Pipeline run failed: campaign_id=%s, duration=%.1fs, error=%s",
                campaign_id, duration, error or "unknown",
            )

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


# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------
def retry(max_retries=3, base_delay=2.0, max_delay=60.0,
          retryable_exceptions=(ClipError,)):
    """Retry a function with exponential backoff on retryable exceptions."""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = base_delay
            last_error = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_error = e
                    if attempt == max_retries:
                        break
                    sleep_time = min(delay, max_delay) + random.uniform(0, 0.5)
                    log.warning(
                        "Attempt %d/%d failed (%s). Retrying in %.1fs...",
                        attempt + 1, max_retries + 1, type(e).__name__, sleep_time,
                    )
                    time.sleep(sleep_time)
                    delay = min(delay * 2, max_delay)
            raise ClipPipelineError(
                "Operation failed after %d attempts: %s" % (max_retries + 1, last_error)
            )
        return wrapper
    return decorator


'''

# Insert PipelineStats and retry decorator before "def _download_video"
content = content.replace(
    "def _download_video(url: str, work_dir: Path, fmt: str) -> Path:",
    pipeline_stats + "\ndef _download_video(url: str, work_dir: Path, fmt: str) -> Path:",
)

# 3. Add stats instance
content = content.replace(
    'log = logger_config("agent.pipeline")\n\nDEFAULT_CLIP_DURATION',
    'log = logger_config("agent.pipeline")\n\nstats = PipelineStats()\n\nDEFAULT_CLIP_DURATION',
)

# 4. Wrap run_from_url with retry decorator
old_run_from_url = '''def run_from_url(url: str, *, num_clips: int = DEFAULT_NUM_CLIPS, aspect: str = "9:16", use_llm: bool = True) -> Dict[str, Any]:
    if not url:
        raise ClipPipelineError("URL is required")
    work_dir = Path(tempfile.mkdtemp(prefix="agent_work_"))
    try:
        source_path = _download_video(url, work_dir, fmt="720")
        log.info("downloaded source -> %s (%.0fs)", source_path, source_path.stat().st_size)
        transcript = transcribe(source_path)
        if not transcript.get("segments"):
            raise ClipError("Whisper produced no segments (no speech detected?)")
        log.info("transcribed -> %d segments, %.0fs", len(transcript["segments"]), transcript["duration"])
        return run(transcript, num_clips=num_clips, aspect=aspect, use_llm=use_llm)
    except Exception:
        raise
    finally:
        for p in list(work_dir.glob("*")):
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                pass'''

new_run_from_url = '''@retry(max_retries=3, base_delay=2.0, max_delay=60.0)
def run_from_url(url: str, *, num_clips: int = DEFAULT_NUM_CLIPS, aspect: str = "9:16", use_llm: bool = True) -> Dict[str, Any]:
    if not url:
        raise ClipPipelineError("URL is required")
    work_dir = Path(tempfile.mkdtemp(prefix="agent_work_"))
    try:
        source_path = _download_video(url, work_dir, fmt="720")
        log.info("downloaded source -> %s (%.0fs)", source_path, source_path.stat().st_size)
        transcript = transcribe(source_path)
        if not transcript.get("segments"):
            raise ClipError("Whisper produced no segments (no speech detected?)")
        log.info("transcribed -> %d segments, %.0fs", len(transcript["segments"]), transcript["duration"])
        return run(transcript, num_clips=num_clips, aspect=aspect, use_llm=use_llm)
    except Exception as e:
        stats.record_run(success=False, duration=0.0, error=str(e))
        raise
    finally:
        for p in list(work_dir.glob("*")):
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                pass'''

if old_run_from_url in content:
    content = content.replace(old_run_from_url, new_run_from_url)
    print("run_from_url wrapped with retry")
else:
    print("run_from_url NOT FOUND")

# 5. Update type imports
content = content.replace(
    "from typing import Any, Dict",
    "from typing import Any, Dict, Optional",
)

# 6. Wrap run() with retry decorator
old_run = '''def run(transcript: Dict[str, Any], num_clips: int, aspect: str = "9:16", use_llm: bool = True) -> Dict[str, Any]:
    duration = transcript.get("duration", 0.0)
    segments = transcript.get("segments", [])
    log.info("run: duration=%.0fs, segments=%d", duration, len(segments))

    content_info = _detect_content_type(transcript)
    transcript_text = build_transcript_text(transcript)
    if use_llm:
        highlights_raw = prompt_highlights(
            transcript_text,
            content_info.get("content_type", "other"),
            content_info.get("density", "medium"),
            num_clips,
            duration,
        ).get("highlights", [])
    else:
        from .heuristic import score_segments
        highlights_raw = score_segments(transcript, num_clips)
    if not highlights_raw:
        raise ClipError("no highlight candidates (LLM and heuristic both empty)")
    highlights = [dict(h) for h in highlights_raw]

    words = [w for s in segments for w in (s.get("words") or [])]
    if not words:
        words = [{}]

    for h in highlights:
        _start, end = adjust_window(words, float(h["start_time"]), float(h["end_time"]))
        h["start_time"] = _start
        h["end_time"] = end

    highlights = score_highlights(highlights, clip_duration=DEFAULT_CLIP_DURATION, video_duration=duration)
    highlights = sorted(highlights, key=lambda x: int(x.get("score", 0)), reverse=True)[:MAX_NUM_CLIPS]

    out_dir = Path(tempfile.gettempdir()) / "agent_clips"
    out_dir.mkdir(parents=True, exist_ok=True)
    clips = []
    for i, h in enumerate(highlights, 1):
        start = max(0.0, float(h["start_time"]))
        end = min(duration, min(float(h["end_time"]), duration - 0.1))
        clip_path = out_dir / ("clip_%02d.mp4" % i)
        ass_path = out_dir / ("clip_%02d.ass" % i)
        ass_path.write_text(build_ass_subtitles(segments, start, end), encoding="utf-8")
        try:
            render_clip(Path("source"), start, end, clip_path, aspect=aspect)
        finally:
            if ass_path.exists():
                ass_path.unlink()
        clips.append({
            "index": i,
            "title": h.get("title", "Highlight %d" % i),
            "start": round(start, 2),
            "end": round(min(end, duration), 2),
            "score": int(h.get("score", 0)),
            "hook_sentence": h.get("hook_sentence", ""),
            "virality_reason": h.get("virality_reason", ""),
            "clip_path": str(clip_path),
            "clip_url": None,
        })
    return {"duration": duration, "segments": segments, "highlights": highlights, "clips": clips}'''

new_run = '''@retry(max_retries=3, base_delay=2.0, max_delay=60.0)
def run(transcript: Dict[str, Any], num_clips: int, aspect: str = "9:16", use_llm: bool = True) -> Dict[str, Any]:
    duration = transcript.get("duration", 0.0)
    segments = transcript.get("segments", [])
    log.info("run: duration=%.0fs, segments=%d", duration, len(segments))

    content_info = _detect_content_type(transcript)
    transcript_text = build_transcript_text(transcript)
    if use_llm:
        highlights_raw = prompt_highlights(
            transcript_text,
            content_info.get("content_type", "other"),
            content_info.get("density", "medium"),
            num_clips,
            duration,
        ).get("highlights", [])
    else:
        highlights_raw = score_segments(transcript, num_clips)
    if not highlights_raw:
        raise ClipError("no highlight candidates (LLM and heuristic both empty)")
    highlights = [dict(h) for h in highlights_raw]

    words = [w for s in segments for w in (s.get("words") or [])]
    if not words:
        words = [{}]

    for h in highlights:
        _start, end = adjust_window(words, float(h["start_time"]), float(h["end_time"]))
        h["start_time"] = _start
        h["end_time"] = end

    highlights = score_highlights(highlights, clip_duration=DEFAULT_CLIP_DURATION, video_duration=duration)
    highlights = sorted(highlights, key=lambda x: int(x.get("score", 0)), reverse=True)[:MAX_NUM_CLIPS]

    out_dir = Path(tempfile.gettempdir()) / "agent_clips"
    out_dir.mkdir(parents=True, exist_ok=True)
    clips = []
    for i, h in enumerate(highlights, 1):
        start = max(0.0, float(h["start_time"]))
        end = min(duration, min(float(h["end_time"]), duration - 0.1))
        clip_path = out_dir / ("clip_%02d.mp4" % i)
        ass_path = out_dir / ("clip_%02d.ass" % i)
        ass_path.write_text(build_ass_subtitles(segments, start, end), encoding="utf-8")
        try:
            render_clip(Path("source"), start, end, clip_path, aspect=aspect)
        finally:
            if ass_path.exists():
                ass_path.unlink()
        clips.append({
            "index": i,
            "title": h.get("title", "Highlight %d" % i),
            "start": round(start, 2),
            "end": round(min(end, duration), 2),
            "score": int(h.get("score", 0)),
            "hook_sentence": h.get("hook_sentence", ""),
            "virality_reason": h.get("virality_reason", ""),
            "clip_path": str(clip_path),
            "clip_url": None,
        })
    stats.record_run(
        success=True,
        duration=0.0,
        campaign_id=None,
        campaign_name=None,
        llm_provider="llm" if use_llm else "heuristic",
        num_clips=num_clips,
        clip_count=len(clips),
    )
    return {"duration": duration, "segments": segments, "highlights": highlights, "clips": clips}'''

if old_run in content:
    content = content.replace(old_run, new_run)
    print("run() wrapped with retry")
else:
    print("run() NOT FOUND")

# Write back
with open(pipeline_path, "w") as f:
    f.write(content)

print("pipeline.py updated successfully")
print("Lines:", len(content.splitlines()))

# Verify compile
result = subprocess.run(
    [VENV_PY, "-m", "py_compile", pipeline_path],
    capture_output=True, text=True
)
print("Compile:", "OK" if result.returncode == 0 else result.stderr)
