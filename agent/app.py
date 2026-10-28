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
from pathlib import Path
from typing import Any, Dict

import threading
import uuid as _uuid

from fastapi import FastAPI, HTTPException, File, UploadFile, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from . import config as cfg

log = logging.getLogger("agent.app")

app = FastAPI(
    title="Video Clipper Agent",
    description="Clip the most viral moments from a YouTube link (Snap Deploy service).",
    version="0.1.0",
)

app.state.workspace = Path(cfg.OUTPUT_DIR)

_job_store: Dict[str, Dict[str, Any]] = {}
_job_lock = threading.Lock()


@app.on_event("startup")
def _startup() -> None:
    cfg.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cfg.OUTPUT_DIR.chmod(0o755)
    log.info("startup: host=%s port=%s output=%s", cfg.HOST, cfg.PORT, cfg.OUTPUT_DIR)


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
            _job_store[job_id] = {
                "status": "completed",
                "result": manifest,
                "error": None,
            }
        log.info("job %s complete: %s -> %d clips in %.1fs", job_id, youtube_url, len(manifest["clips"]), manifest["took_seconds"])

    except ClipPipelineError as e:
        log.error("job %s failed: %s", job_id, e)
        with _job_lock:
            _job_store[job_id] = {
                "status": "error",
                "result": None,
                "error": f"clipping failed: {e}",
            }
    except Exception as e:
        log.exception("job %s unexpected error", job_id)
        with _job_lock:
            _job_store[job_id] = {
                "status": "error",
                "result": None,
                "error": f"unexpected error: {type(e).__name__}: {e}",
            }


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

    job_id = str(_uuid.uuid4())
    with _job_lock:
        _job_store[job_id] = {
            "status": "processing",
            "result": None,
            "error": None,
        }

    background_tasks.add_task(
        _process_clip_job,
        job_id,
        youtube_url,
        campaign_id,
        campaign_name,
        num_clips,
        submit,
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
    with _job_lock:
        job = _job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return JSONResponse(content=job)


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
    video_path = Path("/c/Users/lenovo/video-agent/clips-studio/assets/outro/outro2_1080x1920_30_yuv420p_48000_2.mp4")

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
