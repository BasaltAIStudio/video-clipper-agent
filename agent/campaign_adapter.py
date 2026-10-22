"""agent/campaign_adapter.py - campaign-specific submission adapters.

The campaign platform may accept clips in different formats. This module
provides adapters for:

- POST_JSON:      JSON body to webhook URL
- GET_TOKEN:      GET request with auth token in query string
- FORM_POST:      HTML form POST
- CSV_UPLOAD:     CSV file upload
- JSON_UPLOAD:    JSON file upload

Each adapter implements submit(clips, campaign, config) -> submission_result.
"""

from __future__ import annotations
import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("agent.campaign_adapter")


class SubmissionError(RuntimeError):
    """Raised when a submission fails."""


class BaseAdapter(ABC):
    """Abstract base class for campaign submission adapters."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    @abstractmethod
    def submit(self, clips: List[Dict[str, Any]], campaign: Dict[str, Any]) -> Dict[str, Any]:
        """Submit clips to the campaign platform.

        Returns a submission result dict.
        """
        pass

    @abstractmethod
    def name(self) -> str:
        """Adapter name for logging."""
        pass


class PostJSONAdapter(BaseAdapter):
    """POST JSON body to a webhook URL."""

    def name(self) -> str:
        return "POST_JSON"

    def submit(self, clips: List[Dict[str, Any]], campaign: Dict[str, Any]) -> Dict[str, Any]:
        webhook = campaign.get("submission_webhook")
        if not webhook:
            raise SubmissionError("campaign has no submission_webhook")

        payload = {
            "campaign_id": campaign.get("id", "unknown"),
            "campaign_name": campaign.get("name", "unknown"),
            "clips": clips,
            "submitted_at": self._now_iso(),
            "adapter": self.name(),
        }

        log.info("POST_JSON: submitting %d clips to %s", len(clips), webhook)

        try:
            resp = requests.post(
                webhook,
                json=payload,
                timeout=float(self.config.get("submit_timeout", 30)),
            )
            resp.raise_for_status()
            return {
                "success": True,
                "status_code": resp.status_code,
                "response": resp.json() if resp.content else {},
                "submitted_at": self._now_iso(),
            }
        except requests.RequestException as e:
            log.error("POST_JSON submission failed: %s", e)
            raise SubmissionError("POST_JSON failed: %s" % e) from e

    def _now_iso(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()


class GetTokenAdapter(BaseAdapter):
    """GET request with auth token in query string."""

    def name(self) -> str:
        return "GET_TOKEN"

    def submit(self, clips: List[Dict[str, Any]], campaign: Dict[str, Any]) -> Dict[str, Any]:
        webhook = campaign.get("submission_webhook")
        if not webhook:
            raise SubmissionError("campaign has no submission_webhook")

        token = self.config.get("auth_token")
        if not token:
            raise SubmissionError("GET_TOKEN adapter requires auth_token in config")

        # Convert JSON to query string params
        payload = {
            "campaign_id": campaign.get("id", "unknown"),
            "clips": json.dumps(clips),
            "submitted_at": self._now_iso(),
        }

        log.info("GET_TOKEN: submitting to %s", webhook)

        try:
            resp = requests.get(
                webhook,
                params=payload,
                timeout=float(self.config.get("submit_timeout", 30)),
            )
            resp.raise_for_status()
            return {
                "success": True,
                "status_code": resp.status_code,
                "response": resp.text,
                "submitted_at": self._now_iso(),
            }
        except requests.RequestException as e:
            log.error("GET_TOKEN submission failed: %s", e)
            raise SubmissionError("GET_TOKEN failed: %s" % e) from e

    def _now_iso(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()


class FormPostAdapter(BaseAdapter):
    """HTML form POST."""

    def name(self) -> str:
        return "FORM_POST"

    def submit(self, clips: List[Dict[str, Any]], campaign: Dict[str, Any]) -> Dict[str, Any]:
        webhook = campaign.get("submission_webhook")
        if not webhook:
            raise SubmissionError("campaign has no submission_webhook")

        # Flatten clips into form data
        form_data = {
            "campaign_id": campaign.get("id", "unknown"),
            "campaign_name": campaign.get("name", "unknown"),
            "clip_count": len(clips),
            "submitted_at": self._now_iso(),
        }

        log.info("FORM_POST: submitting to %s", webhook)

        try:
            resp = requests.post(
                webhook,
                data=form_data,
                timeout=float(self.config.get("submit_timeout", 30)),
            )
            resp.raise_for_status()
            return {
                "success": True,
                "status_code": resp.status_code,
                "response": resp.text,
                "submitted_at": self._now_iso(),
            }
        except requests.RequestException as e:
            log.error("FORM_POST submission failed: %s", e)
            raise SubmissionError("FORM_POST failed: %s" % e) from e

    def _now_iso(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()


class CSVUploadAdapter(BaseAdapter):
    """Upload a CSV file."""

    def name(self) -> str:
        return "CSV_UPLOAD"

    def submit(self, clips: List[Dict[str, Any]], campaign: Dict[str, Any]) -> Dict[str, Any]:
        webhook = campaign.get("submission_webhook")
        if not webhook:
            raise SubmissionError("campaign has no submission_webhook")

        # Build CSV content
        csv_lines = ["campaign_id,campaign_name,start,end,title"]
        for i, clip in enumerate(clips):
            csv_lines.append(
                "%s,%s,%.0f,%.0f,%s"
                % (clip.get("campaign_id", ""), clip.get("campaign_name", ""),
                   clip.get("start", 0), clip.get("end", 0), clip.get("title", ""))
            )

        log.info("CSV_UPLOAD: %d rows to %s", len(clips), webhook)

        try:
            resp = requests.post(
                webhook,
                files={"file": ("clips.csv", "\n".join(csv_lines), "text/csv")},
                timeout=float(self.config.get("submit_timeout", 30)),
            )
            resp.raise_for_status()
            return {
                "success": True,
                "status_code": resp.status_code,
                "response": resp.text,
                "submitted_at": self._now_iso(),
            }
        except requests.RequestException as e:
            log.error("CSV_UPLOAD submission failed: %s", e)
            raise SubmissionError("CSV_UPLOAD failed: %s" % e) from e

    def _now_iso(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()


class JSONUploadAdapter(BaseAdapter):
    """Upload a JSON file."""

    def name(self) -> str:
        return "JSON_UPLOAD"

    def submit(self, clips: List[Dict[str, Any]], campaign: Dict[str, Any]) -> Dict[str, Any]:
        webhook = campaign.get("submission_webhook")
        if not webhook:
            raise SubmissionError("campaign has no submission_webhook")

        payload = {
            "campaign_id": campaign.get("id", "unknown"),
            "campaign_name": campaign.get("name", "unknown"),
            "clips": clips,
            "submitted_at": self._now_iso(),
        }

        log.info("JSON_UPLOAD: submitting to %s", webhook)

        try:
            resp = requests.post(
                webhook,
                files={"file": ("clips.json", json.dumps(payload), "application/json")},
                timeout=float(self.config.get("submit_timeout", 30)),
            )
            resp.raise_for_status()
            return {
                "success": True,
                "status_code": resp.status_code,
                "response": resp.text,
                "submitted_at": self._now_iso(),
            }
        except requests.RequestException as e:
            log.error("JSON_UPLOAD submission failed: %s", e)
            raise SubmissionError("JSON_UPLOAD failed: %s" % e) from e

    def _now_iso(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Adapter factory
ADAPTERS = {
    "POST_JSON": PostJSONAdapter,
    "GET_TOKEN": GetTokenAdapter,
    "FORM_POST": FormPostAdapter,
    "CSV_UPLOAD": CSVUploadAdapter,
    "JSON_UPLOAD": JSONUploadAdapter,
}


def get_adapter(method: str, config: Dict[str, Any]) -> Optional[BaseAdapter]:
    """Return an adapter instance for the given method."""
    method = (method or "").upper()
    adapter_class = ADAPTERS.get(method)
    if not adapter_class:
        log.warning("Unknown submission method: %s", method)
        return None
    return adapter_class(config)


def submit_clips_to_campaign(
    clips: List[Dict[str, Any]],
    campaign: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Submit clips to a campaign using the configured adapter."""
    config = config or {}
    method = (campaign.get("submission_method") or "POST_JSON").upper()

    adapter = get_adapter(method, config)
    if not adapter:
        raise SubmissionError("No adapter for method: %s" % method)

    log.info("Using adapter: %s", adapter.name())
    return adapter.submit(clips, campaign)
