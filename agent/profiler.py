#!/usr/bin/env python3
"""
Pipeline Profiler — measures where time is actually spent in the clipping pipeline.

The agent's pipeline is a linear sequence:
  1. Download video
  2. Transcribe
  3. Highlight selection
  4. Render clips
  5. Post to n8n
  6. Poll Content Rewards for analytics

Without profiling, you're guessing where the bottleneck is. This module
measures each step's duration and reports the top bottleneck.

Usage:
    from agent.profiler import PipelineProfiler

    profiler = PipelineProfiler()
    profiler.start()

    # ... run your pipeline ...

    profiler.stop()
    profiler.report()
"""
import cProfile
import json
import logging
import pstats
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DATA_DIR

log = logging.getLogger("agent.profiler")


class PipelineProfiler:
    """
    Measures pipeline performance and identifies real bottlenecks.

    Records timing data for each pipeline step and provides detailed
    reporting on where time is actually spent.
    """

    def __init__(self, profile_output: Path = None, log_level: int = logging.INFO):
        self.profile_output = profile_output or DATA_DIR / "profile"
        self.profile_output.parent.mkdir(parents=True, exist_ok=True)
        self.log_level = log_level

        # Timing data for each step
        self.step_times: Dict[str, float] = {}
        self.step_counts: Dict[str, int] = {}
        self.step_start: Dict[str, float] = {}
        self.errors: List[Dict[str, Any]] = []

        # Overall tracking
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.total_time: float = 0.0
        self.total_clips: int = 0
        self.total_views: int = 0
        self.total_earnings: float = 0.0

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def start(self):
        """Start the profiler."""
        self.start_time = time.perf_counter()
        log.info("Pipeline profiler started")

    def stop(self):
        """Stop the profiler and record total time."""
        self.end_time = time.perf_counter()
        self.total_time = self.end_time - self.start_time
        log.info(f"Pipeline profiler stopped. Total time: {self.total_time:.2f}s")

    def record_step(self, step_name: str, duration: float, count: int = 1):
        """
        Record a measured step.

        Args:
            step_name: Name of the step (e.g., "download", "transcribe",
                       "highlight", "render", "n8n", "analytics")
            duration: Duration in seconds
            count: Number of items processed (for burn-rate calculation)
        """
        self.step_times[step_name] = self.step_times.get(step_name, 0.0) + duration
        self.step_counts[step_name] = self.step_counts.get(step_name, 0) + count
        log.log(self.log_level, f"  {step_name}: {duration:.2f}s (x{count})")

    def record_error(self, error: Exception, context: Dict[str, Any] = None):
        """Record an error that occurred during pipeline execution."""
        self.errors.append({
            "error": str(error),
            "error_type": type(error).__name__,
            "traceback": traceback.format_exc(),
            "context": context or {},
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        })

    def record_performance(self, views: int, earnings: float, watch_time: float = 0.0):
        """Record performance metrics from Content Rewards."""
        self.total_views += views
        self.total_earnings += earnings
        self.total_earnings += earnings

    # ------------------------------------------------------------------
    # Profiling
    # ------------------------------------------------------------------

    def profile(self, func, *args, **kwargs):
        """
        Wrap a function with profiling.

        Returns the result of func(*args, **kwargs) and records timing data.
        """
        step_name = kwargs.pop("step_name", func.__name__)
        count = kwargs.pop("count", 1)

        start = time.perf_counter()
        try:
            result = func(*args, **kwargs)
            duration = time.perf_counter() - start
            self.record_step(step_name, duration, count)
            return result
        except Exception as e:
            self.record_error(e, {"step": step_name, "args": args, "kwargs": kwargs})
            raise

    def dump_profile(self, filename: str = None):
        """Dump a cProfile profile of the current process."""
        import cProfile
        import pstats

        profile_file = self.profile_output / (filename or f"profile_{datetime.now().strftime('%Y%m%d_%H%M%S')}.prof")

        profiler = cProfile.Profile()
        profiler.enable()

        # This is a no-op wrapper — the caller should use self.profile()
        # to profile individual steps. This is a standalone profile dump.
        log.info(f"Profile dump: {profile_file}")
        log.info("Use self.profile(func) to profile individual steps")

        profiler.disable()
        profiler.dump_stats(str(profile_file))
        return str(profile_file)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def report(self, detailed: bool = True) -> Dict[str, Any]:
        """
        Generate a performance report.

        Returns a dict with timing breakdown and bottleneck analysis.
        """
        self.end_time = time.perf_counter()
        self.total_time = self.end_time - self.start_time

        report = {
            "total_time_seconds": self.total_time,
            "total_clips": self.total_clips,
            "total_views": self.total_views,
            "total_earnings": self.total_earnings,
            "step_times": {},
            "step_burn_rates": {},  # seconds per item
            "errors": self.errors,
            "bottleneck": None,
            "bottleneck_percent": 0.0,
        }

        if self.step_times:
            total_step_time = sum(self.step_times.values())
            for step_name, duration in sorted(self.step_times.items(), key=lambda x: x[1], reverse=True):
                count = self.step_counts.get(step_name, 0)
                burn_rate = duration / max(count, 1)
                report["step_times"][step_name] = {
                    "total_seconds": duration,
                    "count": count,
                    "burn_rate_seconds_per_item": round(burn_rate, 3),
                }

            # Determine bottleneck
            if total_step_time > 0:
                top_step = max(self.step_times.items(), key=lambda x: x[1])
                report["bottleneck"] = top_step[0]
                report["bottleneck_percent"] = round(
                    top_step[1] / total_step_time * 100, 1
                )

        log.info("=" * 60)
        log.info("PIPELINE PERFORMANCE REPORT")
        log.info("=" * 60)
        log.info(f"Total time: {self.total_time:.2f}s")
        log.info(f"Total clips: {self.total_clips}")
        log.info(f"Total views: {self.total_views}")
        log.info(f"Total earnings: ${self.total_earnings:.4f}")
        log.info(f"Total step time: {sum(self.step_times.values()):.2f}s")
        log.info("")
        log.info("Step time breakdown:")
        for step_name, data in sorted(report["step_times"].items(),
                                       key=lambda x: x[1]["total_seconds"],
                                       reverse=True):
            log.info(f"  {step_name}: {data['total_seconds']:.2f}s "
                     f"({data['count']}x, "
                     f"{data['burn_rate_seconds_per_item']:.3f}s/item)")
        log.info("")
        log.info(f"Bottleneck: {report['bottleneck']} "
                 f"({report['bottleneck_percent']:.1f}% of total step time)")

        if self.errors:
            log.info(f"\nErrors: {len(self.errors)}")
            for err in self.errors:
                log.info(f"  [{err['error_type']}] {err['error']}")

        log.info("=" * 60)

        return report

    def export_json(self, path: Path = None) -> Dict[str, Any]:
        """Export the report to JSON."""
        report = self.report()
        path = path or self.profile_output / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(json.dumps(report, indent=2, default=str))
        return report


