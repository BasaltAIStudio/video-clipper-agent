"""run.py — CLI entry point for the Video Clipper Agent.

Usage:
    python run.py                     # starts the FastAPI server
    python run.py --once "URL" --num-clips 3   # one-shot clip (no server)
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path

from . import config as cfg
from .app import app
from .pipeline import run_decision_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("agent.run")


def one_shot(url: str, *, num_clips: int, aspect: str, use_llm: bool = True, use_jev: bool = False) -> dict:
    cfg.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        result = run_decision_engine(
            url=url,
            n8n_url="https://your-n8n-webhook.example.com",
            campaign_id=str(uuid.uuid4()),
            campaign_name="unknown",
            num_clips=num_clips,
            use_llm=use_llm,
            use_jev=use_jev,
        )
    except Exception as e:  # noqa: BLE001
        log.error("CLIP FAILED: %s", e)
        sys.exit(1)
    manifest = result.get("manifest", {})
    print("\n" + "=" * 72)
    print("Source: %s" % url)
    print("Duration: %.0fs" % manifest.get("transcript_duration", 0.0))
    print("Highlights: %d generated" % len(result.get("candidates", [])))
    print("=" * 72)
    for c in manifest.get("clips", []):
        print("  #%d  [%.0fs -> %.0fs] score=%s" % (c.get("index", 0), c["start"], c["end"], c.get("score", 0)))
        print("      %s | hook: %s" % (c.get("file_name", ""), c.get("text", "")[:90]))
    print("=" * 72)
    print("Artifacts in %s:" % cfg.OUTPUT_DIR)
    for p in sorted(cfg.OUTPUT_DIR.iterdir()):
        print("  ", p.name, p.stat().st_size, "bytes")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(prog="run.py", description="Video Clipper Agent")
    ap.add_argument("url", nargs="?", help="YouTube URL or local video path")
    ap.add_argument("--num-clips", type=int, default=cfg.DEFAULT_NUM_CLIPS)
    ap.add_argument("--aspect", default=cfg.DEFAULT_ASPECT)
    ap.add_argument("--once", action="store_true", help="one-shot mode (no server)")
    ap.add_argument("--no-llm", action="store_true", help="disable LLM; use Chopify-style heuristic for highlight scoring")
    ap.add_argument("--schedule", action="store_true", help="run continuously; execute one-shot pipeline on each interval (auto mode for overnight)")
    ap.add_argument("--interval-seconds", type=int, default=86400, help="schedule interval in seconds (default 86400 = 24h)")
    ap.add_argument("--stats", action="store_true", help="print pipeline statistics after one-shot run")
    ap.add_argument("--use-jev", action="store_true", help="use JEV fast decision engine for highlight selection (replaces simple top-N heuristic ranking)")
    ap.add_argument("--host", default=cfg.HOST)
    ap.add_argument("--port", type=int, default=cfg.PORT)
    args = ap.parse_args()

    if args.schedule:
        import time
        import signal
        log.info("Scheduler mode: interval=%.1fs (%.0f min, %.2f hours)", args.interval_seconds, args.interval_seconds / 60, args.interval_seconds / 3600)
        run_count = 0
        def signal_handler(sig, frame):
            log.info("Scheduler stopped by signal.")
            sys.exit(0)
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        while True:
            run_count += 1
            log.info("========================================")
            log.info("Run #%d", run_count)
            log.info("========================================")
            try:
                one_shot(args.url, num_clips=args.num_clips, aspect=args.aspect, use_llm=not args.no_llm, use_jev=args.use_jev)
                log.info("Run #%d completed successfully", run_count)
            except Exception as e:
                log.error("Run #%d failed: %s", run_count, e)
            log.info("Scheduler sleeping %.1fs...", args.interval_seconds)
            time.sleep(args.interval_seconds)
        return 0

    if args.once:
        if not args.url:
            ap.error("either provide a URL or use --once with a URL")
        result = one_shot(args.url, num_clips=args.num_clips, aspect=args.aspect, use_llm=not args.no_llm, use_jev=args.use_jev)
        if args.stats and result:
            s = result.get("stats", {})
            if hasattr(s, "get_summary"):
                s = s.get_summary()
            print("\n" + "=" * 72)
            print("PIPELINE STATISTICS")
            print("=" * 72)
            print("Total runs:        %s" % s.get("total_runs", "N/A"))
            print("Successful runs:   %s" % s.get("successful_runs", "N/A"))
            print("Failed runs:       %s" % s.get("failed_runs", "N/A"))
            print("Success rate:      %s%%" % s.get("success_rate", "N/A"))
            print("Avg duration:      %ss" % s.get("avg_duration", "N/A"))
            print("Last run:          %ss" % s.get("last_run_duration", "N/A"))
            print("Last campaign:     %s" % s.get("last_campaign_id", "N/A"))
            print("Last LLM provider: %s" % s.get("last_llm_provider", "N/A"))
            print("=" * 72 + "\n")
        return 0

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
