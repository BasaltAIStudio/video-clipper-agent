"""agent/app.py — FastAPI HTTP surface.

This is the single endpoint n8n's "HTTP Request" node calls. The agent is a
blind builder: it returns JSON only, logs to stdout, and raises ClipperError
(upstream ClipperError) with clear messages instead of silent failures.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
import sqlite3
import contextlib
import json as _json
from pathlib import Path
from typing import Any, Dict

import threading
import uuid as _uuid

from fastapi import FastAPI, HTTPException, File, UploadFile, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from . import config as cfg
from .config import DEFAULT_MAX_DOWNLOAD_SECONDS

log = logging.getLogger("agent.app")

app = FastAPI(
    title="Video Clipper Agent",
    description="Clip the most viral moments from a YouTube link (Snap Deploy service).",
    version="0.1.0",
)

app.state.workspace = Path(cfg.OUTPUT_DIR)

# Persistent job storage using SQLite
_JOB_DB_DIR = Path('/app/cache') if Path('/app/cache').exists() else Path(os.getenv('AGENT_DATA_DIR', '/tmp/video_agent_data'))
_JOB_DB_PATH = Path(os.getenv('JOB_DB_PATH', str(_JOB_DB_DIR / 'jobs.db')))

def _init_job_db():
    _JOB_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(_JOB_DB_PATH)) as conn:
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                result TEXT,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()

def _store_job(job_id: str, status: str, result: Dict = None, error: str = None):
    with contextlib.closing(sqlite3.connect(_JOB_DB_PATH)) as conn:
        result_json = _json.dumps(result) if result else None
        conn.execute(
            'INSERT OR REPLACE INTO jobs (job_id, status, result, error) VALUES (?, ?, ?, ?)',
            (job_id, status, result_json, error)
        )
        conn.commit()

def _get_job(job_id: str) -> Dict:
    with contextlib.closing(sqlite3.connect(_JOB_DB_PATH)) as conn:
        row = conn.execute(
            'SELECT status, result, error FROM jobs WHERE job_id = ?', (job_id,)
        ).fetchone()
        if row:
            return {
                'status': row[0],
                'result': _json.loads(row[1]) if row[1] else None,
                'error': row[2]
            }
        return None

_job_lock = threading.Lock()


@app.on_event("startup")
def _startup() -> None:
    _init_job_db()
    cfg.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cfg.OUTPUT_DIR.chmod(0o755)
    log.info("startup: host=%s port=%s output=%s job_db=%s", cfg.HOST, cfg.PORT, cfg.OUTPUT_DIR, _JOB_DB_PATH)


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "worker": "video-clipper-agent",
        "config": {
            "host": cfg.HOST,
            "port": cfg.PORT,
            "aspect": cfg.DEFAULT_ASPECT,
            "num_clips": cfg.DEFAULT_NUM_CLIPS,
            "max_clips": cfg.MAX_NUM_CLIPS,
        },
        "providers": {
            p["model"]: bool(p["api_key"])
            for p in cfg.PROVIDER_ORDER
        },
    }


def _process_clip_job(
    job_id: str,
    youtube_url: str,
    campaign_id: str,
    campaign_name: str,
    num_clips: int,
    submit: bool,
    max_download_seconds: int = DEFAULT_MAX_DOWNLOAD_SECONDS,
) -> None:
    """Background task: run the full clip pipeline and store results in _job_store."""
    from .clipper import ClipError as ClipPipelineError
    from .pipeline import run_decision_engine

    started = time.time()
    try:
        result = run_decision_engine(
            url=youtube_url,
            n8n_url="",
            campaign_id=campaign_id,
            campaign_name=campaign_name,
            num_clips=min(max(num_clips, 1), cfg.MAX_NUM_CLIPS),
            use_llm=cfg.USE_LLM,
            use_heuristic=not cfg.USE_LLM,
            model_size="tiny",
            chunked=True,
            poll_performance=False,
            max_download_seconds=max_download_seconds,
        )

        manifest: Dict[str, Any] = {
            "job_id": job_id,
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "youtube_url": youtube_url,
            "status": "completed",
            "num_clips": len(result["manifest"].get("clips", [])),
            "duration": result["manifest"].get("transcript_duration", 0.0),
            "clips": result["manifest"].get("clips", []),
            "took_seconds": round(time.time() - started, 3),
        }

        if cfg.CAMPAIGN_WEBHOOK_URL and submit:
            _submit_to_webhook(manifest)
            manifest["submitted"] = True

        with _job_lock:
            _store_job(job_id, "completed", result=manifest, error=None)
        log.info("job %s complete: %s -> %d clips in %.1fs", job_id, youtube_url, len(manifest["clips"]), manifest["took_seconds"])

    except ClipPipelineError as e:
        log.error("job %s failed: %s", job_id, e)
        with _job_lock:
            _store_job(job_id, "error", result=None, error=f"clipping failed: {e}")
    except Exception as e:
        log.exception("job %s unexpected error", job_id)
        with _job_lock:
            _store_job(job_id, "error", result=None, error=f"unexpected error: {type(e).__name__}: {e}")


@app.post("/api/clip")
def clip(payload: Dict[str, Any], background_tasks: BackgroundTasks) -> JSONResponse:
    """Start the clipper on a YouTube URL asynchronously.

    Returns immediately with a job_id. Poll /api/status/{job_id} for results.

    Expected body:
    {
      "youtube_url": "https://www.youtube.com/watch?v=...",
      "campaign_id": "string",     # optional, echoed back for campaign submission
      "campaign_name": "string",   # optional
      "num_clips": 3,              # optional
      "aspect": "9:16",            # optional
      "submit": true               # optional: also POST results to CAMPAIGN_WEBHOOK_URL
    }
    """
    youtube_url = (payload.get("youtube_url") or "").strip()
    if not youtube_url:
        raise HTTPException(status_code=400, detail="missing required field: youtube_url")

    num_clips = int(payload.get("num_clips", cfg.DEFAULT_NUM_CLIPS))
    campaign_id = payload.get("campaign_id") or str(_uuid.uuid4())
    campaign_name = payload.get("campaign_name", "unknown")
    submit = bool(payload.get("submit", False))
    max_download_seconds = int(payload.get("max_download_seconds", DEFAULT_MAX_DOWNLOAD_SECONDS))

    job_id = str(_uuid.uuid4())
    _store_job(job_id, "processing", result=None, error=None)

    background_tasks.add_task(
        _process_clip_job,
        job_id,
        youtube_url,
        campaign_id,
        campaign_name,
        num_clips,
        submit,
        max_download_seconds,
    )

    log.info("job %s started: %s", job_id, youtube_url)
    return JSONResponse(content={
        "job_id": job_id,
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "youtube_url": youtube_url,
        "status": "processing",
        "num_clips": num_clips,
        "message": "processing started; poll /api/status/{job_id}",
    })


@app.get("/api/status/{job_id}")
def get_status(job_id: str) -> JSONResponse:
    """Check the status of a clip job. Returns status and results (if completed).

    Response when processing: {"status": "processing"}
    Response when completed:  {"status": "completed", "result": {...}}
    Response when error:      {"status": "error", "error": "..."}
    """
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return JSONResponse(content=job)
# ===== Queue endpoints for Miget worker integration =====

@app.get("/api/queue")
def get_queue(limit: int = 10):
    """Get pending jobs for Miget workers to claim."""
    with contextlib.closing(_get_conn()) as conn:
        rows = conn.execute(
            'SELECT job_id, campaign_id, campaign_name, youtube_url, aspect, '
            'num_clips, status, current_stage, progress, created_at, updated_at '
            'FROM jobs WHERE status = ? ORDER BY created_at LIMIT ?',
            ('pending', limit),
        ).fetchall()
    jobs = []
    for row in rows:
        jobs.append({
            'job_id': row[0], 'campaign_id': row[1], 'campaign_name': row[2],
            'youtube_url': row[3], 'aspect': row[4], 'num_clips': row[5],
            'status': row[6], 'stage': row[7], 'progress': row[8],
            'created_at': row[9], 'updated_at': row[10],
        })
    return {"jobs": jobs, "count": len(jobs)}

@app.post("/api/dispatch")
def dispatch_job(job_id: str, agent_url: str = "miget-worker"):
    """Claim a job for processing (Miget worker calls this)."""
    with contextlib.closing(_get_conn()) as conn:
        conn.execute(
            'UPDATE jobs SET status = ?, current_stage = ?, progress = ?, '
            'updated_at = ? WHERE job_id = ? AND status = ?',
            ('dispatched', 'download', 0, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
             job_id, 'pending'),
        )
        conn.commit()
        row = conn.execute(
            'SELECT * FROM jobs WHERE job_id = ?', (job_id,)
        ).fetchone()
    if row:
        job = dict(row)
        job.pop('result', None)
        job.pop('error', None)
        job['agent_url'] = agent_url
        log.info(f"Dispatched job {job_id} to {agent_url}")
        return {"job": job, "message": "Job dispatched"}
    return {"error": "Job not found or not pending"}

@app.get("/api/jobs")
def list_jobs(status: Optional[str] = None):
    """List jobs with optional status filter."""
    with contextlib.closing(_get_conn()) as conn:
        if status:
            rows = conn.execute(
                'SELECT job_id, campaign_id, campaign_name, youtube_url, status, '
                'current_stage, progress, created_at, updated_at FROM jobs '
                'WHERE status = ? ORDER BY created_at DESC',
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT job_id, campaign_id, campaign_name, youtube_url, status, '
                'current_stage, progress, created_at, updated_at FROM jobs '
                'ORDER BY created_at DESC LIMIT 50'
            ).fetchall()
    jobs = []
    for row in rows:
        jobs.append({
            'job_id': row[0], 'campaign_id': row[1], 'campaign_name': row[2],
            'youtube_url': row[3], 'status': row[4], 'stage': row[5],
            'progress': row[6], 'created_at': row[7], 'updated_at': row[8],
        })
    return {"jobs": jobs, "count": len(jobs)}

@app.post("/api/job")
def create_new_job(body: Dict[str, Any]):
    """Create a new job from n8n (for split architecture)."""
    required = ["campaign_id", "campaign_name", "youtube_url"]
    for field in required:
        if field not in body:
            raise HTTPException(status_code=400, detail=f"Missing required field: {field}")
    job_id = str(_uuid.uuid4())
    _store_job(job_id, "pending", stage="queue", progress=0)
    log.info(f"Created job {job_id} for campaign {body['campaign_id']}")
    return {"job_id": job_id, **body, "status": "pending"}

# ===== End queue endpoints =====



def _submit_to_webhook(manifest: Dict[str, Any]) -> None:
    """Fire-and-forget POST of the clip manifest to the campaign webhook."""
    import requests as _req

    webhook = cfg.CAMPAIGN_WEBHOOK_URL
    try:
        _req.post(webhook, json=manifest, timeout=cfg.CAMPAIGN_SUBMIT_TIMEOUT)
        log.info("submitted manifest to %s", webhook)
    except Exception as e:
        log.warning("campaign webhook submit failed: %s", e)


@app.get("/clips")
def list_clips() -> Dict[str, Any]:
    """List artifacts in the output dir (bounded listing)."""
    out = cfg.OUTPUT_DIR
    if not out.exists():
        return {"output_dir": str(out), "clips": []}
    clips = []
    for p in sorted(out.iterdir()):
        if p.suffix.lower() in (".mp4", ".m4v"):
            clips.append({"name": p.name, "size": p.stat().st_size, "path": str(p)})
    return {"output_dir": str(out), "clips": clips}


@app.get("/clips/{name}")
def get_clip(name: str):
    """Serve one rendered clip so it has a public https URL (needed by uploaders)."""
    from fastapi.responses import FileResponse

    safe = Path(name).name
    path = cfg.OUTPUT_DIR / safe
    if safe != name or not path.is_file() or path.suffix.lower() not in (".mp4", ".m4v"):
        raise HTTPException(status_code=404, detail="clip not found")
    return FileResponse(str(path), media_type="video/mp4", filename=safe)


@app.get("/api/debug")
def api_debug() -> Dict[str, Any]:
    """Diagnostic endpoint to verify basic request handling works."""
    return {
        "status": "ok",
        "output_dir": str(cfg.OUTPUT_DIR),
        "output_dir_exists": cfg.OUTPUT_DIR.exists(),
        "output_dir_writable": os.access(cfg.OUTPUT_DIR, os.W_OK),
    }

@app.get("/api/debug/model")
def api_debug_model() -> Dict[str, Any]:
    """Test if faster-whisper model loads on Render."""
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("tiny", device="auto", compute_type="auto")
        return {"status": "ok", "model_loaded": True}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}

@app.get("/api/debug/av")
def api_debug_av() -> Dict[str, Any]:
    """Test if PyAV/av works on Render."""
    try:
        import av
        return {
            "status": "ok",
            "av_version": getattr(av, "__version__", "unknown"),
            "has_codec": av.codec is not None,
            "has_container": av.container is not None,
        }
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}

@app.get("/api/debug/ffmpeg")
def api_debug_ffmpeg() -> Dict[str, Any]:
    """Test if ffmpeg CLI works on Render."""
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10)
        return {
            "status": "ok" if result.returncode == 0 else "error",
            "returncode": result.returncode,
            "version": result.stdout.split("\n")[0] if result.stdout else "",
            "stderr": result.stderr[:200] if result.stderr else "",
        }
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}

@app.post("/api/upload-test")
def upload_test(file: UploadFile = File(...)) -> Dict[str, Any]:
    """Minimal upload test - just returns file info, no pipeline."""
    content = file.file.read()
    return {
        "filename": file.filename,
        "size": len(content),
        "content_type": file.content_type,
    }

@app.get("/api/debug/pipeline")
def api_debug_pipeline() -> Dict[str, Any]:
    """Run each pipeline step individually to find which one fails."""
    results = {}
    video_path = Path("/tmp/debug_sample.mp4")
    if not video_path.exists():
        import urllib.request
        urllib.request.urlretrieve("https://www.w3schools.com/html/mov_bbb.mp4", video_path)

    # Step 1: Model loading
    try:
        from agent.transcriber import get_model
        model = get_model("tiny")
        results["model"] = "ok"
    except Exception as e:
        results["model"] = f"error: {type(e).__name__}: {e}"
        return results

    # Step 2: Audio extraction via ffmpeg
    try:
        from agent.transcriber import _extract_audio_ffmpeg
        audio = _extract_audio_ffmpeg(video_path)
        results["audio_extract"] = f"ok (shape={audio.shape}, len={len(audio)})"
    except Exception as e:
        results["audio_extract"] = f"error: {type(e).__name__}: {e}"
        return results

    # Step 3: Transcription with numpy array
    try:
        generator, info = model.transcribe(
            audio, language="en", word_timestamps=True,
            vad_filter=True, beam_size=5,
        )
        segments = list(generator)
        results["transcribe"] = f"ok ({len(segments)} segments)"
    except Exception as e:
        results["transcribe"] = f"error: {type(e).__name__}: {e}"
        return results

    results["all_steps"] = "ok"
    return results

@app.post("/api/upload")
async def upload_clip(request: Request, file: UploadFile = File(None)):
    """Process a locally-uploaded video file (no YouTube download needed).

    Accepts a multipart/form-data upload with a single 'file' field containing
    an MP4 video. The pipeline transcribes, selects highlights, and renders clips
    just like /api/clip, but works with any local video file.

    This is the workaround for the Render free tier: the sandbox has no outbound
    internet access to YouTube, so you cannot test /api/clip with a real URL.
    Upload your own video instead.

    Returns: JSON manifest (same shape as /api/clip response).
    """
    import json as _json
    import uuid as _uuid

    # Check if JSON body with youtube_url was sent
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
            youtube_url = (body.get("youtube_url") or "").strip()
            campaign_id = body.get("campaign_id")
            campaign_name = body.get("campaign_name", "unknown")
            num_clips = int(body.get("num_clips", cfg.DEFAULT_NUM_CLIPS))

            if youtube_url:
                from .clipper import ClipError as ClipPipelineError
                from .pipeline import run_decision_engine
                import os

                if not campaign_id:
                    campaign_id = str(_uuid.uuid4())
                if not campaign_name:
                    campaign_name = "unknown"

                num_clips = min(max(num_clips, 1), cfg.MAX_NUM_CLIPS)

                started = time.time()
                try:
                    result = run_decision_engine(
                        url=youtube_url,
                        n8n_url="",
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        num_clips=num_clips,
                        use_llm=cfg.USE_LLM,
                        use_heuristic=not cfg.USE_LLM,
                        model_size="tiny",
                        chunked=True,
                        poll_performance=False,
                        max_download_seconds=int(body.get("max_download_seconds", DEFAULT_MAX_DOWNLOAD_SECONDS)),
                    )
                except ClipPipelineError as e:
                    log.error("clip pipeline failed for %s: %s", youtube_url, e)
                    raise HTTPException(status_code=502, detail=f"clipping failed: {e}")
                except Exception as e:
                    log.exception("unexpected error clipping %s", youtube_url)
                    raise HTTPException(status_code=500, detail=f"unexpected error: {e}")

                manifest: Dict[str, Any] = {
                    "job_id": os.getenv("AGENT_JOB_ID", "unknown"),
                    "campaign_id": campaign_id,
                    "campaign_name": campaign_name,
                    "youtube_url": youtube_url,
                    "status": "completed",
                    "num_clips": len(result["manifest"].get("clips", [])),
                    "duration": result["manifest"].get("transcript_duration", 0.0),
                    "clips": result["manifest"].get("clips", []),
                    "took_seconds": round(time.time() - started, 3),
                }
                log.info("clip complete: %s -> %d clips in %.1fs", youtube_url, len(manifest["clips"]), manifest["took_seconds"])
                return JSONResponse(content=manifest)
        except Exception:
            pass

    # If no JSON body or no youtube_url, expect a file upload
    if file is None or not file.filename:
        raise HTTPException(
            status_code=400,
            detail="No file or youtube_url provided. Upload a video file or send JSON with 'youtube_url'."
        )

    # Original file upload logic continues below
    try:
        from .clipper import ClipError as ClipPipelineError
        from .pipeline import process_local_video
        import os
        import uuid
        import tempfile

        started = time.time()
        ext = Path(file.filename or "").suffix.lower()
        if ext not in (".mp4", ".m4v", ".mov", ".avi", ".mkv"):
            raise HTTPException(status_code=400, detail=f"unsupported video extension: {ext}")

        # Save to a working directory
        tmp = tempfile.mkdtemp(dir=cfg.OUTPUT_DIR)
        log.info(f"upload: created tmp dir {tmp}")
        video_path = Path(tmp) / file.filename
        content = file.file.read()
        log.info(f"upload: read {len(content)} bytes from {file.filename}")
        with open(video_path, "wb") as f:
            f.write(content)
        log.info(f"upload: saved to {video_path}")

        campaign_id = str(uuid.uuid4())
        campaign_name = f"upload_{campaign_id[:8]}"

        result = process_local_video(
            video_path=video_path,
            work_dir=Path(tmp),
            campaign_id=campaign_id,
            campaign_name=campaign_name,
            num_clips=cfg.DEFAULT_NUM_CLIPS,
            model_size="tiny",
            chunked=True,
        )
        log.info(f"upload: pipeline returned result type={type(result).__name__}")
    except ClipPipelineError as e:
        log.error("upload pipeline failed: %s", e)
        raise HTTPException(status_code=502, detail=f"upload failed: {e}")
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log.error("unexpected upload error:\n%s", tb)
        raise HTTPException(status_code=500, detail=f"upload failed: {type(e).__name__}: {e}\n{tb[:500]}")

    try:
        manifest_result = result.get("manifest", {}) if result else {}
        manifest: Dict[str, Any] = {
            "job_id": os.getenv("AGENT_JOB_ID", "unknown"),
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "youtube_url": None,
            "uploaded_file": file.filename,
            "status": "completed",
            "num_clips": len(manifest_result.get("clips", [])),
            "duration": manifest_result.get("transcript_duration", 0.0),
            "clips": manifest_result.get("clips", []),
            "took_seconds": round(time.time() - started, 3),
        }
    except Exception as e:
        log.exception("manifest construction failed")
        raise HTTPException(status_code=500, detail=f"manifest failed: {type(e).__name__}: {e}")

    log.info("upload complete: %s -> %d clips in %.1fs", file.filename, len(manifest["clips"]), manifest["took_seconds"])
    return JSONResponse(content=manifest)


@app.post("/api/poll")
def api_poll() -> Dict[str, Any]:
    """Trigger a Content Rewards analytics poll cycle (no credential needed)."""
    from agent.poller import ContentRewardsPoller

    poller = ContentRewardsPoller()
    processed = poller.poll()
    return {"processed": processed, "status": "ok"}




# ---------------------------------------------------------------------------
# Modular Pipeline Stages
# Each stage is a lightweight HTTP endpoint that can be called independently
# by the n8n workflow. State is persisted in SQLite and files.
# ---------------------------------------------------------------------------

from .pipeline import DecisionEngine, IdempotencyCache, PostedVideosTracker, PerformanceRecorder
from .clipper import ClipError, render_clip, build_ass_subtitles
from .transcriber import transcribe, get_model
from .highlight_selection import UnifiedHighlightSelector
from .heuristic import score_segments
from .llm import prompt_highlights
from .llm_retry import LLMClient, LLMProvider, RetryConfig

# ---------------------------------------------------------------------------
# Stage 1: Download
# ---------------------------------------------------------------------------
@app.post("/api/stage/download")
def stage_download(payload: Dict[str, Any]) -> JSONResponse:
    """Download a YouTube video and return the local file path.

    Input:  { "youtube_url": "https://...", "campaign_id": "...", "campaign_name": "...", "num_clips": 3 }
    Output: { "video_path": "/tmp/.../video.mp4", "duration": 123.4, "campaign_id": "...", "campaign_name": "...", "num_clips": 3 }
    """
    youtube_url = (payload.get("youtube_url") or "").strip()
    if not youtube_url:
        raise HTTPException(status_code=400, detail="missing required field: youtube_url")

    campaign_id = payload.get("campaign_id") or str(_uuid.uuid4())
    campaign_name = payload.get("campaign_name", "unknown")
    num_clips = min(int(payload.get("num_clips", cfg.DEFAULT_NUM_CLIPS)), cfg.MAX_NUM_CLIPS)
    max_download_seconds = int(payload.get("max_download_seconds", cfg.DEFAULT_MAX_DOWNLOAD_SECONDS))

    job_id = str(_uuid.uuid4())
    _store_job(job_id, "downloading", result={"campaign_id": campaign_id}, error=None)

    started = time.time()
    try:
        engine = DecisionEngine(
            n8n_url="",
            cache=IdempotencyCache(),
            perf_recorder=PerformanceRecorder(),
            posted_videos_tracker=PostedVideosTracker(),
            highlight_selector=UnifiedHighlightSelector(),
            webhook_url=cfg.CAMPAIGN_WEBHOOK_URL,
        )
        video_path = engine.download(
            url=youtube_url,
            campaign_id=campaign_id,
            campaign_name=campaign_name,
            num_clips=num_clips,
            max_download_seconds=max_download_seconds,
        )

        manifest = {
            "job_id": job_id,
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "video_path": str(video_path),
            "duration": engine._get_video_duration(video_path),
            "num_clips": num_clips,
            "status": "completed",
            "took_seconds": round(time.time() - started, 3),
        }

        with _job_lock:
            _store_job(job_id, "completed", result=manifest, error=None)
        log.info("stage download complete: %s -> %.1fs", job_id, manifest["took_seconds"])
        return JSONResponse(content=manifest)

    except Exception as e:
        log.exception("stage download failed")
        with _job_lock:
            _store_job(job_id, "error", result=None, error=f"download failed: {type(e).__name__}: {e}")
        raise


# ---------------------------------------------------------------------------
# Stage 2: Transcribe
# ---------------------------------------------------------------------------
@app.post("/api/stage/transcribe")
def stage_transcribe(payload: Dict[str, Any]) -> JSONResponse:
    """Transcribe audio from a video file.

    Input:  { "video_path": "/tmp/.../video.mp4", "model_size": "tiny", "campaign_id": "..." }
    Output: { "transcript": [...], "duration": 123.4, "transcription_duration": 45.2, "campaign_id": "..." }
    """
    video_path = payload.get("video_path") or ""
    if not video_path:
        raise HTTPException(status_code=400, detail="missing required field: video_path")

    model_size = payload.get("model_size", "tiny")
    campaign_id = payload.get("campaign_id") or str(_uuid.uuid4())
    job_id = str(_uuid.uuid4())

    _store_job(job_id, "transcribing", result={"campaign_id": campaign_id}, error=None)

    started = time.time()
    try:
        video_path = Path(video_path)
        if not video_path.exists():
            raise HTTPException(status_code=400, detail=f"video not found: {video_path}")

        model = get_model(model_size)
        transcript = transcribe(
            audio=str(video_path),
            model=model,
            language="en",
            word_timestamps=True,
            vad_filter=True,
            beam_size=5,
        )

        # Flatten generator into list for JSON serialization
        transcript_list = []
        for segment in transcript:
            transcript_list.append({
                "start": segment.start,
                "end": segment.end,
                "text": segment.text,
            })

        manifest = {
            "job_id": job_id,
            "campaign_id": campaign_id,
            "transcript": transcript_list,
            "duration": transcript_list[-1]["end"] if transcript_list else 0.0,
            "transcription_duration": round(time.time() - started, 3),
            "status": "completed",
        }

        with _job_lock:
            _store_job(job_id, "completed", result=manifest, error=None)
        log.info("stage transcribe complete: %.1fs", time.time() - started)
        return JSONResponse(content=manifest)

    except Exception as e:
        log.exception("stage transcribe failed")
        with _job_lock:
            _store_job(job_id, "error", result=None, error=f"transcribe failed: {type(e).__name__}: {e}")
        raise


# ---------------------------------------------------------------------------
# Stage 3: Highlights
# ---------------------------------------------------------------------------
@app.post("/api/stage/highlights")
def stage_highlights(payload: Dict[str, Any]) -> JSONResponse:
    """Select highlight windows from transcript.

    Input:  { "transcript": [...], "num_clips": 3, "campaign_id": "...", "model_size": "tiny" }
    Output: { "highlights": [{ "start": 10.5, "end": 18.2, "text": "..." }], "num_clips": 3, "status": "completed" }
    """
    transcript = payload.get("transcript") or []
    num_clips = min(int(payload.get("num_clips", cfg.DEFAULT_NUM_CLIPS)), cfg.MAX_NUM_CLIPS)
    campaign_id = payload.get("campaign_id") or str(_uuid.uuid4())
    model_size = payload.get("model_size", "tiny")
    job_id = str(_uuid.uuid4())

    _store_job(job_id, "highlights", result={"campaign_id": campaign_id}, error=None)

    started = time.time()
    try:
        # Build segments list from transcript
        segments = []
        for item in transcript:
            try:
                segments.append({
                    "start": float(item.get("start", 0)),
                    "end": float(item.get("end", 0)),
                    "text": str(item.get("text", "")),
                })
            except (ValueError, TypeError):
                continue

        # Use LLM if USE_LLM is True, otherwise use heuristic
        use_llm = cfg.USE_LLM

        if use_llm and segments:
            # Load LLM client
            provider = LLMProvider.OPENROUTER if cfg.USE_LLM else LLMProvider.HEURISTIC
            client = LLMClient(
                provider=provider,
                base_url=cfg.LLM_BASE_URL,
                api_key=cfg.LLM_API_KEY,
                model=cfg.LLM_MODEL,
                max_retries=cfg.LLM_MAX_RETRIES,
            )
            try:
                highlights = prompt_highlights(
                    segments=segments,
                    num_clips=num_clips,
                    client=client,
                    model=model_size,
                )
            except Exception as e:
                log.warning(f"LLM highlight selection failed: {e}, falling back to heuristic")
                highlights = None
        else:
            highlights = None

        if highlights is None:
            # Fallback: use heuristic scoring
            scores = score_segments(segments)
            highlights = []
            for i, (start, end, score) in enumerate(scores[:num_clips]):
                highlights.append({
                    "start": start,
                    "end": end,
                    "score": float(score),
                    "text": segments[i].get("text", "") if i < len(segments) else "",
                })

        manifest = {
            "job_id": job_id,
            "campaign_id": campaign_id,
            "highlights": highlights,
            "num_clips": len(highlights),
            "transcript_duration": segments[-1]["end"] if segments else 0.0,
            "selection_method": "llm" if use_llm else "heuristic",
            "selection_duration": round(time.time() - started, 3),
            "status": "completed",
        }

        with _job_lock:
            _store_job(job_id, "completed", result=manifest, error=None)
        log.info("stage highlights complete: %s", manifest["selection_method"])
        return JSONResponse(content=manifest)

    except Exception as e:
        log.exception("stage highlights failed")
        with _job_lock:
            _store_job(job_id, "error", result=None, error=f"highlights failed: {type(e).__name__}: {e}")
        raise


# ---------------------------------------------------------------------------
# Stage 4: Render
# ---------------------------------------------------------------------------
@app.post("/api/stage/render")
def stage_render(payload: Dict[str, Any]) -> JSONResponse:
    """Render video clips from highlight windows.

    Input:  { "video_path": "/tmp/.../video.mp4", "highlights": [...], "campaign_id": "...", "num_clips": 3 }
    Output: { "clips": [{ "clip_id": "...", "start": 10.5, "end": 18.2, "path": "/tmp/.../clip.mp4", "size": 1234567 }], "status": "completed" }
    """
    video_path = payload.get("video_path") or ""
    highlights = payload.get("highlights") or []
    if not video_path or not highlights:
        raise HTTPException(status_code=400, detail="missing video_path or highlights")

    campaign_id = payload.get("campaign_id") or str(_uuid.uuid4())
    num_clips = min(int(payload.get("num_clips", cfg.DEFAULT_NUM_CLIPS)), cfg.MAX_NUM_CLIPS)
    aspect = payload.get("aspect", cfg.DEFAULT_ASPECT)
    job_id = str(_uuid.uuid4())

    _store_job(job_id, "rendering", result={"campaign_id": campaign_id}, error=None)

    started = time.time()
    try:
        video_path = Path(video_path)
        if not video_path.exists():
            raise HTTPException(status_code=400, detail=f"video not found: {video_path}")

        output_dir = cfg.OUTPUT_DIR / campaign_id
        output_dir.mkdir(parents=True, exist_ok=True)

        clips = []
        for i, hl in enumerate(highlights[:num_clips]):
            clip_id = f"{campaign_id}_clip_{i:03d}"
            clip_path = output_dir / f"{clip_id}.mp4"

            try:
                render_clip(
                    source_path=video_path,
                    start=float(hl.get("start", 0)),
                    end=float(hl.get("end", 0)),
                    output_path=clip_path,
                    aspect=aspect,
                )
                clips.append({
                    "clip_id": clip_id,
                    "start": float(hl.get("start", 0)),
                    "end": float(hl.get("end", 0)),
                    "path": str(clip_path),
                    "size": clip_path.stat().st_size if clip_path.exists() else 0,
                    "status": "rendered",
                })
            except Exception as e:
                log.warning(f"render failed for {clip_id}: {e}")
                clips.append({
                    "clip_id": clip_id,
                    "start": float(hl.get("start", 0)),
                    "end": float(hl.get("end", 0)),
                    "path": str(clip_path),
                    "size": 0,
                    "status": "failed",
                    "error": str(e),
                })

        manifest = {
            "job_id": job_id,
            "campaign_id": campaign_id,
            "clips": clips,
            "num_clips": len(clips),
            "render_duration": round(time.time() - started, 3),
            "status": "completed",
        }

        with _job_lock:
            _store_job(job_id, "completed", result=manifest, error=None)
        log.info("stage render complete: %d clips", len(clips))
        return JSONResponse(content=manifest)

    except Exception as e:
        log.exception("stage render failed")
        with _job_lock:
            _store_job(job_id, "error", result=None, error=f"render failed: {type(e).__name__}: {e}")
        raise


# ---------------------------------------------------------------------------
# Stage 5: Submit
# ---------------------------------------------------------------------------
@app.post("/api/stage/submit")
def stage_submit(payload: Dict[str, Any]) -> JSONResponse:
    """Submit rendered clips to platforms (YouTube, Instagram, TikTok).

    Input:  { "clips": [...], "campaign_id": "...", "campaign_name": "...", "campaign_url": "..." }
    Output: { "posted": [...], "status": "completed" }
    """
    clips = payload.get("clips") or []
    campaign_id = payload.get("campaign_id") or str(_uuid.uuid4())
    campaign_name = payload.get("campaign_name", "unknown")
    if not clips:
        raise HTTPException(status_code=400, detail="missing clips")

    job_id = str(_uuid.uuid4())
    _store_job(job_id, "submitting", result={"campaign_id": campaign_id}, error=None)

    started = time.time()
    try:
        engine = DecisionEngine(
            n8n_url="",
            cache=IdempotencyCache(),
            perf_recorder=PerformanceRecorder(),
            posted_videos_tracker=PostedVideosTracker(),
            highlight_selector=UnifiedHighlightSelector(),
            webhook_url=cfg.CAMPAIGN_WEBHOOK_URL,
        )
        engine.posted_videos = PostedVideosTracker()

        posted = engine._post_to_platforms(
            clips=clips,
            campaign_id=campaign_id,
            campaign_name=campaign_name,
            cpm=0.0,
        )

        manifest = {
            "job_id": job_id,
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "posted": posted,
            "submit_duration": round(time.time() - started, 3),
            "status": "completed",
        }

        with _job_lock:
            _store_job(job_id, "completed", result=manifest, error=None)
        log.info("stage submit complete: %d posts", len(posted))
        return JSONResponse(content=manifest)

    except Exception as e:
        log.exception("stage submit failed")
        with _job_lock:
            _store_job(job_id, "error", result=None, error=f"submit failed: {type(e).__name__}: {e}")
        raise


# ---------------------------------------------------------------------------
# Pipeline Orchestration
# ---------------------------------------------------------------------------

