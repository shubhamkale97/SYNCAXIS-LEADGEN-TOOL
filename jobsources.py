"""Pluggable job-posting sources used to infer a company's industry from the
roles it advertises.

Every source implements the same ``search`` contract and returns a list of
normalised job dicts::

    {"title", "company", "location", "description", "url", "posted", "source"}

Sources are chosen by the ``JOB_SOURCES`` environment variable (comma
separated names) and by whether their credentials are present:

    ats        no key; reads employers' own Greenhouse/Lever/Ashby boards.
    linkedin   no key; scrapes LinkedIn's public guest endpoint.
    jobspy     no key, but needs the ``python-jobspy`` package installed.
    adzuna     needs ADZUNA_APP_ID + ADZUNA_APP_KEY (free tier).
    jooble     needs JOOBLE_API_KEY (free key on request).
    serpapi    needs SERPAPI_API_KEY; reads the Google Jobs panel.

Runtime packages are used when present and skipped cleanly when absent, so the
module still runs on a bare interpreter:

    playwright       headless Chromium (heaviest tier): renders the LinkedIn
                     guest endpoint in a real browser.
    curl_cffi        browser TLS/JA3 impersonation (lighter fallback).
    tenacity         retry with exponential backoff + jitter on 429/5xx.
    beautifulsoup4   robust HTML parsing (with lxml) for LinkedIn cards,
    lxml             falling back to a regex parser if bs4 is missing.

``search_jobs`` merges duplicate postings found on more than one source into a
single combined record (see ``dedupe_jobs``).
"""

import importlib.util
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

# --- optional runtime packages -------------------------------------------- #
try:
    from curl_cffi import requests as _curl_requests
except Exception:  # noqa: BLE001
    _curl_requests = None

try:
    from bs4 import BeautifulSoup

    _BS4_PARSER = "lxml" if importlib.util.find_spec("lxml") else "html.parser"
except Exception:  # noqa: BLE001
    BeautifulSoup = None
    _BS4_PARSER = None

try:
    from tenacity import (
        retry,
        retry_if_exception,
        stop_after_attempt,
        wait_exponential_jitter,
    )

    def _is_retryable(exc):
        """Retry transient failures only: 429 and 5xx, plus connection errors."""
        for attr in ("status_code", "code"):
            status = getattr(exc, attr, None)
            if isinstance(status, int):
                return status == 429 or status >= 500
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(status, int):
            return status == 429 or status >= 500
        return True

    def _retry(fn):
        return retry(
            stop=stop_after_attempt(int(os.environ.get("HTTP_ATTEMPTS", "3"))),
            wait=wait_exponential_jitter(initial=1, max=8),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )(fn)

except Exception:  # noqa: BLE001
    def _retry(fn):
        return fn

try:
    from playwright.sync_api import sync_playwright
except Exception:  # noqa: BLE001
    sync_playwright = None


