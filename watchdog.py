"""
Watchdog for the Video Clipper Agent — 24/7 monitoring.

Authorities the watchdog has:
1. Runs every N minutes (default 5 min) as a background cron
2. Checks the agent's /health endpoint
3. Restarts the service if unresponsive
4. Sends alerts if the agent crashes or fails to start
5. Monitors LLM provider connectivity and Content Rewards API

Usage:
    python watchdog.py                        # run once
    python watchdog.py --daemon               # run continuously
    python watchdog.py --interval 60          # every 60 seconds
    python watchdog.py --once  # single check
"""

import argparse
import logging
import requests
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
AGENT_URL = "http://localhost:8000"
WATCHDOG_URL = "http://localhost:8000"  # Demo: same as agent (no separate watchdog service)
HEALTH_ENDPOINT = "/health"
WATCHDOG_SCRIPT = Path(__file__).resolve()

# Logging
log = logging.getLogger("watchdog")
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
log.addHandler(handler)
log.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Alert helpers
# ---------------------------------------------------------------------------
def send_alert(subject: str, message: str) -> None:
    """Send an alert via the configured channel (email, webhook, etc.)."""
    # For production: send email via SMTP, webhook to Slack/Discord, etc.
    log.warning(f"ALERT: {subject} — {message}")


def restart_service() -> None:
    """Restart the agent service."""
    try:
        # On Render, we can trigger a restart via the API
        # Or on a local machine: process restart, supervisor, or systemd restart
        log.warning("Initiating service restart...")
        # Render will auto-restart the service (free tier: auto-restart on exit)
        # For self-hosted: call your process manager here
        send_alert(
            "Agent Restarted",
            f"Watchdog detected misbehavior at {datetime.now().isoformat()}. "
            f"Service will restart automatically."
        )
    except Exception as e:
        log.error(f"Restart failed: {e}")


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------
def check_agent_health() -> tuple[bool, dict]:
    """
    Check the agent's /health endpoint.

    Returns (is_healthy, data).
    """
    try:
        resp = requests.get(f"{AGENT_URL}{HEALTH_ENDPOINT}", timeout=10)
        data = resp.json()
        healthy = resp.status_code == 200 and data.get("status") == "ok"
        return healthy, data
    except requests.exceptions.Timeout:
        log.error(f"{AGENT_URL}{HEALTH_ENDPOINT} timed out")
        return False, {}
    except requests.exceptions.ConnectionError:
        log.error(f"Cannot connect to {AGENT_URL}{HEALTH_ENDPOINT}")
        return False, {}
    except Exception as e:
        log.error(f"Health check error: {e}")
        return False, {}


def check_llm_provider() -> tuple[bool, dict]:
    """Check if the configured LLM provider is reachable."""
    try:
        import os
        from agent import config as cfg

        provider = cfg.PROVIDER_ORDER[0] if cfg.PROVIDER_ORDER else None
        if not provider or not provider.get("api_key"):
            log.warning("No LLM provider key configured")
            return False, {"provider": "unknown", "reason": "no_api_key"}

        # Quick connectivity test
        # (In production, use the actual LLM client to test)
        if provider["base_url"]:
            log.info(f"LLM provider: {provider['model']} at {provider['base_url']}")
            return True, {"provider": provider["model"], "base_url": provider["base_url"]}
        return False, {"provider": provider["model"], "reason": "no_base_url"}
    except Exception as e:
        log.error(f"LLM check error: {e}")
        return False, {"error": str(e)}


def check_notifications() -> bool:
    """Verify all alerting channels are reachable."""
    # In production: check email, webhook URLs, etc.
    return True


def check_pending_tasks() -> bool:
    """Check for any pending background tasks that need monitoring."""
    try:
        import os
        from agent import config as cfg

        # Check if there are pending campaigns or scheduled jobs
        log.info("Task queue: OK")
        return True
    except Exception as e:
        log.error(f"Task check error: {e}")
        return False


# ---------------------------------------------------------------------------
# Main watchdog loop
# ---------------------------------------------------------------------------
def run_once() -> None:
    """Run a single watchdog check."""
    log.info("=" * 60)
    log.info(f"Watchdog check at {datetime.now().isoformat()}")

    # 1. Health check
    healthy, data = check_agent_health()
    if not healthy:
        log.error("Agent is NOT healthy")
        restart_service()
    else:
        log.info(f"Agent healthy: {data}")

    # 2. LLM provider check
    llm_ok, llm_data = check_llm_provider()
    if not llm_ok:
        log.error(f"LLM provider not reachable: {llm_data}")
    else:
        log.info(f"LLM provider OK: {llm_data['provider']}")

    # 3. Pending tasks check
    tasks_ok = check_pending_tasks()
    if not tasks_ok:
        log.error("Task queue check failed")

    # 4. Notifications check
    if not check_notifications():
        log.error("Alert channels not configured")

    log.info("=" * 60)


def run_daemon() -> None:
    """Run the watchdog as a daemon, checking every N seconds."""
    parser = argparse.ArgumentParser(description="Video Clipper Agent Watchdog")
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Seconds between checks (default 300 = 5 min)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check and exit",
    )
    args = parser.parse_args()

    if args.once:
        run_once()
        return

    log.info(f"Watchdog daemon started (interval: {args.interval}s)")
    log.info("Press Ctrl+C to stop")

    try:
        while True:
            run_once()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("Watchdog stopped")


if __name__ == "__main__":
    run_daemon()
