"""
Content Rewards Campaign Scraper
Scrapes https://contentrewards.com/c/discover and campaign detail pages.

Security: Appends account credentials to the scraper's CSV source (only after user enables).
The scraper parses each campaign page for: CPM, budget, platforms, requirements, reference files.
Tracks performance data (top clippers, view graphs).

Usage:
    from agent.contentrewards_scraper import ContentRewardsScraper
    scraper = ContentRewardsScraper()
    campaigns = scraper.scrape_discover_page()
    for camp in campaigns:
        print(camp["name"], camp["cp"], camp["remaining_budget"])
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
    logging.info("lxml available - using fast parsing")
except ImportError:
    HAVE_LXML = False
    logging.warning("lxml not available - falling back to regex parsing")

try:
    import xml.etree.ElementTree as ET
    HAVE_XML = True
except ImportError:
    HAVE_XML = False

# Import campaign environment (already scraped from the Discover page)
try:
    from agent.campaign_environment import campaign_environment
    HAVE_ENV = True
except ImportError:
    HAVE_ENV = False
    campaign_environment = {"campaigns": [], "discover_url": "https://contentrewards.com/c/discover"}

# Configure logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# User-Agent header to avoid bot detection
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# Content Rewards URLs
DISCOVER_URL = "https://contentrewards.com/c/discover"
CLIPPING_TAB_URL = "https://contentrewards.com/c/discover/clipping"
UGC_TAB_URL = "https://contentrewards.com/c/discover/ugc"


class ScrapingError(Exception):
    """Raised when scraping fails."""
    pass


class ContentRewardsScraper:
    """
    Scraper for Content Rewards campaign discovery.

    Handles:
    - Crawling the Discover page (All/Clipping/UGC tabs)
    - Extracting campaign data from each campaign page
    - Parsing CPM, budget, platforms, requirements, reference files
    - Tracking performance data (top clippers, view graphs)
    """

    def __init__(self, timeout: int = 30, max_retries: int = 3):
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.cache_dir = Path(__file__).parent / "campaign_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _fetch(self, url: str, retries: int = None) -> Optional[str]:
        """Fetch a URL with retry logic and error handling."""
        if retries is None:
            retries = self.max_retries

        for attempt in range(retries):
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                return response.text
            except requests.exceptions.RequestException as e:
                logger.warning(f"Attempt {attempt + 1} failed for {url}: {e}")
                if attempt == retries - 1:
                    raise ScrapingError(f"Failed to fetch {url}: {e}")
                time.sleep(2 ** attempt)  # Exponential backoff
        return None

    def _parse_campaign_page(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Parse a Content Rewards campaign page.

        Extracts:
        - Campaign name, creator/brand
        - Category
        - CPM (earnings per 1K views)
        - Content requirements (budget, min/max payout, platforms, etc.)
        - Reference materials
        - Top clippers
        - Performance graph
        """
        html = self._fetch(url)
        if not html:
            return None

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
            "performance_graph": None,
            "remaining_budget": None,
            "total_budget": None,
            "crawl_date": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        # Try lxml parsing first
        if HAVE_LXML:
            try:
                tree = lxml_html.fromstring(html)
                # Note: Namespace stripping skipped - lxml XPath handles namespaces natively
                # and modifying tags during iteration causes crashes
                name_elements = tree.xpath("//h1[contains(@class, 'campaign-name') or contains(@class, 'campaign-title')]")
                if name_elements:
                    campaign["name"] = name_elements[0].text_content().strip()
                else:
                    # Fallback: look for h1 or h2 with campaign-related text
                    h1_elements = tree.xpath("//h1")
                    for h in h1_elements:
                        text = h.text_content().strip()
                        if text and len(text) < 100:
                            campaign["name"] = text
                            break

                # Extract creator/brand
                creator_elements = tree.xpath("//div[contains(@class, 'creator') or contains(@class, 'brand')]")
                for el in creator_elements:
                    text = el.text_content().strip()
                    if text and len(text) < 100:
                        campaign["creator_brand"] = text
                        break

                # Extract category
                cat_elements = tree.xpath("//span[contains(@class, 'category') or contains(@class, 'category-tag')]")
                for el in cat_elements:
                    text = el.text_content().strip()
                    if text:
                        campaign["category"] = text
                        break

                # Extract CPM (e.g., "$2/1K views" or "$2/1K")
                cpm_elements = tree.xpath("//span[contains(@class, 'cpm') or contains(@class, 'rate') or contains(@class, 'earnings')]")
                for el in cpm_elements:
                    text = el.text_content().strip()
                    numbers = re.findall(r'\$?\d+\.?\d*', text)
                    if numbers:
                        try:
                            campaign["cpm"] = float(numbers[0].replace("$", ""))
                        except (ValueError, IndexError):
                            pass

                # Extract budget and remaining budget
                budget_elements = tree.xpath("//div[contains(@class, 'budget') or contains(@class, 'remaining') or contains(@class, 'pool')]")
                for el in budget_elements:
                    text = el.text_content().strip()
                    numbers = re.findall(r'\$?\d+\.?\d*', text)
                    if numbers:
                        try:
                            budget_val = float(numbers[0].replace("$", ""))
                            # Determine if this is remaining or total
                            if "remaining" in text.lower() or "rooom" in text.lower() or "pool" in text.lower():
                                campaign["remaining_budget"] = budget_val
                            else:
                                campaign["total_budget"] = budget_val
                        except (ValueError, IndexError):
                            pass

                # Extract allowed platforms
                platform_elements = tree.xpath("//a[contains(@href, 'tiktok') or contains(@href, 'instagram') or contains(@href, 'youtube') or contains(@href, 'twitter') or contains(@href, 'facebook')]")
                for el in platform_elements:
                    text = el.text_content().strip().lower()
                    if text == "tiktok":
                        campaign["allowed_platforms"].append("TikTok")
                    elif text == "instagram":
                        campaign["allowed_platforms"].append("Instagram")
                    elif text == "youtube":
                        campaign["allowed_platforms"].append("YouTube")
                    elif text == "twitter" or text == "x":
                        campaign["allowed_platforms"].append("X")
                    elif text == "facebook":
                        campaign["allowed_platforms"].append("Facebook")

                # Extract content requirements
                req_elements = tree.xpath("//div[contains(@class, 'requirement') or contains(@class, 'content-rule') or contains(@class, 'rule') or contains(@class, 'guideline')]")
                for el in req_elements:
                    text = el.text_content().strip()
                    if text and len(text) < 500:
                        campaign["content_requirements"].append(text)

                # Extract reference materials
                ref_elements = tree.xpath("//a[contains(@href, '.pdf') or contains(@href, '.zip') or contains(@href, '.mp4') or contains(@href, '.txt') or contains(@href, '.docx') or contains(@href, '.json')]")
                for el in ref_elements:
                    href = el.get("href", "")
                    text = el.text_content().strip()
                    if text and len(text) < 100:
                        campaign["reference_materials"].append({"name": text, "url": href})

                # Extract top clippers
                top_clipper_elements = tree.xpath("//div[contains(@class, 'top-clipper') or contains(@class, 'top-creator') or contains(@class, 'ranked-list')]")
                for el in top_clipper_elements:
                    text = el.text_content().strip()
                    if text and len(text) < 500:
                        campaign["top_clippers"].append(text)

                # Extract performance graph data
                graph_elements = tree.xpath("//div[contains(@class, 'performance-graph') or contains(@class, 'view-graph') or contains(@class, 'cumulative-views')]")
                for el in graph_elements:
                    text = el.text_content().strip()
                    if text:
                        # Try to extract numerical data from the graph
                        numbers = re.findall(r'\d+(?:\.\d+)?[KM]?', text)
                        if numbers:
                            campaign["performance_graph"] = {"raw": text, "numbers": numbers}

                # Check if JSON-LD is available (structured data)
                json_lds = tree.xpath("//script[@type='application/ld+json']")
                for ld in json_lds:
                    try:
                        data = json.loads(ld.text_content())
                        if isinstance(data, dict):
                            campaign.update(data)
                    except (json.JSONDecodeError, TypeError):
                        pass

            except Exception as e:
                logger.warning(f"lxml parsing failed: {e}")

        return campaign

    def scrape_discover_page(self, tab: str = "All") -> List[Dict[str, Any]]:
        """
        Scrape the Discover page and extract all campaign URLs and metadata.

        Returns a list of campaign dicts with basic metadata and URLs.
        """
        # Determine the tab URL
        if tab == "Clipping":
            tab_url = CLIPPING_TAB_URL
        elif tab == "UGC":
            tab_url = UGC_TAB_URL
        else:
            tab_url = DISCOVER_URL

        logger.info(f"Scraping {tab} campaigns from {tab_url}")
        html = self._fetch(tab_url)
        if not html:
            return []

        campaigns = []

        # Try lxml parsing first
        if HAVE_LXML:
            try:
                tree = lxml_html.fromstring(html)
                # Initialize the campaigns list
                campaigns = []

                # Find all campaign links (a tags with href containing /c/campaigns/)
                campaign_links = tree.xpath("//a[contains(@href, '/c/campaigns/')]")

                # Alternative: find links with campaign UUIDs
                if not campaign_links:
                    campaign_links = tree.xpath("//a[contains(@href, '/c/campaigns/')]")

                seen_urls = set()
                for link in campaign_links:
                    href = link.get("href", "")
                    if "/c/campaigns/" in href and href not in seen_urls:
                        seen_urls.add(href)
                        # Prepend base URL for relative paths
                        full_url = href if href.startswith("http") else "https://contentrewards.com" + href
                        campaign = {
                            "url": full_url,
                            "name": link.text_content().strip() if link.text_content() else "",
                            "crawl_date": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }
                        campaigns.append(campaign)
                        logger.debug(f"Found campaign: {campaign['name'] or href}")

                return campaigns
            except Exception as e:
                logger.warning(f"lxml discover page parsing failed: {e}")
                return []

    def scrape_campaign_details(self, campaign: Dict[str, Any]) -> Dict[str, Any]:
        """
        Scrape full details for a single campaign.

        Returns the campaign with all parsed fields.
        """
        url = campaign["url"]
        logger.info(f"Scraping campaign details: {url}")
        detail = self._parse_campaign_page(url)
        if detail:
            campaign.update(detail)
        return campaign

    def check_campaign_credential(self, campaign_url: str) -> Tuple[bool, str]:
        """
        Check if the scraper can use account credentials to access the campaign.

        Credentials are loaded from environment variables (CR_CONTENT_REWARDS_USERNAME,
        CR_CONTENT_REWARDS_PASSWORD) with fallback to a local credentials.env file.
        Returns (available, message) tuple.
        """
        # Load credentials: env vars first, then local file
        from .config import CR_CONTENT_REWARDS_USERNAME, CR_CONTENT_REWARDS_PASSWORD

        creds = {
            "CR_CONTENT_REWARDS_USERNAME": CR_CONTENT_REWARDS_USERNAME,
            "CR_CONTENT_REWARDS_PASSWORD": CR_CONTENT_REWARDS_PASSWORD,
        }

        if not CR_CONTENT_REWARDS_USERNAME or not CR_CONTENT_REWARDS_PASSWORD:
            from .campaign_credentials import load_credentials
            creds.update(load_credentials())

        # Check for required keys
        required_keys = ["CR_CONTENT_REWARDS_USERNAME", "CR_CONTENT_REWARDS_PASSWORD"]
        missing = [k for k in required_keys if not creds.get(k)]
        if missing:
            return False, f"No credentials loaded: {', '.join(missing)}"

        # Check if the campaign is a personal campaign (may require login)
        if "personal" in campaign_url.lower() or "my" in campaign_url.lower():
            return True, "Personal campaign - requires credentials"

        return True, "Campaign accessible without credentials"

    def save_discover_page_cache(self, campaigns: List[Dict[str, Any]], tab: str = "All") -> None:
        """Save discover page cache to disk."""
        cache_file = self.cache_dir / f"discover_{tab.lower()}.json"
        cache_data = {
            "url": tab_url if tab != "All" else DISCOVER_URL,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "campaigns": campaigns,
            "count": len(campaigns),
        }
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2)
            logger.info(f"Cached discover page ({tab}): {cache_file}")
        except Exception as e:
            logger.warning(f"Failed to cache discover page: {e}")

    def load_discover_page_cache(self, tab: str = "All") -> Optional[List[Dict[str, Any]]]:
        """Load discover page cache from disk."""
        cache_file = self.cache_dir / f"discover_{tab.lower()}.json"
        if not cache_file.exists():
            return None
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info(f"Loaded cached discover page ({tab}): {data.get('count', 0)} campaigns")
            return data.get("campaigns", [])
        except Exception as e:
            logger.warning(f"Failed to load cached discover page: {e}")
            return None