# ---------------------------------------------------------------------------
# Fast summary printer (for quick terminal output)
# -------------------------------------------------------------------------

def profile_summary(report: Dict[str, Any]):
    """Print a quick summary of a profiling report."""
    log.info("=" * 60)
    log.info("PIPELINE PERFORMANCE SUMMARY")
    log.info("=" * 60)
    log.info(f"Total time: {report.get('total_time_seconds', 'N/A'):.2f}s")
    log.info(f"Total clips: {report.get('total_clips', 'N/A')}")
    log.info(f"Total views: {report.get('total_views', 'N/A')}")
    log.info(f"Total earnings: ${report.get('total_earnings', 0):.4f}")
    log.info("")
    log.info("Top steps by time:")
    for step_name, data in sorted(report.get("step_times", {}).items(),
                                   key=lambda x: x[1]["total_seconds"], reverse=True)[:5]:
        log.info(f"  {step_name}: {data['total_seconds']:.2f}s ({data['burn_rate_seconds_per_item']:.3f}s/item)")
    log.info("")
    log.info(f"Bottleneck: {report.get('bottleneck', 'N/A')} "
             f"({report.get('bottleneck_percent', 0):.1f}%)")
    if report.get("errors"):
        log.info(f"Errors: {len(report['errors'])}")
    log.info("=" * 60)
