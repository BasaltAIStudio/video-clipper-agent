# Video Clipper Agent — Snap Deploy / Docker image.
#
# Build:
#   docker build -t video-clipper .
# Run:
#   docker run --rm -p 8000:8000 --name clipper video-clipper
#
# Snap Deploy (the user's Docker service) builds this image from the repo and
# deploys it as a container; each clip job is a single HTTP call to /api/clip.

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

# Copy the application.
COPY agent /app/agent
COPY run.py /app/run.py

# Writable volumes (persisted across restarts; bind-mountable on Snap Deploy).
RUN mkdir -p /app/output /app/work /app/uploads /app/.cache/hf && \
    chown -R nobody:nogroup /app/output /app/work /app/uploads 2>/dev/null || true

USER nobody

ENV AGENT_HOST=0.0.0.0 \
    AGENT_PORT=8000 \
    DEFAULT_ASPECT=9:16 \
    DEFAULT_NUM_CLIPS=3 \
    MAX_NUM_CLIPS=10

# LLM keys are injected by Snap Deploy secrets / the host environment; see
# .env.example. None of these are baked into this image.

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/health >/dev/null || exit 1

# The agent package uses relative imports, so run as `python -m agent.run`
CMD ["python", "-m", "agent.run"]
