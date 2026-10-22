"""agent/campaign_discovery.py - campaign discovery agent.

The campaign discovery agent:

1. Reads the campaign registry (JSON)
2. For each campaign, scrapes the URL to verify (via Composio in n8n or direct)
3. Checks eligibility against the user's profile
4. Pushes eligible campaigns to n8n (Clip Pipeline Entry stage)
5. n8n calls the clipper agent -> returns clip manifests
6. Discovery agent POSTs clip analytics to the campaign platform

Architecture:
    Campaign Registry -> Discovery Agent -> n8n (Clip Entry) -> Clipper -> Clip Manifests
                                                 |                         |
                                                 +---> POST analytics ----+

Deployment: Composio (Firecrawl/Browserbase/BrowserAct/ScrapingBee/ScrapingDog)
in n8n is used for scraping. The discovery agent runs as a one-shot or
scheduled task.
"""

from __future__ import annotations
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .campaign_adapter import (
    submit_clips_to_campaign,
    SubmissionError,
)
from .campaign_registry import (
    CampaignRegistry,
    get_eligibility_report,
)

log = logging.getLogger("agent.campaign_discovery")

# n8n HTTP endpoint for the Clip Pipeline Entry stage
N8N_CLIP_ENTRY_URL = os.getenv("N8N_CLIP_ENTRY_URL", "http://localhost:5678")

# Campaign platform submission webhook
CAMPAIGN_WEBHOOK_URL = os.getenv("CAMPAIGN_WEBHOOK_URL", "")

# Cache file for discovered/cached campaign metadata
CACHE_DIR = Path(__file__).parent.parent / "campaign_cache"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_cache_dir() -> None:
    """Create the cache directory if it doesn't exist."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_cache() -> Dict[str, Any]:
    """Load the campaign metadata cache."""
    ensure_cache_dir()
    cache_file = CACHE_DIR / "campaign_metadata.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_cache(cache: Dict[str, Any]) -> None:
    """Save the campaign metadata cache."""
    ensure_cache_dir()
    cache_file = CACHE_DIR / "campaign_metadata.json"
    cache_file.write_text(json.dumps(cache, indent=2))


def scrape_campaign_url(url: str, max_depth: int = 2) -> Dict[str, Any]:
    """Scrape a campaign website to verify and extract metadata.

    Uses Composio (n8n) via HTTP POST to the n8n scrape workflow.
    Falls back to a simple HTTP GET if the scrape workflow is not available.

    Returns:
        Dict with keys: campaign_id, title, description, keywords, eligibility, raw
    """
    log.info("Scraping campaign URL: %s", url)

    # Check if n8n scrape workflow is available
    n8n_scrape_available = os.getenv("N8N_SCRAPE_WORKFLOW_ID") is not None

    if n8n_scrape_available:
        # Use Composio (n8n) to scrape
        scrape_url = os.getenv("N8N_SCRAPE_URL", N8N_CLIP_ENTRY_URL)
        payload = {
            "url": url,
            "max_depth": max_depth,
            "mode": "campaign_discovery",
        }

        try:
            resp = requests.post(
                scrape_url,
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            scraped = resp.json()
            log.info("Scraped via n8n: %s", scraped.get("title", url))
            return {
                "campaign_id": scraped.get("campaign_id", "unknown"),
                "title": scraped.get("title", ""),
                "description": scraped.get("description", ""),
                "keywords": scraped.get("keywords", []),
                "eligibility": scraped.get("eligibility", {}),
                "raw": scraped,
                "scraped_via": "n8n",
            }
        except Exception as e:
            log.warning("n8n scrape failed: %s, falling back to simple GET", e)

    # Fallback: simple HTTP GET
    try:
        import requests
        resp = requests.get(url, timeout=10)
        # Note: This is a very basic fallback - real scraping needs a proper
        # headless browser or scraping API. The n8n workflow should be used
        # in production.
        log.info("Simple GET succeeded: %d bytes", len(resp.text))
        # Extract basic metadata
        title = ""
        if "<title>" in resp.text:
            start = resp.text.find("<title>") + 7
            end = resp.text.find("</title>")
            if end > start:
                title = resp.text[start:end].strip()

        return {
            "campaign_id": _parse_campaign_id(url),
            "title": title,
            "description": "",
            "keywords": [],
            "eligibility": {},
            "raw": {"status_code": resp.status_code},
            "scraped_via": "simple_get",
        }
    except Exception as e:
        log.error("Failed to scrape %s: %s", url, e)
        raise


def _parse_campaign_id(url: str) -> str:
    """Extract a campaign ID from a URL."""
    u = url.strip()
    for prefix in ("https://", "http://", "www."):
        if u.startswith(prefix):
            u = u[len(prefix):]
    if u.endswith(".json"):
        u = u[: -len(".json")]
    return u.rstrip("/").split("/")[-1].split("?")[0].split("#")[0] or "unknown"


def check_eligibility(profile: Dict[str, Any], campaign: Dict[str, Any]) -> Dict[str, Any]:
    """Check campaign eligibility against the user's profile."""
    return get_eligibility_report(profile, campaign)


