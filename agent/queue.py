"""agent/queue.py — Job queue for split Render/Miget architecture.

Render is the API server; Miget worker pulls jobs from the queue:
  - Render creates jobs in SQLite and exposes /api/queue
  - Miget worker claims jobs via /api/dispatch
  - Worker processes and updates status in the same DB
  - n8n polls /api/status for campaign results
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

log = logging.getLogger("agent.queue")

JOB_DB_PATH = Path("/tmp/video_agent_data/jobs.db")


def get_job_db() -> Path:
    """Resolve job database path (env override for Miget worker)."""
    return Path(os.getenv("JOB_DB_PATH", str(JOB_DB_PATH)))


def init_db() -> None:
    """Create jobs table if missing."""
    db_path = get_job_db()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                campaign_id TEXT,
                campaign_name TEXT,
                youtube_url TEXT,
                aspect TEXT,
                num_clips INTEGER,
                status TEXT NOT NULL,
                current_stage TEXT,
                progress INTEGER,
                result TEXT,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_jobs_status
            ON jobs (status, created_at)
            """
        )
        conn.commit()


def get_conn() -> sqlite3.Connection:
    """Get a database connection."""
    db_path = get_job_db()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def create_job(
    campaign_id: str,
    campaign_name: str,
    youtube_url: str,
    aspect: str = "9:16",
    num_clips: int = 3,
    agent_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new job in the queue."""
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with closing(get_conn()) as conn:
        conn.execute(
            """
            INSERT INTO jobs
                (job_id, campaign_id, campaign_name, youtube_url, aspect,
                 num_clips, status, current_stage, progress, result, error,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, 0, NULL, NULL, ?, ?)
            """,
            (job_id, campaign_id, campaign_name, youtube_url, aspect,
             num_clips, now, now),
        )
        conn.commit()
    log.info(f"Created job {job_id} for campaign {campaign_id}")
    return {
        "job_id": job_id,
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "youtube_url": youtube_url,
        "aspect": aspect,
        "num_clips": num_clips,
        "status": "pending",
        "created_at": now,
    }


def dispatch_job(job_id: str, agent_url: str) -> Dict[str, Any]:
    """Dispatch (claim) a job for processing."""
    with closing(get_conn()) as conn:
        conn.execute(
            "UPDATE jobs SET status = 'dispatched', current_stage = 'download', "
            "progress = 0, updated_at = ? WHERE job_id = ? AND status = 'pending'",
            (datetime.now(timezone.utc).isoformat(), job_id),
        )
        conn.commit()
        conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        row = conn.fetchone()
    if row:
        job = dict(row)
        job["agent_url"] = agent_url
        log.info(f"Dispatched job {job_id} to agent {agent_url}")
        return job
    return {}


def update_job(job_id: str, status: str, stage: Optional[str] = None,
               progress: int = 0, result: Optional[Dict] = None,
               error: Optional[str] = None) -> Optional[Dict]:
    """Update job status and return the full job row."""
    now = datetime.now(timezone.utc).isoformat()
    with closing(get_conn()) as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, current_stage = ?, progress = ?, "
            "result = ?, error = ?, updated_at = ? WHERE job_id = ?",
            (status, stage, progress, json.dumps(result) if result else None,
             error, now, job_id),
        )
        conn.commit()
        conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        row = conn.fetchone()
    if row:
        job = dict(row)
        job.pop("result", None)
        job.pop("error", None)
        return job
    return None


def get_job(job_id: str) -> Optional[Dict]:
    """Get a single job by ID."""
    with closing(get_conn()) as conn:
        conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        row = conn.fetchone()
    if row:
        job = dict(row)
        job.pop("result", None)
        job.pop("error", None)
        return job
    return None


def get_pending_jobs(limit: int = 10) -> List[Dict]:
    """Get pending jobs for workers to claim."""
    with closing(get_conn()) as conn:
        conn.execute(
            "SELECT job_id, campaign_id, campaign_name, youtube_url, aspect, "
            "num_clips, status, current_stage, progress, created_at, updated_at "
            "FROM jobs WHERE status = 'pending' ORDER BY created_at LIMIT ?",
            (limit,),
        )
        rows = conn.fetchall()
    jobs = []
    for row in rows:
        job = dict(row)
        job.pop("result", None)
        job.pop("error", None)
        jobs.append(job)
    return jobs


def list_jobs(status: Optional[str] = None, limit: int = 50) -> List[Dict]:
    """List jobs with optional status filter."""
    with closing(get_conn()) as conn:
        if status:
            conn.execute(
                "SELECT job_id, campaign_id, campaign_name, youtube_url, status, "
                "current_stage, progress, created_at, updated_at FROM jobs "
                "WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            conn.execute(
                "SELECT job_id, campaign_id, campaign_name, youtube_url, status, "
                "current_stage, progress, created_at, updated_at FROM jobs "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        rows = conn.fetchall()
    jobs = []
    for row in rows:
        job = dict(row)
        job.pop("result", None)
        job.pop("error", None)
        jobs.append(job)
    return jobs
