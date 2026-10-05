"""Pluggable job-posting sources used to infer a company's industry from the
roles it advertises.

Every source implements the same ``search`` contract and returns a list of
normalised job dicts::

    {"title", "company", "location", "description", "url", "posted", "source"}

Sources are chosen by the ``JOB_SOURCES`` environment variable (comma
separated names) and by whether their credentials are present:

    linkedin   no key; scrapes LinkedIn's public guest endpoint.  Rate limited
               and carries a terms-of-service risk - see ``limits()``.
    adzuna     needs ADZUNA_APP_ID + ADZUNA_APP_KEY (free tier).
    jooble     needs JOOBLE_API_KEY (free key on request).
    serpapi    needs SERPAPI_API_KEY; reads the Google Jobs panel.

The module is dependency-free on purpose, matching app.py's stdlib-only style.
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request
from html import unescape

USER_AGENT = "syncaxis-leadgen/1.0 (+local development app)"
DEFAULT_TIMEOUT = 25
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _http(method, url, *, headers=None, data=None, timeout=DEFAULT_TIMEOUT, proxy=None):
    """Minimal HTTP helper with optional per-request proxy support."""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with opener.open(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _clean(text):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


# --------------------------------------------------------------------------- #
# LinkedIn public guest endpoint
# --------------------------------------------------------------------------- #

def parse_linkedin_cards(html):
    """Parse the job cards returned by LinkedIn's guest search endpoint.

    The markup is not an official contract, so this is defensive: any field it
    cannot read is left blank rather than raising, and a parse miss simply
    yields fewer jobs.
    """
    jobs = []
    for block in re.findall(r"<li\b.*?</li>", html, flags=re.S | re.I):
        url_match = re.search(r'base-card__full-link[^>]*href="([^"]+)"', block, re.I)
        title_match = re.search(r'base-search-card__title[^>]*>(.*?)</h3>', block, re.S | re.I)
        company_match = re.search(
            r'base-search-card__subtitle[^>]*>(.*?)</h4>', block, re.S | re.I
        )
        location_match = re.search(
            r'job-search-card__location[^>]*>(.*?)</span>', block, re.S | re.I
        )
        posted_match = re.search(
            r'job-search-card__listdate[^>]*>(.*?)</time>', block, re.S | re.I
        )
        title = _clean(title_match.group(1)) if title_match else ""
        if not title:
            continue
        jobs.append(
            {
                "title": title,
                "company": _clean(company_match.group(1)) if company_match else "",
                "location": _clean(location_match.group(1)) if location_match else "",
                "description": "",
                "url": unescape(url_match.group(1)) if url_match else "",
                "posted": _clean(posted_match.group(1)) if posted_match else "",
                "source": "linkedin",
            }
        )
    return jobs


class JobSource:
    """Base class: one searchable source of job postings."""

    name = "base"
    kind = "api"          # "api" | "scrape"
    risky = False         # True when use may breach the site's terms
    page_size = 25
    min_interval = 1.0    # seconds to wait between requests to this source

    def configured(self):
        """Whether this source has what it needs to run (usually credentials)."""
        return True

    def limits(self):
        """Documented / recommended request limits, surfaced via the API."""
        return {}

    def search(self, keyword, location, pages=1, limit=None):
        raise NotImplementedError


class LinkedInGuestSource(JobSource):
    name = "linkedin"
    kind = "scrape"
    risky = True
    page_size = 25
    endpoint = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

    def __init__(self):
        self.min_interval = float(os.environ.get("LINKEDIN_DELAY", "3.5"))
        self.max_pages = max(1, int(os.environ.get("LINKEDIN_PAGES", "3")))
        self.proxy = os.environ.get("LINKEDIN_PROXY", "").strip()

    def limits(self):
        return {
            "kind": "scrape",
            "results_per_query_cap": 1000,
            "page_size": 25,
            "max_pages_per_query": 40,
            "recommended_delay_seconds": self.min_interval,
            "recommended_pages_per_minute": 12,
            "safe_requests_per_day_per_ip": (
                "~20-30 conservative; ~500/hour with rotating residential proxies"
            ),
            "note": (
                "Scraping LinkedIn's guest endpoint is against its User "
                "Agreement and can get an IP blocked. Prefer an API source."
            ),
        }

    def search(self, keyword, location, pages=1, limit=None):
        pages = max(1, min(int(pages), self.max_pages))
        headers = {
            "User-Agent": _CHROME_UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.linkedin.com/jobs/search/",
        }
        jobs = []
        for page in range(pages):
            params = urllib.parse.urlencode(
                {"keywords": keyword, "location": location, "start": page * self.page_size}
            )
            html = _http(
                "GET", f"{self.endpoint}?{params}", headers=headers, proxy=self.proxy
            )
            page_jobs = parse_linkedin_cards(html)
            if not page_jobs:
                break
            jobs.extend(page_jobs)
            if limit and len(jobs) >= limit:
                break
            if page < pages - 1:
                time.sleep(self.min_interval)
        return jobs[:limit] if limit else jobs


# --------------------------------------------------------------------------- #
# Adzuna (free tier, broad multi-country coverage incl. India)
# --------------------------------------------------------------------------- #

class AdzunaSource(JobSource):
    name = "adzuna"
    page_size = 50

    def __init__(self):
        self.app_id = os.environ.get("ADZUNA_APP_ID", "").strip()
        self.app_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
        self.country = os.environ.get("ADZUNA_COUNTRY", "in").strip() or "in"

    def configured(self):
        return bool(self.app_id and self.app_key)

    def limits(self):
        return {
            "kind": "api",
            "calls_per_minute": 25,
            "calls_per_day": 250,
            "calls_per_month": 2500,
            "page_size": self.page_size,
            "country": self.country,
            "note": "Free tier; commercial use needs a licence after the trial.",
        }

    def search(self, keyword, location, pages=1, limit=None):
        jobs = []
        for page in range(1, max(1, int(pages)) + 1):
            params = urllib.parse.urlencode(
                {
                    "app_id": self.app_id,
                    "app_key": self.app_key,
                    "what": keyword,
                    "where": location,
                    "results_per_page": self.page_size,
                    "content-type": "application/json",
                }
            )
            url = f"https://api.adzuna.com/v1/api/jobs/{self.country}/search/{page}?{params}"
            payload = json.loads(_http("GET", url, headers={"User-Agent": USER_AGENT}))
            results = payload.get("results", [])
            for item in results:
                jobs.append(
                    {
                        "title": item.get("title", ""),
                        "company": (item.get("company") or {}).get("display_name", ""),
                        "location": (item.get("location") or {}).get("display_name", ""),
                        "description": item.get("description", ""),
                        "url": item.get("redirect_url", ""),
                        "posted": item.get("created", ""),
                        "source": "adzuna",
                    }
                )
            if len(results) < self.page_size or (limit and len(jobs) >= limit):
                break
            time.sleep(0.4)
        return jobs[:limit] if limit else jobs


# --------------------------------------------------------------------------- #
# Jooble (free key, 60+ countries from one endpoint)
# --------------------------------------------------------------------------- #

class JoobleSource(JobSource):
    name = "jooble"

    def __init__(self):
        self.key = os.environ.get("JOOBLE_API_KEY", "").strip()

    def configured(self):
        return bool(self.key)

    def limits(self):
        return {
            "kind": "api",
            "note": "Per-key limits are set by Jooble and not published publicly.",
            "coverage": "60+ countries from a single endpoint",
        }

    def search(self, keyword, location, pages=1, limit=None):
        body = json.dumps({"keywords": keyword, "location": location}).encode()
        raw = _http(
            "POST",
            f"https://jooble.org/api/{self.key}",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            data=body,
        )
        payload = json.loads(raw)
        jobs = []
        for item in payload.get("jobs", []):
            jobs.append(
                {
                    "title": item.get("title", ""),
                    "company": item.get("company", ""),
                    "location": item.get("location", ""),
                    "description": item.get("snippet", ""),
                    "url": item.get("link", ""),
                    "posted": item.get("updated", ""),
                    "source": "jooble",
                }
            )
        return jobs[:limit] if limit else jobs


# --------------------------------------------------------------------------- #
# SerpAPI Google Jobs (aggregates LinkedIn/Indeed listings without scraping)
# --------------------------------------------------------------------------- #

class SerpApiSource(JobSource):
    name = "serpapi"
    page_size = 10

    def __init__(self):
        self.key = os.environ.get("SERPAPI_API_KEY", "").strip()

    def configured(self):
        return bool(self.key)

    def limits(self):
        return {
            "kind": "api",
            "searches_per_month_free": 250,
            "results_per_page": 10,
            "note": "Aggregates Google Jobs (LinkedIn/Indeed listings) without scraping.",
        }

    def search(self, keyword, location, pages=1, limit=None):
        jobs = []
        token = None
        for _ in range(max(1, int(pages))):
            params = {"engine": "google_jobs", "q": keyword, "api_key": self.key}
            if location:
                params["location"] = location
            if token:
                params["next_page_token"] = token
            payload = json.loads(
                _http("GET", "https://serpapi.com/search.json?" + urllib.parse.urlencode(params))
            )
            for item in payload.get("jobs_results", []):
                apply_options = item.get("apply_options") or []
                jobs.append(
                    {
                        "title": item.get("title", ""),
                        "company": item.get("company_name", ""),
                        "location": item.get("location", ""),
                        "description": item.get("description", ""),
                        "url": apply_options[0].get("link", "") if apply_options else item.get("share_link", ""),
                        "posted": (item.get("detected_extensions") or {}).get("posted_at", ""),
                        "source": "serpapi",
                    }
                )
            token = (payload.get("serpapi_pagination") or {}).get("next_page_token")
            if not token or (limit and len(jobs) >= limit):
                break
            time.sleep(0.5)
        return jobs[:limit] if limit else jobs


ALL_SOURCE_CLASSES = [LinkedInGuestSource, AdzunaSource, JoobleSource, SerpApiSource]


def _registry():
    return {cls.name: cls for cls in ALL_SOURCE_CLASSES}


def available_sources():
    """Describe every known source and whether it is ready to use."""
    described = []
    for cls in ALL_SOURCE_CLASSES:
        source = cls()
        described.append(
            {
                "name": source.name,
                "configured": source.configured(),
                "kind": source.kind,
                "risky": source.risky,
                "limits": source.limits(),
            }
        )
    return described


def selected_sources(names=None):
    """Instantiate the requested (or env-selected) sources that are configured."""
    registry = _registry()
    if names:
        wanted = [str(n).strip().lower() for n in names]
    else:
        env = os.environ.get("JOB_SOURCES", "").strip()
        wanted = [n.strip().lower() for n in env.split(",") if n.strip()] if env else list(registry)
    chosen = []
    for name in wanted:
        cls = registry.get(name)
        if cls is None:
            continue
        source = cls()
        if source.configured():
            chosen.append(source)
    return chosen


def search_jobs(keyword, location, pages=1, limit_per_source=40, sources=None):
    """Search every selected source and return (jobs, per_source_report).

    A failing source is reported but never aborts the others.
    """
    jobs = []
    report = []
    for source in selected_sources(sources):
        try:
            found = source.search(keyword, location, pages=pages, limit=limit_per_source)
        except Exception as error:  # noqa: BLE001 - surface any source failure
            report.append({"source": source.name, "error": f"{type(error).__name__}: {error}"})
            continue
        jobs.extend(found)
        report.append({"source": source.name, "count": len(found), "risky": source.risky})
    return jobs, report
