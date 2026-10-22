"""agent/campaign_registry.py - campaign registry (source of truth).

Manages the campaign registry as a JSON/YAML file with per-campaign config:

    {
      "campaigns": [
        {
          "id": "camp_001",
          "name": "Example Affiliate Campaign",
          "url": "https://campaigns.example.com/campaign/1",
          "requirements": {
            "client": "Example Client",
            "budget_usd": 500,
            "zone": "africa"
          },
          "eligible_countries": ["KE", "UG", "TZ", "NG"],
          "submission_webhook": "https://campaigns.example.com/api/v1/clips",
          "submission_method": "POST_JSON",
          "ad_keywords": ["shorts", "youtube", "african creator"]
        }
      ]
    }

Design goals:
- Read-only source of truth for the discovery agent
- Caching supported (re-scrape only when spec is due)
- Decoupled from clipping logic (discovery -> n8n -> clipper -> submission)
"""

from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("agent.campaign_registry")


class RegistryError(RuntimeError):
    """One of the registry operations failed."""


class Campaign:
    """A single campaign entry from the registry."""

    def __init__(self, data: Dict[str, Any]):
        self.id = data.get("id", "unknown")
        self.name = data.get("name", "unknown")
        self.url = data.get("url", "")
        self.requirements = data.get("requirements", {}) or {}
        self.eligible_countries = data.get("eligible_countries", []) or []
        self.submission_webhook = data.get("submission_webhook", "")
        self.submission_method = data.get("submission_method", "").upper()
        self.ad_keywords = data.get("ad_keywords", []) or []
        self.last_scraped_at = data.get("last_scraped_at")
        self.last_updated = data.get("last_updated")
        self.campaign_data = data  # raw data for reference

    @property
    def spec_due(self, refresh_hours: int = 24) -> bool:
        """Check if the campaign spec is due for a refresh."""
        if not self.last_updated:
            return True
        if self.last_updated == "recent":
            return True
        try:
            # Try parsing ISO timestamp
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(str(self.last_updated).replace("Z", "+00:00"))
            import time
            age = time.time() - dt.timestamp()
            return age > refresh_hours * 3600
        except (ValueError, TypeError, ImportError):
            # Fallback: treat as due if no valid timestamp
            return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "requirements": self.requirements,
            "eligible_countries": self.eligible_countries,
            "submission_webhook": self.submission_webhook,
            "submission_method": self.submission_method,
            "ad_keywords": self.ad_keywords,
            "last_scraped_at": self.last_scraped_at,
            "last_updated": self.last_updated,
        }


class CampaignRegistry:
    """Manages the campaign registry file."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or Path(__file__).parent.parent / "campaign_registry.json"
        self._cache: Dict[str, Campaign] = {}
        self._cache_valid = False
        self.load()

    def load(self) -> None:
        """Load the registry from disk."""
        if not self.path.exists():
            self._cache = {}
            self._cache_valid = True
            log.info("Campaign registry not found at %s, using empty registry", self.path)
            return

        try:
            data = json.loads(self.path.read_text())
            campaigns = data.get("campaigns", []) or []
            self._cache = {c.id: Campaign(c) for c in campaigns}
            self._cache_valid = True
            log.info("Loaded %d campaigns from %s", len(self._cache), self.path)
        except (json.JSONDecodeError, IOError) as e:
            log.error("Failed to load campaign registry: %s", e)
            self._cache = {}
            self._cache_valid = True

    def list_campaigns(self) -> List[Campaign]:
        """Return all campaigns in the registry."""
        return list(self._cache.values())

    def get_campaign(self, campaign_id: str) -> Optional[Campaign]:
        """Get a single campaign by ID."""
        return self._cache.get(campaign_id)

    def find_campaign_by_url(self, url: str) -> Optional[Campaign]:
        """Find a campaign by URL."""
        for c in self._cache.values():
            if c.url == url:
                return c
        return None

    def find_eligible_campaigns(self, profile: Dict[str, Any]) -> List[Campaign]:
        """Find campaigns eligible for the user's profile."""
        eligible = []
        for c in self._cache.values():
            is_eligible, reasons = check_eligibility(profile, c.campaign_data)
            if is_eligible:
                eligible.append(c)
        return eligible

    def save(self) -> None:
        """Save the registry to disk."""
        data = {"campaigns": [c.to_dict() for c in self._cache.values()]}
        self.path.write_text(json.dumps(data, indent=2))
        log.info("Saved %d campaigns to %s", len(self._cache), self.path)

    def add_campaign(self, campaign: Dict[str, Any]) -> Campaign:
        """Add a new campaign to the registry."""
        c = Campaign(campaign)
        self._cache[c.id] = c
        self.save()
        return c


