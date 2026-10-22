# Video Clipper Agent

A self-contained **FastAPI video clipper service** deployed on Snap Deploy.
Given a YouTube link (or local video), it cuts the most viral moments and
writes captioned vertical (9:16) shorts, then hands the manifest back to n8n
for campaign submission.

## What it does (borrowed from the reference repos)

| Stage | Source repo | Technique |
|---|---|---|
| Transcription | `Anil-matcha/AI-Youtube-Shorts-Generator` (local mode) | `yt-dlp` + `faster-whisper` (local, offline) |
| Highlight detection | `Anil-matcha/AI-Youtube-Shorts-Generator` | virality-criteria LLM prompt, fallback chain |
| "No mid-sentence cuts" | `mehbul/chopify` | sentence-boundary enforcement on word timestamps |
| Vertical 9:16 render + burned-in captions | `artbyjazi/autoclip`, `NaufalRizqullah/opensource-clipping` | scale-to-fill + ASS subtitle burn-in |
| Audio normalize | `mehbul/chopify` | `loudnorm` (-14 LUFS) |
| Virality scoring + dedup | `Anil-matcha/AI-Youtube-Shorts-Generator`, `xixihhhh/hotclip` | score-based dedupe, hook-boost |
| Multi-LLM fallback | `multi-provider-agent-pipeline` skill | OpenRouter -> NVIDIA NIM (Nous removed) |
| Agent HTTP surface | `fralapo/clippyme` | FastAPI, idempotent `/api/clip`, `/health` |
| Campaign submission | n8n HTTP Request node | POST manifest to `CAMPAIGN_WEBHOOK_URL` |

## Quick start (local)

```bash
cp .env.example .env        # fill in ADMIN/SNAP deploy secrets + LLM keys
pip install -r requirements.txt
python run.py --once "https://www.youtube.com/watch?v=..." --num-clips 3
```

## n8n integration

Point n8n's **HTTP Request** node at `http://<agent-host>:8000/api/clip`:

```json
{
  "method": "POST",
  "url": "http://clipper:8000/api/clip",
  "body": {
    "youtube_url": "{{ $json.youtube_url }}",
    "campaign_id": "{{ $json.campaign_id }}",
    "campaign_name": "{{ $json.campaign_name }}",
    "num_clips": 3,
    "submit": true
  },
  "sendBody": true
}
```

The agent returns a manifest (clip paths, timestamps, hooks, scores). Set
`submit: true` and `CAMPAIGN_WEBHOOK_URL` to fire the manifest back to n8n's
campaign stage. See `n8n_clip_pipeline_entry.json`.

## Snap Deploy deployment

Snap Deploy builds a Docker image from this repo. The `Dockerfile` is the whole
deploy surface:

```bash
docker build -t video-clipper .
docker run --rm -p 8000:8000 --name clipper video-clipper
```

On Snap Deploy, inject the LLM keys as **secrets/env vars** (see `.env.example`);
the image runs `CMD ["python", "run.py"]` with `AGENT_HOST=0.0.0.0`.
Each clip job is one `POST /api/clip`; the container health-checks on
`/health`.

## Health & one-shot endpoints

- `GET /health` - worker status + which LLM providers are configured
- `GET /clips` - list artifacts in `AGENT_OUTPUT_DIR`
- `POST /api/clip` - run the pipeline on a YouTube URL; returns a JSON manifest

## LLM provider note

Two free providers are wired through a pluggable `llm_fn` (OpenRouter -> NVIDIA NIM).
Set `OPENROUTER_API_KEY` and `NVIDIA_NIM_API_KEY` in `.env`. If one key rate-limits or
errors, the chain retries on the next. You can also run with `LLM_PROVIDER=none` (or
`--no-llm`) to use Chopify-style keyword/density heuristics for zero cost and zero
rate-limit risk.
