"""agent: environment configuration.

Credentials are read from environment variables only (never from code, never
inlined on the command line): https://open.hermes-agent.nousresearch.com/docs
flags/history/transcripts/s blurred. See
command-execution-hygiene skill for the standing rule.
"""

from __future__ import annotations

import os
from pathlib import Path

# Render injects a `PORT` env var for web services; honour it so the app
# listens on Render's public port rather than the Dockerfile default 8000.
PORT = int(os.getenv("PORT") or os.getenv("AGENT_PORT", "8000"))
HOST = os.getenv("HOST") or os.getenv("AGENT_HOST", "0.0.0.0")
WORK_DIR = Path(os.getenv("AGENT_WORK_DIR", "/app/work")).resolve()
OUTPUT_DIR = Path(os.getenv("AGENT_OUTPUT_DIR", "/app/output")).resolve()
UPLOAD_DIR = Path(os.getenv("AGENT_UPLOAD_DIR", "/app/uploads")).resolve()
MAX_WORK_DIR_BYTES = int(os.getenv("AGENT_MAX_WORK_DIR_BYTES", str(4 * 1024 * 1024 * 1024)))

# Writable scratch/state dir. The container user (nobody) has no home dir, so
# never use Path.home().
DATA_DIR = Path(os.getenv("AGENT_DATA_DIR", "/tmp/video_agent_data"))

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.0-flash-001")


NVIDIA_NIM_API_KEY = os.getenv("NVIDIA_NIM_API_KEY", "").strip()
NVIDIA_NIM_BASE_URL = os.getenv("NVIDIA_NIM_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
NVIDIA_NIM_MODEL = os.getenv("NVIDIA_NIM_MODEL", "google/gemini-2.0-flash-001")

PROVIDER_CONFIG = {
    "openrouter": {
        "api_key": OPENROUTER_API_KEY,
        "base_url": OPENROUTER_BASE_URL,
        "model": OPENROUTER_MODEL,
        "priority": 0,
    },
    "nvidia_nim": {
        "api_key": NVIDIA_NIM_API_KEY,
        "base_url": NVIDIA_NIM_BASE_URL,
        "model": NVIDIA_NIM_MODEL,
        "priority": 1,
    },
}

PROVIDER_ORDER = sorted(PROVIDER_CONFIG.values(), key=lambda c: c["priority"])

# LLM / heuristic toggle
#   LLM_PROVIDER = openrouter | nvidia_nim | none
#   "none" => heuristic-only scoring (Chopify-style keyword/density), zero LLM cost
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openrouter").strip().lower()

# Heuristic / LLM toggle: True when an LLM provider is selected, False when
# LLM_PROVIDER is "none" (heuristic mode).
USE_LLM = LLM_PROVIDER != "none"

# Transcription: tiny = fastest (default for campaign clipping), small = better
# accuracy but slower. Campaign-linked clips should be fast, so tiny is the
# production default. Override locally with: LOCAL_WHISPER_MODEL=base
LOCAL_WHISPER_MODEL = os.getenv("LOCAL_WHISPER_MODEL", "tiny")
LOCAL_WHISPER_DEVICE = os.getenv("LOCAL_WHISPER_DEVICE", "auto")
LOCAL_WHISPER_COMPUTE_TYPE = os.getenv("LOCAL_WHISPER_COMPUTE_TYPE", "auto")

# word_timestamps=False (faster, segment-level only) is the production default.
# word_timestamps=True is available for research/LLM-prompt scenarios that need
# per-word timing, but it adds 3-5+ minutes on a 36-min video.
LOCAL_WHISPER_WORD_TIMESTAMPS = os.getenv("LOCAL_WHISPER_WORD_TIMESTAMPS", "false").lower() == "true"

# Video processing
FFMPEG_BINARY = os.getenv("FFMPEG_BINARY", "ffmpeg")
FFPROBE_BINARY = os.getenv("FFPROBE_BINARY", "ffprobe")
DEFAULT_ASPECT = os.getenv("DEFAULT_ASPECT", "9:16")  # 9:16 vertical short
TARGET_WIDTH = int(os.getenv("TARGET_WIDTH", "1080"))

# Idempotency cache directory for submitted clip tracking
AGENT_CACHE_DIR = Path(os.getenv("AGENT_CACHE_DIR", "/app/cache")).resolve()

# Limits
DEFAULT_NUM_CLIPS = int(os.getenv("DEFAULT_NUM_CLIPS", "3"))
MAX_NUM_CLIPS = int(os.getenv("MAX_NUM_CLIPS", "10"))
MIN_CLIP_DURATION = float(os.getenv("MIN_CLIP_DURATION", "15.0"))
MAX_CLIP_DURATION = float(os.getenv("MAX_CLIP_DURATION", "180.0"))
MAX_VIDEO_SECONDS = int(os.getenv("AGENT_MAX_VIDEO_SECONDS", "3600"))

# Campaign / n8n integration
CAMPAIGN_WEBHOOK_URL = os.getenv("CAMPAIGN_WEBHOOK_URL", "https://contentrewards.com/webhook/analytics")

# Content Rewards credentials (loaded from env vars, never from local files)
CR_CONTENT_REWARDS_USERNAME = os.getenv("CR_CONTENT_REWARDS_USERNAME", "")
CR_CONTENT_REWARDS_PASSWORD = os.getenv("CR_CONTENT_REWARDS_PASSWORD", "")

# YouTube API via OAuth (sign-in in n8n) or API key
# If set, used as fallback when n8n YouTube node doesn't have credentials
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()

CAMPAIGN_SUBMIT_TIMEOUT = float(os.getenv("CAMPAIGN_SUBMIT_TIMEOUT", "30.0"))


def logger_config(name: str = "agent"):
    """Return a small, dependency-free logger for the service.

    Kept stdlib-only so the container has zero extra runtime deps.
    """
    import logging

    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
