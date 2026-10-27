"""agent/app.py — FastAPI HTTP surface.

This is the single endpoint n8n's "HTTP Request" node calls. The agent is a
blind builder: it returns JSON only, logs to stdout, and raises ClipperError
(upstream ClipperError) with clear messages instead of silent failures.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, File, UploadFile
from fastapi.responses import JSONResponse

from . import config as cfg

log = logging.getLogger("agent.app")

app = FastAPI(
    title="Video Clipper Agent",
    description="Clip the most viral moments from a YouTube link (Snap Deploy service).",
    version="0.1.0",
)

app.state.workspace = Path(cfg.OUTPUT_DIR)


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


@app.post("/api/clip")
def clip(payload: Dict[str, Any]) -> JSONResponse:
    """Run the clipper on a YouTube URL and return the clip manifest.

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
    # Lazy imports: these pull in heavy packages (faster-whisper, openai, etc.)
    # that would otherwise slow the container's startup past Render's health
    # check window on the free tier. Import inside the function instead.
    from .clipper import ClipError as ClipPipelineError
    from .pipeline import IdempotencyCache
    from .pipeline import run_decision_engine

    youtube_url = (payload.get("youtube_url") or "").strip()
    if not youtube_url:
        raise HTTPException(status_code=400, detail="missing required field: youtube_url")

    num_clips = int(payload.get("num_clips", cfg.DEFAULT_NUM_CLIPS))
    aspect = str(payload.get("aspect", cfg.DEFAULT_ASPECT))

    # Build a campaign context (required by run_decision_engine)
    import uuid as _uuid
    campaign_id = payload.get("campaign_id") or str(_uuid.uuid4())
    campaign_name = payload.get("campaign_name", "unknown")

    started = time.time()
    try:
        result = run_decision_engine(
            url=youtube_url,
            n8n_url="",  # the route posts to the webhook itself (below), only if submit is true
            campaign_id=campaign_id,
            campaign_name=campaign_name,
            num_clips=min(max(num_clips, 1), cfg.MAX_NUM_CLIPS),
            use_llm=cfg.USE_LLM,
            use_heuristic=not cfg.USE_LLM,
            model_size="tiny",
            chunked=True,
            poll_performance=False,
        )
    except ClipPipelineError as e:
        log.error("clip pipeline failed for %s: %s", youtube_url, e)
        raise HTTPException(status_code=502, detail=f"clipping failed: {e}")
    except Exception as e:  # noqa: BLE001 - surface any unexpected error cleanly
        log.exception("unexpected error clipping %s", youtube_url)
        raise HTTPException(status_code=500, detail=f"unexpected error: {e}")

    manifest: Dict[str, Any] = {
        "job_id": os.getenv("AGENT_JOB_ID", "unknown"),
        "campaign_id": payload.get("campaign_id"),
        "campaign_name": payload.get("campaign_name"),
        "youtube_url": youtube_url,
        "status": "completed",
        "num_clips": len(result["manifest"].get("clips", [])),
        "duration": result["manifest"].get("transcript_duration", 0.0),
        "clips": result["manifest"].get("clips", []),
        "took_seconds": round(time.time() - started, 3),
    }

    if cfg.CAMPAIGN_WEBHOOK_URL and bool(payload.get("submit", False)):
        _submit_to_webhook(manifest)
        manifest["submitted"] = True

    log.info("clip complete: %s -> %d clips in %.1fs", youtube_url, len(manifest["clips"]), manifest["took_seconds"])
    return JSONResponse(content=manifest)


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

@app.post("/api/upload-test")
def upload_test(file: UploadFile = File(...)) -> Dict[str, Any]:
    """Minimal upload test - just returns file info, no pipeline."""
    content = file.file.read()
    return {
        "filename": file.filename,
        "size": len(content),
        "content_type": file.content_type,
    }

@app.post("/api/upload")
def upload_clip(file: UploadFile = File(...)):
    """Process a locally-uploaded video file (no YouTube download needed).

    Accepts a multipart/form-data upload with a single 'file' field containing
    an MP4 video. The pipeline transcribes, selects highlights, and renders clips
    just like /api/clip, but works with any local video file.

    This is the workaround for the Render free tier: the sandbox has no outbound
    internet access to YouTube, so you cannot test /api/clip with a real URL.
    Upload your own video instead.

    Returns: JSON manifest (same shape as /api/clip response).
    """
    from .clipper import ClipError as ClipPipelineError
    from .pipeline import process_local_video
    import os
    import uuid
    import tempfile

    started = time.time()
    try:
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
        log.exception("unexpected upload error")
        raise HTTPException(status_code=500, detail=f"upload failed: {type(e).__name__}: {e}")

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