@_retry
def _http(method, url, *, headers=None, data=None, timeout=DEFAULT_TIMEOUT,
          proxy=None, impersonate=False):
    """HTTP request with optional browser impersonation, proxy and retries.

    Uses curl_cffi (with real Chrome TLS/JA3 fingerprints) when available and
    falls back to the standard library otherwise.
    """
    if _curl_requests is not None:
        kwargs = {"headers": headers or {}, "timeout": timeout}
        if data is not None:
            kwargs["data"] = data
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}
        if impersonate:
            kwargs["impersonate"] = os.environ.get("IMPERSONATE_BROWSER", "chrome")
        response = _curl_requests.request(method, url, **kwargs)
        response.raise_for_status()
        return response.text

    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with opener.open(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _clean(text):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _env_proxy():
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(name)
        if value:
            return value
    return ""


def _playwright_proxy(proxy):
    """Convert a proxy string into Playwright's proxy dict."""
    if not proxy:
        return None
    value = proxy.split("://", 1)[1] if "://" in proxy else proxy
    if "@" in value:
        creds, host_port = value.rsplit("@", 1)
        username, _, password = creds.partition(":")
        return {
            "server": host_port if "://" in host_port else f"http://{host_port}",
            "username": username,
            "password": password,
        }
    return {"server": value if "://" in value else f"http://{value}"}


def _browser_fetch_many(urls, *, timeout_ms=None, wait_selector=None, proxy=None):
    """Fetch pages with headless Chromium (Playwright), reusing one browser.

    Returns one HTML string per URL; a failure yields an empty string so the
    caller can carry on with the remaining pages.
    """
    if sync_playwright is None:
        raise RuntimeError("playwright is not installed")
    timeout_ms = timeout_ms or int(os.environ.get("BROWSER_TIMEOUT_MS", "30000"))
    launch_kwargs = {
        "headless": os.environ.get("BROWSER_HEADLESS", "true").lower() != "false",
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    proxy_config = _playwright_proxy(proxy or _env_proxy())
    if proxy_config:
        launch_kwargs["proxy"] = proxy_config
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent=_CHROME_UA, locale="en-US",
            viewport={"width": 1366, "height": 900},
        )
        for url in urls:
            page = context.new_page()
            try:
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                if wait_selector:
                    try:
                        page.wait_for_selector(wait_selector, timeout=timeout_ms)
                    except Exception:  # noqa: BLE001 - selector may never appear
                        pass
                results.append(page.content())
            except Exception:  # noqa: BLE001
                results.append("")
            finally:
                page.close()
        context.close()
        browser.close()
    return results


# --------------------------------------------------------------------------- #
# LinkedIn public guest endpoint
# --------------------------------------------------------------------------- #

def _parse_linkedin_cards_regex(html):
    """Regex fallback parser for LinkedIn guest job cards."""
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


def _parse_linkedin_cards_soup(html):
    """BeautifulSoup parser for LinkedIn guest job cards."""
    soup = BeautifulSoup(html, _BS4_PARSER)
    jobs = []
    for card in soup.select("li"):
        title_el = card.select_one(".base-search-card__title")
        if title_el is None:
            continue
        title = _clean(title_el.get_text())
        if not title:
            continue
        company_el = card.select_one(".base-search-card__subtitle")
        location_el = card.select_one(".job-search-card__location")
        link_el = card.select_one("a.base-card__full-link")
        date_el = card.select_one(".job-search-card__listdate")
        jobs.append(
            {
                "title": title,
                "company": _clean(company_el.get_text()) if company_el else "",
                "location": _clean(location_el.get_text()) if location_el else "",
                "description": "",
                "url": link_el.get("href", "") if link_el else "",
                "posted": _clean(date_el.get_text()) if date_el else "",
                "source": "linkedin",
            }
        )
    return jobs


def parse_linkedin_cards(html):
    """Parse LinkedIn guest job cards, preferring BeautifulSoup over regex."""
    if BeautifulSoup is not None:
        try:
            jobs = _parse_linkedin_cards_soup(html)
            if jobs:
                return jobs
        except Exception:  # noqa: BLE001 - fall back to regex
            pass
    return _parse_linkedin_cards_regex(html)


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
        # auto -> playwright if installed, else curl_cffi.
        self.engine = os.environ.get("LINKEDIN_ENGINE", "auto").strip().lower()

    def _engine(self):
        if self.engine in ("playwright", "browser"):
            return "playwright" if sync_playwright is not None else "curl_cffi"
        if self.engine in ("curl_cffi", "curl", "http"):
            return "curl_cffi"
        return "playwright" if sync_playwright is not None else "curl_cffi"

    def limits(self):
        return {
            "kind": "scrape",
            "engine": self._engine(),
            "playwright_available": sync_playwright is not None,
            "results_per_query_cap": 1000,
            "page_size": 25,
            "max_pages_per_query": 40,
            "recommended_delay_seconds": self.min_interval,
            "recommended_pages_per_minute": 12,
            "safe_requests_per_day_per_ip": (
                "~20-30 conservative; ~500/hour with rotating residential proxies"
            ),
            "tls_impersonation": _curl_requests is not None,
            "note": (
                "Scraping LinkedIn's guest endpoint is against its User "
                "Agreement and can get an IP blocked. Prefer an API source."
            ),
        }

    def _search_browser(self, urls, limit):
        jobs = []
        htmls = _browser_fetch_many(urls, wait_selector="li", proxy=self.proxy or None)
        for html in htmls:
            page_jobs = parse_linkedin_cards(html) if html else []
            if not page_jobs:
                break
            jobs.extend(page_jobs)
            if limit and len(jobs) >= limit:
                break
        return jobs

    def _search_http(self, urls, limit):
        headers = {
            "User-Agent": _CHROME_UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.linkedin.com/jobs/search/",
        }
        jobs = []
        for index, url in enumerate(urls):
            html = _http("GET", url, headers=headers, proxy=self.proxy, impersonate=True)
            page_jobs = parse_linkedin_cards(html)
            if not page_jobs:
                break
            jobs.extend(page_jobs)
            if limit and len(jobs) >= limit:
                break
            if index < len(urls) - 1:
                time.sleep(self.min_interval)
        return jobs

    def search(self, keyword, location, pages=1, limit=None):
        pages = max(1, min(int(pages), self.max_pages))
        urls = [
            f"{self.endpoint}?" + urllib.parse.urlencode(
                {"keywords": keyword, "location": location, "start": page * self.page_size}
            )
            for page in range(pages)
        ]
        if self._engine() == "playwright":
            try:
                jobs = self._search_browser(urls, limit)
                if jobs:
                    return jobs[:limit] if limit else jobs
            except Exception:  # noqa: BLE001 - browser unavailable; fall back to HTTP
                pass
        jobs = self._search_http(urls, limit)
        return jobs[:limit] if limit else jobs


# --------------------------------------------------------------------------- #
# JobSpy (optional multi-board scraper library)
# --------------------------------------------------------------------------- #

def jobspy_rows_to_jobs(records, limit=None):
    """Map JobSpy output (a DataFrame or a list of dicts) to our job dicts."""
    if hasattr(records, "to_dict"):  # pandas DataFrame
        try:
            rows = records.to_dict("records")
        except TypeError:
            rows = records.to_dict(orient="records")
    else:
        rows = list(records or [])

    def value(row, key):
        item = row.get(key)
        if item is None:
            return ""
        if isinstance(item, float) and item != item:  # NaN
            return ""
        return str(item)

    jobs = []
    for row in rows:
        site = value(row, "site")
        jobs.append(
            {
                "title": value(row, "title"),
                "company": value(row, "company"),
                "location": value(row, "location"),
                "description": value(row, "description"),
                "url": value(row, "job_url_direct") or value(row, "job_url"),
                "posted": value(row, "date_posted"),
                "source": f"jobspy:{site}" if site else "jobspy",
                "company_industry": value(row, "company_industry"),
                "skills": value(row, "skills"),
            }
        )
    return jobs[:limit] if limit else jobs


class JobSpySource(JobSource):
    name = "jobspy"
    kind = "scrape"
    risky = True

    def __init__(self):
        self.sites = [
            s.strip() for s in os.environ.get(
                "JOBSPY_SITES", "linkedin,indeed,glassdoor,google,naukri"
            ).split(",") if s.strip()
        ]
        self.country = os.environ.get("JOBSPY_COUNTRY", "india").strip() or "india"

    def configured(self):
        return importlib.util.find_spec("jobspy") is not None

    def limits(self):
        return {
            "kind": "scrape",
            "boards": self.sites,
            "country_indeed": self.country,
            "requires": "pip install python-jobspy",
            "note": (
                "Wraps the same boards as direct scraping (LinkedIn/Indeed/"
                "Glassdoor/Google/Naukri); the same terms-of-service caveats apply."
            ),
        }

    def search(self, keyword, location, pages=1, limit=None):
        from jobspy import scrape_jobs  # imported lazily so the module stays optional

        wanted = limit or 40
        frame = scrape_jobs(
            site_name=self.sites,
            search_term=keyword,
            location=location or None,
            results_wanted=wanted,
            country_indeed=self.country,
            fetch_description=False,
            verbose=0,
        )
        return jobspy_rows_to_jobs(frame, limit)


# --------------------------------------------------------------------------- #
# ATS boards (keyless, ToS-safe: employers' own Greenhouse / Lever / Ashby)
# --------------------------------------------------------------------------- #

def candidate_slugs(name):
    """Turn a company name into likely ATS board slugs."""
    base = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    if not base:
        return []
    compact = base.replace(" ", "")
    hyphenated = base.replace(" ", "-")
    return list(dict.fromkeys([compact, hyphenated]))


def _fetch_ats(provider, slug):
    """Fetch one employer's open roles from a keyless public ATS board."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    jobs = []
    if provider == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        data = json.loads(_http("GET", url, headers=headers, timeout=12))
        for item in data.get("jobs", []):
            jobs.append({
                "title": item.get("title", ""),
                "company": item.get("company_name") or slug,
                "location": (item.get("location") or {}).get("name", ""),
                "description": item.get("content", "") or "",
                "url": item.get("absolute_url", ""),
                "posted": item.get("updated_at", "") or item.get("first_published", ""),
                "source": "ats:greenhouse",
            })
    elif provider == "lever":
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        data = json.loads(_http("GET", url, headers=headers, timeout=12))
        for item in data:
            categories = item.get("categories") or {}
            jobs.append({
                "title": item.get("text", ""),
                "company": slug,
                "location": categories.get("location", "") or "",
                "description": item.get("descriptionPlain", "") or "",
                "url": item.get("hostedUrl", ""),
                "posted": item.get("createdAt", "") or "",
                "source": "ats:lever",
            })
    elif provider == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        data = json.loads(_http("GET", url, headers=headers, timeout=12))
        for item in data.get("jobs", []):
            jobs.append({
                "title": item.get("title", ""),
                "company": item.get("companyName") or slug,
                "location": item.get("location", "") or "",
                "description": item.get("descriptionPlain", "") or "",
                "url": item.get("jobUrl", ""),
                "posted": item.get("publishedAt", "") or "",
                "source": "ats:ashby",
            })
    return jobs


class ATSSource(JobSource):
    name = "ats"
    kind = "api"
    risky = False

    def __init__(self):
        self.providers = [
            p.strip().lower() for p in os.environ.get(
                "ATS_PROVIDERS", "greenhouse,lever,ashby"
            ).split(",") if p.strip()
        ]

    def configured(self):
        return True  # keyless public boards

    def limits(self):
        return {
            "kind": "api",
            "providers": self.providers,
            "note": (
                "Keyless public ATS boards (employers' own careers pages). No key, "
                "no scraping, no terms-of-service issue - the most legitimate source."
            ),
        }

    def search(self, keyword, location, pages=1, limit=None):
        slugs = candidate_slugs(keyword)
        jobs = []
        for provider in self.providers:
            for slug in slugs:
                try:
                    jobs.extend(_fetch_ats(provider, slug))
                except Exception:  # noqa: BLE001 - a miss just means no board here
                    continue
                if limit and len(jobs) >= limit:
                    break
            if limit and len(jobs) >= limit:
                break
        if location:
            wanted = location.lower()
            filtered = [
                job for job in jobs
                if not job.get("location") or wanted in job["location"].lower()
            ]
            jobs = filtered or jobs
        seen, unique = set(), []
        for job in jobs:
            key = (job["title"].lower(), job["company"].lower(), job["url"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(job)
        return unique[:limit] if limit else unique


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


ALL_SOURCE_CLASSES = [
    ATSSource,
    LinkedInGuestSource,
    JobSpySource,
    AdzunaSource,
    JoobleSource,
    SerpApiSource,
]


# --------------------------------------------------------------------------- #
# De-duplication: merge the same posting seen on more than one source
# --------------------------------------------------------------------------- #

def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def dedupe_jobs(jobs):
    """Merge duplicate postings across sources into one combined record.

    Two postings are treated as the same job when their normalised title and
    company match (falling back to the URL when both are empty). A merged
    record keeps the longest description, the first non-empty value for the
    other fields, and lists every source it was seen on under ``sources``.
    """
    merged = {}
    order = []
    for job in jobs:
        key = (_norm(job.get("title")), _norm(job.get("company")))
        if key == ("", ""):
            key = ("url", job.get("url", ""))
        if key not in merged:
            record = dict(job)
            record["sources"] = [job["source"]] if job.get("source") else []
            merged[key] = record
            order.append(key)
            continue
        record = merged[key]
        source = job.get("source")
        if source and source not in record["sources"]:
            record["sources"].append(source)
        if len(job.get("description") or "") > len(record.get("description") or ""):
            record["description"] = job["description"]
        for field in ("location", "url", "posted", "company_industry", "skills"):
            if not record.get(field) and job.get(field):
                record[field] = job[field]
    return [merged[key] for key in order]


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

    Results from all sources are merged and de-duplicated, so a posting that
    appears on more than one board is returned once with a combined record. A
    failing source is reported but never aborts the others.
    """
    raw = []
    report = []
    for source in selected_sources(sources):
        try:
            found = source.search(keyword, location, pages=pages, limit=limit_per_source)
        except Exception as error:  # noqa: BLE001 - surface any source failure
            report.append({"source": source.name, "error": f"{type(error).__name__}: {error}"})
            continue
        raw.extend(found)
        report.append({"source": source.name, "count": len(found), "risky": source.risky})
    jobs = dedupe_jobs(raw)
    report.append(
        {
            "summary": {
                "raw_total": len(raw),
                "unique": len(jobs),
                "merged": len(raw) - len(jobs),
            }
        }
    )
    return jobs, report
