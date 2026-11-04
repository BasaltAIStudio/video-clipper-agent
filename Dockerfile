# Video Clipper Agent — Split Architecture (Render API + Miget Worker)
#
# Render: Runs agent.server:app (API + queue management)
# Miget: Runs agent.worker (compute worker)
#
# Environment variables (set via Render/Miget service config):
#   RENDER_URL - Base URL of Render API (only needed by Miget worker)
#   JOB_DB_PATH - Path to shared SQLite DB
#   AGENT_WORK_DIR - Working directory for jobs
#   AGENT_CACHE_DIR - Cache directory for downloads/transcripts
#   LOCAL_WHISPER_MODEL - Whisper model size (tiny/en recommended for free tier)
#   LOCAL_WHISPER_COMPUTE_TYPE - CPU speed optimization (default: int8)
#   LOCAL_WHISPER_DEVICE - CPU/GPU (default: cpu for free tier)

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AUDIO_CACHE=/app/.cache/audio \
    HF_HOME=/app/.cache/hf \
    TRANSFORMERS_CACHE=/app/.cache/hf \
    DATABASES_CACHE_DIR=/app/.cache/db

# Runtime deps: ffmpeg (video/audio processing) + curl (healthcheck).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

WORKDIR /app

# Install Python deps first (layer cache).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application package
COPY agent /app/agent

# Make worker entrypoint executable
RUN chmod +x /app/agent/worker.py

# Writable volumes (persisted across restarts)
RUN mkdir -p /app/output /app/work /app/cache && \
    chown -R nobody:nogroup /app /tmp 2>/dev/null || true

USER nobody

ENV AGENT_DATA_DIR=/tmp/video_agent_data \
    AGENT_HOST=0.0.0.0 \
    AGENT_PORT=8000

# Health check with extended start-period for model loading
HEALTHCHECK --interval=30s --timeout=30s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:${PORT:-8000}/health >/dev/null || exit 1

# Render uses: python -m uvicorn agent.server:app
# Miget uses: python -m uvicorn agent.worker:app (default)
CMD ["python", "-m", "uvicorn", "agent.server:app", "--host", "0.0.0.0", "--port", "${PORT:-8000}"]
