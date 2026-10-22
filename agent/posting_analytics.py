"""Posting & Analytics Pipeline for Content Rewards Campaigns.

This module handles:
1. Submitting clips to Content Rewards (webhook POST JSON)
2. Posting uploaded videos to YouTube via n8n (using n8n YouTube API nodes)
3. Tracking views/earnings analytics
4. Getting the YouTube URL of posted videos to submit back to Content Rewards

Architecture:
    Agent clips video
      ↓
    Agent submits clip to Content Rewards (webhook POST JSON)
      ↓
    n8n workflow receives clip → posts to YouTube via YouTube API
      ↓
    n8n YouTube API returns video URL
      ↓
    n8n POSTs video URL back to Content Rewards (analytics submission)
      ↓
    Content Rewards tracks views/earnings
      ↓
    Agent pulls analytics → reports to Content Rewards
"""
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Content Rewards submission endpoint
# Content Rewards accepts webhooks via their "Submit Video" feature.
# The agent POSTs the clip manifest (with YouTube URL) to the campaign platform.
CONTENT_REWARDS_WEBHOOK_URL = os.getenv("CAMPAIGN_WEBHOOK_URL", "")

# n8n clip pipeline entry URL (HTTP Request node hitting agent API)
N8N_CLIP_ENTRY_URL = os.getenv("N8N_CLIP_ENTRY_URL", "http://localhost:8000/api/clip")

# YouTube API config (n8n YouTube node)
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "")

# Analytics tracking
ANALYTICS_FILE = Path(__file__).parent / "campaign_analytics.json"

# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

class ClipManifest:
    """Represents a clip ready for submission to Content Rewards."""
    def __init__(
        self,
        clip_id: str,
        campaign_id: str,
        campaign_name: str,
        video_url: str,
        video_filename: str,
        duration_seconds: float,
        highlights: List[Dict[str, Any]],
        cpm: float = 0.0,
        platform: str = "YouTube",
        post_url: Optional[str] = None,
        status: str = "pending",
    ):
        self.clip_id = clip_id
        self.campaign_id = campaign_id
        self.campaign_name = campaign_name
        self.video_url = video_url
        self.video_filename = video_filename
        self.duration_seconds = duration_seconds
        self.highlights = highlights
        self.cpm = cpm
        self.platform = platform
        self.post_url = post_url  # YouTube URL of the posted video
        self.status = status
        self.submitted_at: Optional[str] = None
        self.analytics = {}  # views, earnings, etc.

    def to_dict(self) -> Dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "campaign_id": self.campaign_id,
            "campaign_name": self.campaign_name,
            "video_url": self.video_url,
            "video_filename": self.video_filename,
            "duration_seconds": self.duration_seconds,
            "highlights": self.highlights,
            "cpm": self.cpm,
            "platform": self.platform,
            "post_url": self.post_url,
            "status": self.status,
            "submitted_at": self.submitted_at,
            "analytics": self.analytics,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ClipManifest":
        return cls(**data)

    def to_content_rewards_payload(self) -> Dict[str, Any]:
        """Format the manifest for Content Rewards webhook submission."""
        return {
            "campaign_id": self.campaign_id,
            "campaign_name": self.campaign_name,
            "video_url": self.video_url,
            "post_url": self.post_url,
            "platform": self.platform,
            "duration_seconds": self.duration_seconds,
            "cpm": self.cpm,
            "highlights": self.highlights,
            "submitted_at": self.submitted_at or datetime.utcnow().isoformat(),
            "status": self.status,
        }


