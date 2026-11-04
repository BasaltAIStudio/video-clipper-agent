"""agent/server.py — Extended FastAPI app with job queue endpoints.

Additions over agent/app.py:
  - /api/queue - GET: list pending jobs for Miget workers to claim
  - /api/dispatch - POST: claim a job for processing
  - /api/status - GET: get job status (n8n polls this)

Usage:
  python -m uvicorn agent.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import config as cfg
from .queue import (
    init_db, create_job, dispatch_job, get_job, list_jobs, get_pending_jobs,
)

app = FastAPI(
    title="Video Clipper Agent (Split Architecture)",
    description="Render API server with job queue for Miget worker distribution.",
    version="0.2.0",
)

app.state.workspace = Path(cfg.OUTPUT_DIR)
init_db()

@app.get("/health")
def health():
    """Check service health."""
    return {"status": "ok", "worker": "video-clipper-agent", "version": "0.2.0-split"}

@app.get("/api/queue")
def get_queue(limit: int = Query(10, ge=1, le=50)):
    """Get pending jobs for Miget workers to claim."""
    pending = get_pending_jobs(limit=limit)
    return {"jobs": pending, "count": len(pending)}

@app.post("/api/dispatch")
def dispatch_queue(job_id: str, agent_url: str):
    """Claim a job for processing."""
    job = dispatch_job(job_id, agent_url)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or not pending")
    return {"job": job, "message": "Job dispatched"}

@app.get("/api/status/{job_id}")
def get_status(job_id: str):
    """Get job status for n8n polling."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "status": job["status"], "stage": job.get("current_stage"),
            "progress": job.get("progress", 0), "result": job.get("result")}

@app.get("/api/jobs")
def list_all_jobs(status: Optional[str] = None, limit: int = 50):
    """List jobs with optional status filter."""
    jobs = list_jobs(status=status, limit=limit)
    return {"jobs": jobs, "count": len(jobs)}

@app.post("/api/job")
def create_new_job(body: Dict[str, Any]):
    """Create a new job from n8n."""
    required = ["campaign_id", "campaign_name", "youtube_url"]
    for field in required:
        if field not in body:
            raise HTTPException(status_code=400, detail=f"Missing required field: {field}")
    job = create_job(
        campaign_id=body["campaign_id"],
        campaign_name=body["campaign_name"],
        youtube_url=body["youtube_url"],
        aspect=body.get("aspect", "9:16"),
        num_clips=body.get("num_clips", 3),
        agent_url=body.get("agent_url"),
    )
    return job

# Include the original app routes if needed
# from .app import app as original_app