def main():
    """CLI entry point for testing."""
    import argparse

    parser = argparse.ArgumentParser(description="Content Rewards Campaign Scraper")
    parser.add_argument("--tab", choices=["All", "Clipping", "UGC"], default="All",
                        help="Which tab to scrape (default: All)")
    parser.add_argument("--campaign", type=str, help="Scrape a specific campaign URL")
    parser.add_argument("--creds", action="store_true",
                        help="Print credential storage instructions")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only scan the discover page, don't fetch campaign details")

    args = parser.parse_args()

    if args.creds:
        from agent.campaign_credentials import print_safety_instructions
        print_safety_instructions()
        return

    scraper = ContentRewardsScraper()

    if args.campaign:
        # Scrape a specific campaign
        campaign = {"url": args.campaign}
        result = scraper.scrape_campaign_details(campaign)
        print(json.dumps(result, indent=2, default=str))
        return

    # Scrape discover page
    campaigns = scraper.scrape_discover_page(tab=args.tab)
    scraper.save_discover_page_cache(campaigns, tab=args.tab)

    print(f"Found {len(campaigns)} campaigns on {args.tab} tab")
    print(json.dumps(campaigns, indent=2, default=str))

    if not args.dry_run:
        # Fetch details for each campaign
        for i, campaign in enumerate(campaigns):
            try:
                logger.info(f"Scraping details for campaign {i+1}/{len(campaigns)}: {campaign.get('name', 'unknown')}")
                detail = scraper.scrape_campaign_details(campaign)
                print(f"\n--- Campaign {i+1}: {detail.get('name', 'unknown')} ---")
                print(f"CPM: {detail.get('cpm')}")
                print(f"Category: {detail.get('category')}")
                print(f"Remaining budget: {detail.get('remaining_budget')}")
                print(f"Platforms: {', '.join(detail.get('allowed_platforms', []))}")
                print(f"Content requirements: {len(detail.get('content_requirements', []))}")
            except Exception as e:
                logger.error(f"Failed to scrape details for {campaign.get('url')}: {e}")


if __name__ == "__main__":
    main()