class AnalyticsSubmission:
    """Analytics submission payload for Content Rewards."""
    def __init__(
        self,
        campaign_id: str,
        clip_id: str,
        views: int = 0,
        earnings_usd: float = 0.0,
        watch_time_seconds: int = 0,
        comments: int = 0,
        shares: int = 0,
        likes: int = 0,
        submitted_at: Optional[str] = None,
    ):
        self.campaign_id = campaign_id
        self.clip_id = clip_id
        self.views = views
        self.earnings_usd = earnings_usd
        self.watch_time_seconds = watch_time_seconds
        self.comments = comments
        self.shares = shares
        self.likes = likes
        self.submitted_at = submitted_at or datetime.utcnow().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "clip_id": self.clip_id,
            "views": self.views,
            "earnings_usd": self.earnings_usd,
            "watch_time_seconds": self.watch_time_seconds,
            "comments": self.comments,
            "shares": self.shares,
            "likes": self.likes,
            "submitted_at": self.submitted_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AnalyticsSubmission":
        return cls(**data)


# ---------------------------------------------------------------------------
# Content Rewards Submission
# ---------------------------------------------------------------------------

def submit_to_content_rewards(manifest: ClipManifest) -> bool:
    """
    Submit a clip manifest to Content Rewards via webhook POST JSON.

    Content Rewards accepts POST JSON webhooks. The submission includes:
    - campaign_id
    - campaign_name
    - video_url (source video)
    - post_url (YouTube link of the posted video)
    - platform
    - duration_seconds
    - cpm
    - highlights (array of highlight metadata)

    Args:
        manifest: The clip manifest to submit.

    Returns:
        True if submission succeeded, False otherwise.
    """
    if not CONTENT_REWARDS_WEBHOOK_URL:
        logger.warning("CAMPAIGN_WEBHOOK_URL not set. Skipping submission.")
        return False

    payload = manifest.to_content_rewards_payload()
    logger.info(f"Submitting clip {manifest.clip_id} to Content Rewards...")

    try:
        import requests
        response = requests.post(
            CONTENT_REWARDS_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()
        logger.info(f"Content Rewards submission successful: {result}")
        manifest.status = "submitted"
        manifest.submitted_at = datetime.utcnow().isoformat()
        return True
    except Exception as e:
        logger.error(f"Failed to submit to Content Rewards: {e}")
        return False


# ---------------------------------------------------------------------------
# YouTube Posting via n8n
# ---------------------------------------------------------------------------

def post_to_youtube_via_n8n(clip: ClipManifest) -> Optional[str]:
    """
    Post the clipped video to YouTube via n8n HTTP Request node.

    The n8n workflow uses the n8n YouTube node which:
    1. Uploads the video to YouTube
    2. Returns the video ID and URL

    The HTTP Request node in n8n hits the agent's /api/clip endpoint
    with the clip manifest and YouTube API config.

    Args:
        clip: The clip manifest with the video file.

    Returns:
        The YouTube URL of the posted video, or None if failed.
    """
    if not N8N_CLIP_ENTRY_URL:
        logger.warning("N8N_CLIP_ENTRY_URL not set. Skipping YouTube posting.")
        return None

    payload = {
        "campaign_id": clip.campaign_id,
        "campaign_name": clip.campaign_name,
        "video_url": clip.video_url,
        "video_filename": clip.video_filename,
        "duration_seconds": clip.duration_seconds,
        "highlights": clip.highlights,
        "cpm": clip.cpm,
        "platform": "YouTube",
        "action": "post_to_youtube",
        "youtube": {
            "api_key": YOUTUBE_API_KEY,
            "channel_id": YOUTUBE_CHANNEL_ID,
        },
    }

    logger.info(f"Posting clip {clip.clip_id} to YouTube via n8n...")
    try:
        import requests
        response = requests.post(
            N8N_CLIP_ENTRY_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=300,  # 5 minute timeout for YouTube upload
        )
        response.raise_for_status()
        result = response.json()

        if result.get("success"):
            video_id = result.get("video_id")
            video_url = result.get("video_url")
            logger.info(f"YouTube upload successful: {video_url}")
            clip.post_url = video_url
            clip.status = "posted_to_youtube"
            return video_url
        else:
            logger.error(f"n8n YouTube upload failed: {result}")
            return None
    except Exception as e:
        logger.error(f"Failed to post to YouTube via n8n: {e}")
        return None


# ---------------------------------------------------------------------------
# Analytics Tracking
# ---------------------------------------------------------------------------

def track_analytics(manifest: ClipManifest, data: Dict[str, Any]) -> None:
    """
    Track analytics for a submitted clip.

    Analytics data includes:
    - views
    - earnings_usd
    - watch_time_seconds
    - comments
    - shares
    - likes

    This data is submitted back to Content Rewards via webhook.

    Args:
        manifest: The clip manifest.
        data: Analytics data dictionary.
    """
    manifest.analytics.update(data)
    manifest.status = "tracked"

    # Submit analytics back to Content Rewards
    submission = AnalyticsSubmission(
        campaign_id=manifest.campaign_id,
        clip_id=manifest.clip_id,
        views=data.get("views", 0),
        earnings_usd=data.get("earnings_usd", 0.0),
        watch_time_seconds=data.get("watch_time_seconds", 0),
        comments=data.get("comments", 0),
        shares=data.get("shares", 0),
        likes=data.get("likes", 0),
    )

    logger.info(f"Tracking analytics for clip {manifest.clip_id}: {data}")

    # Submit to Content Rewards
    submit_analytics_to_content_rewards(submission)


def submit_analytics_to_content_rewards(submission: AnalyticsSubmission) -> bool:
    """
    Submit analytics to Content Rewards.

    Args:
        submission: The analytics submission.

    Returns:
        True if successful, False otherwise.
    """
    if not CONTENT_REWARDS_WEBHOOK_URL:
        logger.warning("CAMPAIGN_WEBHOOK_URL not set. Skipping analytics submission.")
        return False

    payload = {
        "campaign_id": submission.campaign_id,
        "clip_id": submission.clip_id,
        "views": submission.views,
        "earnings_usd": submission.earnings_usd,
        "watch_time_seconds": submission.watch_time_seconds,
        "comments": submission.comments,
        "shares": submission.shares,
        "likes": submission.likes,
        "submitted_at": submission.submitted_at,
    }

    logger.info(f"Submitting analytics to Content Rewards: {payload}")
    try:
        import requests
        response = requests.post(
            CONTENT_REWARDS_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()
        logger.info(f"Analytics submission successful: {result}")
        return True
    except Exception as e:
        logger.error(f"Failed to submit analytics: {e}")
        return False


# ---------------------------------------------------------------------------
# YouTube URL Extraction
# ---------------------------------------------------------------------------

def extract_youtube_url(post_url: str) -> Optional[str]:
    """
    Extract the YouTube URL from a post.

    Handles various YouTube URL formats:
    - https://www.youtube.com/watch?v=DASHBOARD_ID
    - https://youtu.be/DASHBOARD_ID
    - https://www.youtube.com/shorts/DASHBOARD_ID
    - https://www.youtube.com/embed/DASHBOARD_ID

    Args:
        post_url: The post URL or video ID.

    Returns:
        The canonical YouTube URL, or None if invalid.
    """
    if not post_url:
        return None

    # Already a full URL
    if "youtube.com" in post_url or "youtu.be" in post_url:
        return post_url

    # Just a video ID (e.g., "VLsgB6U1UxI")
    if len(post_url) == 11 and all(c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in post_url):
        return f"https://youtu.be/{post_url}"

    return None


def get_youtube_url_from_n8n_response(response_data: Dict[str, Any]) -> Optional[str]:
    """
    Extract the YouTube URL from the n8n HTTP Request node response.

    The n8n YouTube node returns the video URL in various formats.

    Args:
        response_data: The HTTP Request node response JSON.

    Returns:
        The YouTube URL, or None if not found.
    """
    # Common response formats from n8n YouTube node
    if isinstance(response_data, dict):
        # Format 1: direct URL
        if "video_url" in response_data:
            return response_data["video_url"]
        if "url" in response_data:
            return response_data["url"]

        # Format 2: embedded in a result object
        if "result" in response_data and isinstance(response_data["result"], dict):
            if "video_url" in response_data["result"]:
                return response_data["result"]["video_url"]
            if "url" in response_data["result"]:
                return response_data["result"]["url"]

        # Format 3: data array with URL
        if "data" in response_data and isinstance(response_data["data"], list):
            for item in response_data["data"]:
                if isinstance(item, dict) and "video_url" in item:
                    return item["video_url"]
                if isinstance(item, dict) and "url" in item:
                    return item["url"]

    elif isinstance(response_data, list):
        for item in response_data:
            if isinstance(item, dict) and "video_url" in item:
                return item["video_url"]
            if isinstance(item, dict) and "url" in item:
                return item["url"]

    return None


# ---------------------------------------------------------------------------
# Analytics Persistence
# ---------------------------------------------------------------------------

def load_analytics() -> Dict[str, Any]:
    """Load analytics from disk cache."""
    if ANALYTICS_FILE.exists():
        try:
            with open(ANALYTICS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load analytics: {e}")
    return {"clips": {}, "campaigns": {}, "total_views": 0, "total_earnings": 0.0}


def save_analytics(data: Dict[str, Any]) -> None:
    """Save analytics to disk cache."""
    try:
        with open(ANALYTICS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save analytics: {e}")


def update_analytics(clip: ClipManifest) -> None:
    """Update analytics cache with a clip's data."""
    data = load_analytics()

    # Store clip analytics
    data["clips"][clip.clip_id] = {
        "campaign_id": clip.campaign_id,
        "campaign_name": clip.campaign_name,
        "platform": clip.platform,
        "post_url": clip.post_url,
        "video_url": clip.video_url,
        "duration_seconds": clip.duration_seconds,
        "cpm": clip.cpm,
        "views": clip.analytics.get("views", 0),
        "earnings_usd": clip.analytics.get("earnings_usd", 0.0),
        "watch_time_seconds": clip.analytics.get("watch_time_seconds", 0),
        "comments": clip.analytics.get("comments", 0),
        "shares": clip.analytics.get("shares", 0),
        "likes": clip.analytics.get("likes", 0),
        "submitted_at": clip.submitted_at,
    }

    # Update campaign totals
    campaign_id = clip.campaign_id
    if campaign_id not in data["campaigns"]:
        data["campaigns"][campaign_id] = {
            "name": clip.campaign_name,
            "total_clips": 0,
            "total_views": 0,
            "total_earnings": 0.0,
            "last_submitted": None,
        }
    data["campaigns"][campaign_id]["total_clips"] = (
        data["campaigns"][campaign_id].get("total_clips", 0) + 1
    )
    data["campaigns"][campaign_id]["total_views"] += clip.analytics.get("views", 0)
    data["campaigns"][campaign_id]["total_earnings"] += clip.analytics.get("earnings_usd", 0.0)
    data["campaigns"][campaign_id]["last_submitted"] = clip.submitted_at

    # Global totals
    data["total_views"] += clip.analytics.get("views", 0)
    data["total_earnings"] += clip.analytics.get("earnings_usd", 0.0)

    save_analytics(data)
    logger.info(f"Analytics updated: {data['total_views']} views, ${data['total_earnings']:.2f} earnings")


# ---------------------------------------------------------------------------
# Campaign Discovery Flow Integration
# ---------------------------------------------------------------------------

def run_discovery_flow(campaign_environment: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Run the full campaign discovery flow.

    1. Crawl Content Rewards Discover page
    2. Scrape each campaign page
    3. Check eligibility
    4. Push eligible campaigns to n8n Clip Pipeline
    5. Agent clips the video
    6. Agent submits clip manifest to Content Rewards
    7. Analytics tracked and submitted back

    Args:
        campaign_environment: The campaign environment JSON.

    Returns:
        List of discovered and processed campaign summaries.
    """
    from agent.campaign_environment import campaign_environment as env

    summaries = []

    # Step 1: Scrape discover page
    logger.info("Step 1/7: Scraping Content Rewards Discover page...")
    from agent.contentrewards_scraper import ContentRewardsScraper

    scraper = ContentRewardsScraper()
    campaigns = scraper.scrape_discover_page(tab="All")
    logger.info(f"Found {len(campaigns)} campaigns on Discover page")

    # Step 2: Filter by eligibility profile
    logger.info("Step 2/7: Filtering eligible campaigns...")
    from agent.campaign_eligibility import CampaignEligibility

    eligibility = CampaignEligibility()
    eligible = eligibility.check_all(campaigns, env.get("campaigns", []))
    logger.info(f"Eligible campaigns: {len(eligible)}")

    # Step 3: Push to n8n Clip Pipeline
    logger.info("Step 3/7: Pushing eligible campaigns to n8n Clip Pipeline...")
    for camp in eligible:
        # In a real deployment, this would push to n8n via webhook
        # For now, we track the campaign
        summaries.append({
            "campaign_id": camp["campaign_id"],
            "campaign_name": camp["name"],
            "url": camp["url"],
            "status": "ready_for_clipping",
            "cpm": camp.get("cpm"),
        })

    # Step 4: Agent clips the video (via user command)
    # The agent.run --once command clips each campaign video

    # Step 5: Submit clip manifest to Content Rewards
    # via webhook POST JSON

    # Step 6: Track analytics
    # via n8n YouTube API + Content Rewards webhook

    # Step 7: Submit analytics back to Content Rewards
    # via webhook POST JSON

    logger.info(f"Discovery flow complete. {len(summaries)} campaigns ready.")
    return summaries


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Content Rewards Posting & Analytics Pipeline"
    )
    parser.add_argument(
        "--submit",
        nargs=2,
        metavar=("CLIP_ID", "VIDEO_URL"),
        help="Submit a clip to Content Rewards (clip_id, video_url)",
    )
    parser.add_argument(
        "--post-youtube",
        metavar="CLIP_ID",
        help="Post a clip to YouTube via n8n (clip_id)",
    )
    parser.add_argument(
        "--track",
        nargs=2,
        metavar=("CLIP_ID", "ANALYTICS_JSON"),
        help="Track analytics for a clip (clip_id, analytics JSON)",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Run campaign discovery flow",
    )
    parser.add_argument(
        "--list-campaigns",
        action="store_true",
        help="List discovered campaigns",
    )

    args = parser.parse_args()

    if args.submit:
        clip_id, video_url = args.submit
        from agent.pipeline import ClipManifest
        manifest = ClipManifest(
            clip_id=clip_id,
            campaign_id="unknown",
            campaign_name="Unknown",
            video_url=video_url,
            video_filename="",
            duration_seconds=0,
            highlights=[],
        )
        submit_to_content_rewards(manifest)
        logger.info("Submission complete")

    elif args.post_youtube:
        clip_id = args.post_youtube
        from agent.pipeline import ClipManifest
        manifest = ClipManifest(
            clip_id=clip_id,
            campaign_id="unknown",
            campaign_name="Unknown",
            video_url="",
            video_filename="",
            duration_seconds=0,
            highlights=[],
        )
        post_to_youtube_via_n8n(manifest)
        logger.info("YouTube posting complete")

    elif args.track:
        clip_id, analytics_json = args.track
        data = json.loads(analytics_json)
        from agent.pipeline import ClipManifest
        manifest = ClipManifest(
            clip_id=clip_id,
            campaign_id="unknown",
            campaign_name="Unknown",
            video_url="",
            video_filename="",
            duration_seconds=0,
            highlights=[],
        )
        track_analytics(manifest, data)
        logger.info("Analytics tracking complete")

    elif args.discover:
        run_discovery_flow()

    elif args.list_campaigns:
        env = json.load(open("agent/campaign_environment.json"))
        for camp in env.get("campaigns", []):
            print(f"  {camp['name']}: CPM ${camp['cp_min']}/1K, Budget ${camp['remaining_budget']}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
