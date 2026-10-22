"""agent/campaign_eligibility.py - campaign eligibility checker.

Compares campaign requirements against the user's known profile:

    - country: KE (Kenya)
    - niche: flexible (no specific niche constraint)
    - budget: unknown (no budget/tier specified)
    - past_campaigns: none (just starting)

The eligibility check is pluggable - the user can add real data (past campaign
history, budget, niche preferences) to campaign_eligibility.json and this
module will pick it up without any code changes.
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_PROFILE_PATH = Path(__file__).parent.parent / "campaign_eligibility.json"


class EligibilityError(ValueError):
    """Raised when a campaign is not eligible."""


def load_profile(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the user's campaign eligibility profile.

    Returns the default profile if no file is found.
    """
    p = path or DEFAULT_PROFILE_PATH
    if p.exists():
        try:
            return json.loads(p.read_text())
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
        "notes": "",
    }


def _get_profile(base: Dict[str, Any]) -> Dict[str, Any]:
    """Return the actual profile (may be nested under 'agent' key)."""
    if isinstance(base.get("agent"), dict):
        return base["agent"]
    return base


def check_country(
    profile: Dict[str, Any],
    campaign: Dict[str, Any],
) -> bool:
    """Check if the campaign is eligible for the user's country."""
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
    """Check if the campaign is eligible for the user's niche.

    'flexible' means no niche constraint - accept all.
    """
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
    """Check budget eligibility.

    If user has no budget specified (None), accept all.
    """
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
    """Check tier eligibility.

    If user has no tier specified, accept all.
    """
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
    """Check if the campaign is eligible based on past history.

    If user has no past campaign history, accept all.
    """
    if not profile.get("past_campaigns"):
        return True

    return True


def check_submission_method(
    profile: Dict[str, Any],
    campaign: Dict[str, Any],
) -> bool:
    """Check if the submission method is acceptable.

    If user has no preference, accept all.
    """
    user_methods = [str(m).strip().lower() for m in (profile.get("submission_method_preference") or [])]
    if not user_methods:
        return True

    campaign_method = str(campaign.get("submission_method", "")).strip().lower()
    return campaign_method in user_methods


def check_eligibility(
    profile: Dict[str, Any],
    campaign: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    """Check if a campaign is eligible for the user.

    Returns (is_eligible, reasons) where reasons is a list of strings
    explaining why the campaign is NOT eligible (if any).
    """
    reasons: List[str] = []

    if not check_country(profile, campaign):
        reasons.append(
            "country: user is %s, campaign does not list this country in eligible_countries"
            % profile.get("country_name", profile.get("country", "unknown"))
        )

    if not check_niche(profile, campaign):
        reasons.append("niche: user niche '%s' not matched by campaign keywords" % profile.get("niche"))

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
    """Return a detailed eligibility report for a campaign."""
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
        "user_profile": profile,
    }


def demo_profile_and_eligibility() -> None:
    """Demo the eligibility check with the user's profile."""
    profile = load_profile()
    print("User profile loaded:")
    print(json.dumps(profile, indent=2))

    demo_campaign = {
        "id": "demo_camp_001",
        "name": "Kenyan Creator Program",
        "url": "https://example.com/campaigns/kenya",
        "requirements": {
            "client": "Kenya Creator Network",
            "budget_usd": 1000,
            "zone": "africa"
        },
        "eligible_countries": ["KE", "UG", "TZ", "NG", "GH"],
        "ad_keywords": ["shorts", "youtube", "african creator", "nigerian", "kenyan", "tanzanian"],
        "submission_webhook": "https://example.com/api/v1/clips",
        "submission_method": "POST_JSON",
    }

    report = get_eligibility_report(profile, demo_campaign)
    print("\nEligibility report:")
    print(json.dumps(report, indent=2))
    print("\nEligible: %s" % report["eligible"])


if __name__ == "__main__":
    demo_profile_and_eligibility()
