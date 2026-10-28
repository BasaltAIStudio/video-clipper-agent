"""
Content Rewards Campaign Scraper - Robust Version

Handles:
- Crawling Content Rewards Discover page with retry + backoff
- Login support for protected campaigns
- Multiple parsing strategies (lxml, regex fallback)
- Campaign detail extraction (CPM, budget, platforms, requirements)
- Error handling for network issues, anti-bot, etc.
"""

import csv
import io
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# Try to import lxml for faster parsing
try:
    from lxml import html as lxml_html
    HAVE_LXML = True
except ImportError:
    HAVE_LXML = False

try:
    import xml.etree.ElementTree as ET
    HAVE_XML = True
except ImportError:
    HAVE_XML = False

# Import campaign environment fallback
try:
    from agent.campaign_environment import campaign_environment
    HAVE_ENV = True
except ImportError:
    HAVE_ENV = False
    campaign_environment = {"campaigns": [], "discover_url": "https://contentrewards.com/c/discover"}

# Import config for credentials
try:
    from agent.config import CR_CONTENT_REWARDS_USERNAME, CR_CONTENT_REWARDS_PASSWORD
except ImportError:
    CR_CONTENT_REWARDS_USERNAME = ""
    CR_CONTENT_REWARDS_PASSWORD = ""

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

DISCOVER_URL = "https://contentrewards.com/c/discover"
LOGIN_URL = "https://contentrewards.com/auth/login"


class ScrapingError(Exception):
    """Raised when scraping fails after all retries."""
    pass