def push_to_n8n(campaign: Dict[str, Any]) -> Dict[str, Any]:
    """Push an eligible campaign to n8n's Clip Pipeline Entry stage.

    Returns the n8n response.
    """
    log.info("Pushing campaign %s to n8n", campaign.get("id"))

    payload = {
        "campaign_id": campaign.get("id", "unknown"),
        "campaign_name": campaign.get("name", "unknown"),
        "campaign_url": campaign.get("url", ""),
        "agent_url": os.getenv("AGENT_URL", "http://localhost:8000"),
        "submit": True,
    }

    try:
        resp = requests.post(
            N8N_CLIP_ENTRY_URL,
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        log.info("n8n accepted campaign %s", campaign.get("id"))
        return {
            "success": True,
            "status_code": resp.status_code,
            "response": resp.json() if resp.content else {},
        }
    except Exception as e:
        log.error("Failed to push to n8n: %s", e)
        return {
            "success": False,
            "error": str(e),
            "campaign_id": campaign.get("id"),
        }


def run_discovery(
    user_profile: Optional[Dict[str, Any]] = None,
    campaign_registry_path: Optional[Path] = None,
    submit_clips: bool = False,
    campaign_clips: Optional[List[Dict[str, Any]]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the full campaign discovery workflow.

    1. Load campaign registry
    2. For each campaign, scrape URL and verify
    3. Check eligibility
    4. Push eligible campaigns to n8n
    5. (Optional) Submit clip analytics back to campaign platform

    Returns a workflow report.
    """
    config = config or {}
    profile = user_profile or load_profile()

    log.info("Campaign discovery started")
    log.info("User profile: %s", json.dumps(profile, indent=2))

    registry = CampaignRegistry(campaign_registry_path)
    cache = load_cache()
    results = {
        "profile": profile,
        "total_campaigns": len(registry.list_campaigns()),
        "eligible": [],
        "not_eligible": [],
        "errors": [],
        "submitted": [],
        "n8n_pushes": [],
        "submissions": [],
        "cache": cache,
        "completed_at": _now_iso(),
    }

    # Step 1: Scrape and verify each campaign
    for camp in registry.list_campaigns():
        log.info("Discovering campaign: %s (%s)", camp.name, camp.url)

        try:
            scraped = scrape_campaign_url(camp.url)

            # Merge scraped data into the campaign
            campaign_data = camp.campaign_data or {}
            campaign_data["scraped_title"] = scraped.get("title", campaign_data.get("name", ""))
            campaign_data["scraped_description"] = scraped.get("description", "")
            campaign_data["scraped_keywords"] = scraped.get("keywords", campaign_data.get("ad_keywords", []))
            campaign_data["last_scraped_at"] = _now_iso()

            # Step 2: Check eligibility
            report = check_eligibility(profile, campaign_data)

            if report["eligible"]:
                results["eligible"].append({
                    "campaign": camp,
                    "eligibility_report": report,
                    "scraped": scraped,
                })
                log.info("Campaign %s is ELIGIBLE", camp.id)
            else:
                results["not_eligible"].append({
                    "campaign": camp,
                    "eligibility_report": report,
                    "scraped": scraped,
                })
                log.info("Campaign %s is NOT eligible: %s", camp.id, "; ".join(report["reasons"]))
        except Exception as e:
            log.error("Error processing campaign %s: %s", camp.id, e)
            results["errors"].append({
                "campaign_id": camp.id,
                "error": str(e),
            })

    # Step 3: Push eligible campaigns to n8n
    for entry in results["eligible"]:
        camp = entry["campaign"]
        n8n_result = push_to_n8n(camp.campaign_data)
        results["n8n_pushes"].append({
            "campaign_id": camp.id,
            "success": n8n_result["success"],
        })
        if n8n_result["success"]:
            # Step 4: If clips were returned, submit analytics
            if submit_clips and campaign_clips:
                from .campaign_adapter import submit_clips_to_campaign
                try:
                    sub_result = submit_clips_to_campaign(campaign_clips, camp.campaign_data, config)
                    results["submissions"].append({
                        "campaign_id": camp.id,
                        "success": sub_result.get("success", False),
                        "status_code": sub_result.get("status_code"),
                    })
                    results["submitted"].append(camp.id)
                except SubmissionError as e:
                    log.error("Submission failed for campaign %s: %s", camp.id, e)
                    results["submissions"].append({
                        "campaign_id": camp.id,
                        "success": False,
                        "error": str(e),
                    })

    # Save cache
    save_cache(results["cache"])

    # Summary
    results["summary"] = {
        "total_campaigns": results["total_campaigns"],
        "eligible": len(results["eligible"]),
        "not_eligible": len(results["not_eligible"]),
        "errors": len(results["errors"]),
        "n8n_pushes": len([p for p in results["n8n_pushes"] if p["success"]]),
        "submissions": len([s for s in results["submissions"] if s.get("success", False)]),
        "submitted_campaigns": results["submitted"],
        "completed_at": results["completed_at"],
    }

    log.info("Campaign discovery complete: %s", json.dumps(results["summary"], indent=2))
    return results


def load_profile() -> Dict[str, Any]:
    """Load the user's campaign eligibility profile."""
    profile_path = Path(__file__).parent.parent / "campaign_eligibility.json"
    if profile_path.exists():
        try:
            return json.loads(profile_path.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return {
        "country": "KE",
        "country_name": "Kenya",
        "niche": "flexible",
        "budget": None,
        "tier": None,
        "past_campaigns": [],
        "preferred_campaigns": [],
        "submission_method_preference": [],
        "notes": "New creator. No budget specified.",
    }


# ---------------------------------------------------------------------------
# CLI entry point
def main() -> int:
    """CLI entry point for the campaign discovery agent."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="campaign_discovery",
        description="Campaign discovery agent for the Video Clipper Agent",
    )
    parser.add_argument(
        "--profile",
        default="campaign_eligibility.json",
        help="Path to the user's eligibility profile JSON",
    )
    parser.add_argument(
        "--registry",
        default="campaign_registry.json",
        help="Path to the campaign registry JSON",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Submit clip analytics to the campaign platform after clipping",
    )
    parser.add_argument(
        "--registry-json",
        help="Path to campaign registry JSON file (or provide inline JSON)",
    )
    parser.add_argument(
        "--save-registry",
        help="Path to save the campaign registry JSON",
    )
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Load profile
    profile_path = Path(args.profile)
    if profile_path.exists():
        try:
            profile = json.loads(profile_path.read_text())
            log.info("Loaded profile from %s", profile_path)
        except (json.JSONDecodeError, IOError) as e:
            log.error("Failed to load profile: %s", e)
            profile = None
    else:
        log.info("No profile file found, using defaults")
        profile = None

    # Load registry
    registry_path = Path(args.registry)
    if registry_path.exists():
        log.info("Loading registry from %s", registry_path)
    else:
        # Create a default registry
        log.info("No registry file found, creating a default")
        default_registry = {
            "campaigns": [
                {
                    "id": "camp_test_001",
                    "name": "Test Kenya Campaign",
                    "url": "https://example.com/campaigns/kenya-test",
                    "requirements": {
                        "client": "Test Client",
                        "budget_usd": 500,
                        "zone": "africa",
                    },
                    "eligible_countries": ["KE", "UG", "TZ", "NG"],
                    "submission_webhook": "https://example.com/api/v1/clips",
                    "submission_method": "POST_JSON",
                    "ad_keywords": ["shorts", "youtube", "african creator"],
                }
            ]
        }
        registry_path.write_text(json.dumps(default_registry, indent=2))
        log.info("Created default registry at %s", registry_path)

    # Run discovery
    results = run_discovery(
        user_profile=profile,
        campaign_registry_path=registry_path,
        submit_clips=args.submit,
        config={"submit_timeout": 30},
    )

    # Print summary
    print("\n" + "=" * 60)
    print("Campaign Discovery Report")
    print("=" * 60)
    summary = results.get("summary", {})
    print("Total campaigns: %d" % summary.get("total_campaigns", 0))
    print("Eligible:        %d" % summary.get("eligible", 0))
    print("Not eligible:    %d" % summary.get("not_eligible", 0))
    print("Errors:          %d" % summary.get("errors", 0))
    print("n8n pushes:      %d" % summary.get("n8n_pushes", 0))
    print("Submissions:     %d" % summary.get("submissions", 0))
    print("Submitted ids:   %s" % ", ".join(summary.get("submitted_campaigns", [])))
    print("=" * 60 + "\n")

    # Save registry if requested
    if args.save_registry:
        registry = CampaignRegistry(args.save_registry)
        registry.save()

    return 0


if __name__ == "__main__":
    sys.exit(main())
