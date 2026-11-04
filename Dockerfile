# Video Clipper Agent — Split Architecture (Render API + Miget Worker)
#
# Render: Runs agent.server:app (API + queue management)
# Miget: Runs agent.worker (compute worker)
#
# Free tier considerations:
# - No Playwright (uses Scrapling HTTP Fetcher only)
# - Uses tiny.wav2vec model by default (fast!)
# - Minimal dependencies

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install Python deps first (layer cache).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application package
COPY agent /app/agent

# Make worker entrypoint executable
RUN chmod +x /app/agent/worker.py

# Health check - shorter timeout for fast deploy
HEALTHCHECK --interval=30s --timeout=15s --start-period=30s --retries=2 \
    CMD curl -fsS http://localhost:${PORT:-8000}/health >/dev/null || exit 1

# Render uses: python -m uvicorn agent.server:app
# Miget uses: python -m uvicorn agent.worker:app (default)
CMD ["python", "-m", "uvicorn", "agent.server:app", "--host", "0.0.0.0", "--port", "${PORT:-8000}"]