class ContentRewardsScraper:
    """Robust Content Rewards campaign scraper with login support and retry logic."""

    def __init__(self, timeout: int = 30, max_retries: int = 3):
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.cache_dir = Path(__file__).parent / "campaign_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._logged_in = False

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    def login(self) -> bool:
        """Log in to Content Rewards using credentials from env vars."""
        if not CR_CONTENT_REWARDS_USERNAME or not CR_CONTENT_REWARDS_PASSWORD:
            logger.warning("Content Rewards credentials not configured")
            return False

        try:
            # Try common login form patterns
            login_data = {
                "username": CR_CONTENT_REWARDS_USERNAME,
                "password": CR_CONTENT_REWARDS_PASSWORD,
                "email": CR_CONTENT_REWARDS_USERNAME,
                "pass": CR_CONTENT_REWARDS_PASSWORD,
            }
            resp = self.session.post(
                LOGIN_URL,
                data=login_data,
                timeout=self.timeout,
                allow_redirects=True,
            )
            if resp.status_code == 200 and ("dashboard" in resp.url.lower() or "logout" in resp.text.lower()):
                self._logged_in = True
                logger.info("Logged in to Content Rewards successfully")
                return True
        except Exception as e:
            logger.warning(f"Login attempt failed: {e}")

        self._logged_in = False
        return False

    # ------------------------------------------------------------------
    # HTTP fetch with retry + backoff
    # ------------------------------------------------------------------
    def _fetch(self, url: str, retries: int = None, method: str = "GET", **kwargs) -> Optional[str]:
        """Fetch a URL with retry logic and exponential backoff."""
        if retries is None:
            retries = self.max_retries

        last_error = None
        for attempt in range(retries):
            try:
                if method.upper() == "POST":
                    response = self.session.post(url, timeout=self.timeout, **kwargs)
                else:
                    response = self.session.get(url, timeout=self.timeout, **kwargs)
                response.raise_for_status()
                return response.text
            except requests.exceptions.RequestException as e:
                last_error = e
                logger.warning(f"Attempt {attempt + 1}/{retries} failed for {url}: {e}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s...
        raise ScrapingError(f"Failed to fetch {url} after {retries} attempts: {last_error}")

    # ------------------------------------------------------------------
    # Discover page scraping
    # ------------------------------------------------------------------
    def scrape_discover_page(self, tab: str = "All") -> List[Dict[str, Any]]:
        """Scrape the Discover page and extract campaign URLs."""
        tab_url = {
            "Clipping": "https://contentrewards.com/c/discover/clipping",
            "UGC": "https://contentrewards.com/c/discover/ugc",
        }.get(tab, DISCOVER_URL)

        logger.info(f"Scraping {tab} campaigns from {tab_url}")
        try:
            html = self._fetch(tab_url)
        except ScrapingError as e:
            logger.error(f"Failed to scrape discover page: {e}")
            return []

        if not html:
            return []

        campaigns = []

        if HAVE_LXML:
            try:
                tree = lxml_html.fromstring(html)
                campaign_links = tree.xpath("//a[contains(@href, '/c/campaigns/')]")

                seen_urls = set()
                for link in campaign_links:
                    href = link.get("href", "")
                    if "/c/campaigns/" in href and href not in seen_urls:
                        seen_urls.add(href)
                        full_url = href if href.startswith("http") else f"https://contentrewards.com{href}"
                        campaigns.append({
                            "url": full_url,
                            "name": (link.text_content() or "").strip() or "unknown",
                            "crawl_date": time.strftime("%Y-%m-%d %H:%M:%S"),
                        })
                logger.info(f"Found {len(campaigns)} campaigns on {tab} tab")
                return campaigns
            except Exception as e:
                logger.warning(f"lxml discover parsing failed: {e}")

        # Regex fallback
        campaigns = self._regex_extract_campaigns(html)
        logger.info(f"Regex fallback found {len(campaigns)} campaigns")
        return campaigns

    def _regex_extract_campaigns(self, html: str) -> List[Dict[str, Any]]:
        """Regex fallback for campaign URL extraction."""
        campaigns = []
        seen = set()
        # Match URLs containing /c/campaigns/
        for match in re.finditer(r'href=["\']([^"\']*/c/campaigns/[^"\']*)["\']', html):
            href = match.group(1)
            if href not in seen:
                seen.add(href)
                full_url = href if href.startswith("http") else f"https://contentrewards.com{href}"
                campaigns.append({"url": full_url, "name": "unknown", "crawl_date": time.strftime("%Y-%m-%d %H:%M:%S")})
        return campaigns

    # ------------------------------------------------------------------
    # Campaign detail scraping
    # ------------------------------------------------------------------
    def scrape_campaign_details(self, campaign: Dict[str, Any]) -> Dict[str, Any]:
        """Scrape full details for a single campaign."""
        url = campaign["url"]
        logger.info(f"Scraping campaign details: {url}")
        try:
            html = self._fetch(url)
        except ScrapingError as e:
            logger.error(f"Failed to fetch campaign page: {e}")
            campaign["error"] = str(e)
            return campaign

        if not html:
            campaign["error"] = "Empty response"
            return campaign

        detail = self._parse_campaign_page(html, url)
        campaign.update(detail)
        return campaign

    def _parse_campaign_page(self, html: str, url: str) -> Dict[str, Any]:
        """Parse campaign detail page."""
        campaign = {
            "url": url,
            "name": "",
            "creator_brand": "",
            "category": "",
            "cpm": None,
            "budget": None,
            "min_payout": None,
            "max_payout": None,
            "allowed_platforms": [],
            "reference_materials": [],
            "creator_requirements": [],
            "content_requirements": [],
            "top_clippers": [],
            "remaining_budget": None,
            "total_budget": None,
            "crawl_date": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        if HAVE_LXML:
            try:
                tree = lxml_html.fromstring(html)

                # Name
                for xpath in ["//h1[contains(@class,'campaign')]", "//h1"]:
                    els = tree.xpath(xpath)
                    if els:
                        campaign["name"] = els[0].text_content().strip()[:200]
                        break

                # Category
                for xpath in ["//span[contains(@class,'category')]", "//div[contains(@class,'category')]"]:
                    els = tree.xpath(xpath)
                    if els:
                        campaign["category"] = els[0].text_content().strip()[:100]
                        break

                # CPM
                cpm_text = ""
                for xpath in ["//span[contains(@class,'cpm')]", "//span[contains(@class,'rate')]", "//span[contains(@class,'earnings')]"]:
                    els = tree.xpath(xpath)
                    if els:
                        cpm_text = els[0].text_content().strip()
                        break
                if cpm_text:
                    nums = re.findall(r'\$?(\d+\.?\d*)', cpm_text)
                    if nums:
                        try:
                            campaign["cpm"] = float(nums[0])
                        except ValueError:
                            pass

                # Platforms
                for a in tree.xpath("//a[contains(@href,'tiktok') or contains(@href,'instagram') or contains(@href,'youtube') or contains(@href,'twitter') or contains(@href,'facebook')]"):
                    txt = (a.text_content() or "").strip().lower()
                    href = a.get("href", "").lower()
                    if "tiktok" in txt or "tiktok" in href:
                        campaign["allowed_platforms"].append("TikTok")
                    elif "instagram" in txt or "instagram" in href:
                        campaign["allowed_platforms"].append("Instagram")
                    elif "youtube" in txt or "youtube" in href:
                        campaign["allowed_platforms"].append("YouTube")

                # Deduplicate platforms
                campaign["allowed_platforms"] = list(set(campaign["allowed_platforms"]))

                # Requirements
                for el in tree.xpath("//div[contains(@class,'requirement') or contains(@class,'rule') or contains(@class,'guideline')]"):
                    txt = el.text_content().strip()
                    if txt and len(txt) < 500:
                        campaign["content_requirements"].append(txt)

            except Exception as e:
                logger.warning(f"lxml parsing failed: {e}")

        # Regex fallback for CPM
        if campaign["cpm"] is None:
            cpm_match = re.search(r'\$(\d+(?:\.\d+)?)\s*/?\s*(?:1K|1k|per\s*1000|views?)', html, re.IGNORECASE)
            if cpm_match:
                try:
                    campaign["cpm"] = float(cpm_match.group(1))
                except ValueError:
                    pass

        return campaign

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------
    def save_cache(self, campaigns: List[Dict[str, Any]], tab: str = "All"):
        cache_file = self.cache_dir / f"discover_{tab.lower()}.json"
        cache_data = {
            "url": DISCOVER_URL if tab == "All" else f"https://contentrewards.com/c/discover/{tab.lower()}",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "campaigns": campaigns,
            "count": len(campaigns),
        }
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2)
        except Exception as e:
            logger.warning(f"Cache save failed: {e}")

    def load_cache(self, tab: str = "All") -> Optional[List[Dict[str, Any]]]:
        cache_file = self.cache_dir / f"discover_{tab.lower()}.json"
        if not cache_file.exists():
            return None
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f).get("campaigns", [])
        except Exception as e:
            logger.warning(f"Cache load failed: {e}")
            return None
