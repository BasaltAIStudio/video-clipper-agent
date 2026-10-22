"""agent/poller.py — Poll Content Rewards scraper for views/earnings and post analytics.

Flow:
    Every N minutes (configurable, default 15min):
      1. Check idempotency cache for pending clip IDs
      2. Scrape Content Rewards for views/earnings on submitted clips
      3. POST analytics to Content Rewards webhook
      4. Mark clips as tracked
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from . import config
from .campaign_cache import campaign_cache
from .pipeline import IdempotencyCache

log = logging.getLogger("agent.poller")


class ContentRewardsPoller:
    """Polls Content Rewards for views/earnings on submitted clips."""

    def __init__(self, interval_minutes: int = 15, cache_dir: Path = None):
        self.interval_minutes = interval_minutes
        self.cache = IdempotencyCache(cache_dir or config.AGENT_CACHE_DIR)
        self._last_run = 0

    def should_run(self) -> bool:
        """Check if polling interval has elapsed."""
        now = time.time()
        elapsed = now - self._last_run
        if elapsed >= self.interval_minutes * 60:
            self._last_run = now
            return True
        return False

    def get_submitted_clips(self) -> list[dict]:
        """Get all submitted clip records from cache."""
        return self.cache.get_all()

    def scrape_views_earnings(
        self,
        campaign_id: str,
        video_url: str,
    ) -> dict:
        """Scrape Content Rewards for views/earnings.

        Uses the existing scraper (agent/contentrewards_scraper.py).
        Returns: {views, earnings_usd, watch_time_seconds, comments, shares, likes}
        """
        # Import inside function to avoid circular imports
        from .contentrewards_scraper import ContentRewardsScraper

        scraper = ContentRewardsScraper()
        try:
            # Try to find the campaign
            results = scraper.scrape_discover_page(tab="All")
            # Look for the campaign by ID or URL
            campaign = None
            for camp in results:
                if camp.get("campaign_id") == campaign_id or campaign_id in camp.get("url", ""):
                    campaign = camp
                    break

            if campaign is None:
                return {
                    "views": 0,
                    "earnings_usd": 0.0,
                    "watch_time_seconds": 0,
                    "comments": 0,
                    "shares": 0,
                    "likes": 0,
                    "scraped": False,
                    "note": "Campaign not found via discover page",
                }

            # Extract analytics from campaign data
            # This is placeholder - actual implementation depends on scraper output structure
            return {
                "views": 0,
                "earnings_usd": 0.0,
                "watch_time_seconds": 0,
                "comments": 0,
                "shares": 0,
                "likes": 0,
                "scraped": True,
                "campaign": campaign.get("name", campaign_id),
            }
        except Exception as e:
            log.error(f"Scraper error for {campaign_id}: {e}")
            return {
                "views": 0,
                "earnings_usd": 0.0,
                "watch_time_seconds": 0,
                "comments": 0,
                "shares": 0,
                "likes": 0,
                "scraped": False,
                "error": str(e),
            }

    def post_analytics(
        self,
        campaign_id: str,
        clip_id: str,
        analytics: dict,
        youtube_url: str = None,
    ) -> bool:
        """POST analytics to Content Rewards webhook."""
        log.info(f"POSTing analytics to Content Rewards: campaign={campaign_id}, clip={clip_id}")

        payload = {
            "campaign_id": campaign_id,
            "clip_id": clip_id,
            "youtube_url": youtube_url,
            "views": analytics.get("views", 0),
            "earnings_usd": analytics.get("earnings_usd", 0.0),
            "watch_time_seconds": analytics.get("watch_time_seconds", 0),
            "comments": analytics.get("comments", 0),
            "shares": analytics.get("shares", 0),
            "likes": analytics.get("likes", 0),
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "status": "tracked",
        }

        # POST to Content Rewards webhook
        # NOTE: Content Rewards has no API, so this is a webhook endpoint
        # The agent must configure the webhook URL via .env
        webhook_url = config.CAMPAIGN_WEBHOOK_URL
        if not webhook_url:
            log.warning("CAMPAIGN_WEBHOOK_URL not set. Skipping analytics POST.")
            return False

        try:
            import requests
            response = requests.post(
                webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            response.raise_for_status()
            log.info(f"Analytics submitted successfully: {response.json()}")
            return True
        except Exception as e:
            log.error(f"Analytics POST failed: {e}")
            return False

    def poll(self) -> int:
        """Run a single poll cycle. Returns number of clips processed."""
        log.info("Poller cycle starting...")
        clips = self.get_submitted_clips()
        processed = 0

        for clip in clips:
            clip_id = clip["clip_id"]
            campaign_id = clip["campaign_id"]
            campaign_name = clip["campaign_name"]
            video_url = clip["video_url"]
            youtube_url = clip.get("youtube_url")

            if not video_url:
                continue

            log.info(f"Polling: campaign={campaign_id}, video={video_url[:50]}...")

            # Scrape views/earnings
            analytics = self.scrape_views_earnings(campaign_id, video_url)

            if analytics.get("scraped"):
                # POST analytics
                if self.post_analytics(campaign_id, clip_id, analytics, youtube_url):
                    processed += 1
                    log.info(f"Analytics tracked: {analytics.get('views')} views, "
                             f"${analytics.get('earnings_usd', 0):.2f} earnings")
                else:
                    log.warning(f"Failed to submit analytics for {clip_id}")
            else:
                log.info(f"No data scraped for {clip_id}: {analytics.get('note', 'unknown')}")

        log.info(f"Poller cycle complete: {processed} clips tracked")
        return processed


def poll_once(interval_minutes: int = 15, once: bool = False):
    """Run the poller. If once=True, run one cycle and exit. Otherwise loop."""
    poller = ContentRewardsPoller(interval_minutes=interval_minutes)

    if once:
        poller.poll()
        return

    # Continuous polling
    log.info(f"Poller started: interval={interval_minutes}min")
    try:
        while True:
            if poller.should_run():
                poller.poll()
            time.sleep(60)  # Check every minute
    except KeyboardInterrupt:
        log.info("Poller stopped.")


def main():
    """CLI entry point for the poller."""
    import argparse

    parser = argparse.ArgumentParser(description="Content Rewards Analytics Poller")
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=15,
        help="Polling interval in minutes (default: 15)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle and exit (for testing)",
    )
    args = parser.parse_args()

    poll_once(interval_minutes=args.interval_minutes, once=args.once)


if __name__ == "__main__":
    main()