# ---------------------------------------------------------------------------
# Eligibility check (replicated here for independence)
def check_country(
    profile: Dict[str, Any],
    campaign: Dict[str, Any],
) -> bool:
    user_country = (profile.get("country") or "").strip()
    user_country_name = (profile.get("country_name") or "").strip().lower()

    if not user_country:
        return True

    eligible = campaign.get("eligible_countries") or []
    eligible_codes = [str(c).strip().upper() for c in eligible]
    eligible_names = [str(c).strip().lower() for c in eligible]

    if user_country.upper() in eligible_codes:
        return True
    if user_country_name in eligible_names:
        return True
    if user_country in eligible:
        return True

    return False


def check_niche(
    profile: Dict[str, Any],
    campaign: Dict[str, Any],
) -> bool:
    user_niche = (profile.get("niche") or "").strip().lower()

    if not user_niche or user_niche == "flexible":
        return True

    ad_keywords = campaign.get("ad_keywords") or []
    if isinstance(ad_keywords, str):
        ad_keywords = [ad_keywords]

    if user_niche in [str(k).lower() for k in ad_keywords]:
        return True

    return bool(ad_keywords)


def check_budget(
    profile: Dict[str, Any],
    campaign: Dict[str, Any],
) -> bool:
    user_budget = profile.get("budget")
    if user_budget is None:
        return True

    campaign_budget = campaign.get("requirements", {}).get("budget_usd")
    if campaign_budget is None:
        return True

    try:
        return float(campaign_budget) >= float(user_budget)
    except (ValueError, TypeError):
        return True


def check_tier(
    profile: Dict[str, Any],
    campaign: Dict[str, Any],
) -> bool:
    user_tier = (profile.get("tier") or "").strip().lower()
    if not user_tier:
        return True

    req = campaign.get("requirements", {})
    campaign_tier = str(req.get("tier", "")).strip().lower()
    return campaign_tier == user_tier


def check_past_history(
    profile: Dict[str, Any],
    campaign: Dict[str, Any],
) -> bool:
    if not profile.get("past_campaigns"):
        return True
    return True


def check_submission_method(
    profile: Dict[str, Any],
    campaign: Dict[str, Any],
) -> bool:
    user_methods = [str(m).strip().lower() for m in (profile.get("submission_method_preference") or [])]
    if not user_methods:
        return True

    campaign_method = str(campaign.get("submission_method", "")).strip().lower()
    return campaign_method in user_methods


def check_eligibility(
    profile: Dict[str, Any],
    campaign: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    reasons: List[str] = []

    if not check_country(profile, campaign):
        reasons.append("country: user is %s, campaign does not list this country"
                       % profile.get("country_name", profile.get("country", "unknown")))

    if not check_niche(profile, campaign):
        reasons.append("niche: user niche '%s' not matched by campaign keywords"
                       % profile.get("niche"))

    if not check_budget(profile, campaign):
        reasons.append(
            "budget: user budget %s not met by campaign budget %s"
            % (profile.get("budget"), campaign.get("requirements", {}).get("budget_usd"))
        )

    if not check_tier(profile, campaign):
        reasons.append("tier: user tier '%s' not matched by campaign tier" % profile.get("tier"))

    if not check_past_history(profile, campaign):
        reasons.append("past_history: conflict with user's past campaigns")

    if not check_submission_method(profile, campaign):
        reasons.append(
            "submission_method: user prefers %s, campaign uses %s"
            % (profile.get("submission_method_preference"), campaign.get("submission_method"))
        )

    return len(reasons) == 0, reasons


def get_eligibility_report(
    profile: Dict[str, Any],
    campaign: Dict[str, Any],
) -> Dict[str, Any]:
    is_eligible, reasons = check_eligibility(profile, campaign)
    return {
        "campaign_id": campaign.get("id", "unknown"),
        "campaign_name": campaign.get("name", "unknown"),
        "campaign_url": campaign.get("url", ""),
        "eligible": is_eligible,
        "reasons": reasons,
        "checks": {
            "country": check_country(profile, campaign),
            "niche": check_niche(profile, campaign),
            "budget": check_budget(profile, campaign),
            "tier": check_tier(profile, campaign),
            "past_history": check_past_history(profile, campaign),
            "submission_method": check_submission_method(profile, campaign),
        },
    }
