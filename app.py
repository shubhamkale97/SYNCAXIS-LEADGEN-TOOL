#!/usr/bin/env python3
"""Small web gateway for the local Google Maps scraper.

Serves the UI from ``web/`` and exposes a validated JSON API in front of the
scraper container, the job-posting sources and the geocoders.

Hardening in this module (all configurable through the environment):

* binds to ``BIND_HOST`` (default 127.0.0.1) rather than every interface;
* request bodies are capped at ``MAX_BODY_BYTES``;
* optional HTTP Basic authentication (``AUTH_USER`` + ``AUTH_PASSWORD``);
* per-client rate limit on expensive endpoints (``RATE_LIMIT_PER_MIN``);
* upstream and internal error details are logged, not returned to clients.
"""

import base64
import hmac
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from genre import infer_genre, match_field
from jobsources import available_sources, search_jobs
from network_year import current_year
from textutil import company_matches

log = logging.getLogger("leadgen.app")

ROOT = Path(__file__).parent / "web"
SCRAPER_URL = os.environ.get("SCRAPER_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
PORT = int(os.environ.get("PORT", "3000"))
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
# Nominatim's usage policy asks for an identifying User-Agent with a contact.
NOMINATIM_CONTACT = os.environ.get("NOMINATIM_CONTACT", "").strip()
USER_AGENT = "syncaxis-leadgen/1.0" + (f" ({NOMINATIM_CONTACT})" if NOMINATIM_CONTACT else "")
# Server-side key (Geocoding API). The map key sent to browsers can be a
# separate, referrer-restricted key; it falls back to the server key.
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
GOOGLE_MAPS_BROWSER_KEY = os.environ.get("GOOGLE_MAPS_BROWSER_KEY", "") or GOOGLE_MAPS_API_KEY
HERE_API_KEY = os.environ.get("HERE_API_KEY", "")

MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", "65536"))
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "60"))  # 0 disables
AUTH_USER = os.environ.get("AUTH_USER", "")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")

JOBS_PATHS = {"/api/company-genre", "/api/job-search", "/api/company-match"}
SEARCH_PATHS = {"/api/jobs", "/api/alternative-search"}
# /api/jobs/<id> and /api/jobs/<id>/download only; ids are UUID-like.
JOB_PATH = re.compile(r"^/api/jobs/([A-Za-z0-9-]{1,64})(/download)?$")
STOP_WORDS = {"a", "an", "and", "business", "businesses", "company", "companies", "in", "near", "the"}


class RateLimiter:
    """Sliding-window limiter keyed by client address."""

    def __init__(self):
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key, limit, window=60.0, now=None):
        """Record a hit; return ``(allowed, retry_after_seconds)``."""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= window:
                hits.popleft()
            if len(hits) >= limit:
                return False, max(1, int(window - (now - hits[0])) + 1)
            hits.append(now)
            if len(self._hits) > 4096:  # forget idle clients
                for stale in [k for k, v in self._hits.items() if not v or now - v[-1] >= window]:
                    del self._hits[stale]
            return True, 0

    def reset(self):
        with self._lock:
            self._hits.clear()


RATE_LIMITER = RateLimiter()

# Nominatim allows at most one request per second and asks clients to cache.
_nominatim_lock = threading.Lock()
_nominatim_last = 0.0
_geocode_cache = {}
_GEOCODE_TTL = 24 * 3600
_GEOCODE_CACHE_MAX = 512


def nominatim_get(url, timeout):
    """GET a Nominatim URL, at most once per second across all threads."""
    global _nominatim_last
    with _nominatim_lock:
        wait = _nominatim_last + 1.1 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _nominatim_last = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def geocode(query):
    """Resolve a typed location. Returns ``(status, payload)``."""
    cache_key = query.lower()
    cached = _geocode_cache.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return 200, cached[1]
    try:
        if GOOGLE_MAPS_API_KEY:
            url = "https://maps.googleapis.com/maps/api/geocode/json?" + urllib.parse.urlencode({
                "address": query, "key": GOOGLE_MAPS_API_KEY,
            })
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read())
            if payload.get("status") != "OK" or not payload.get("results"):
                log.warning("Google geocoding failed: %s %s", payload.get("status"), payload.get("error_message", ""))
                return 502, {"error": "Google geocoding failed"}
            result = payload["results"][0]
            point = result["geometry"]["location"]
            found = {
                "lat": str(point["lat"]), "lon": str(point["lng"]),
                "label": result["formatted_address"], "provider": "google",
            }
        else:
            url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
                "q": query, "format": "jsonv2", "limit": 1,
            })
            places = nominatim_get(url, timeout=20)
            if not places:
                return 404, {"error": "Location not found"}
            hit = places[0]
            found = {
                "lat": hit["lat"], "lon": hit["lon"], "label": hit["display_name"],
                "provider": "nominatim",
            }
    except Exception as error:  # noqa: BLE001 - report a generic failure to the client
        log.warning("Geocoding failed: %s: %s", type(error).__name__, error)
        return 502, {"error": "Geocoding failed"}
    if len(_geocode_cache) >= _GEOCODE_CACHE_MAX:
        _geocode_cache.pop(next(iter(_geocode_cache)), None)
    _geocode_cache[cache_key] = (time.monotonic() + _GEOCODE_TTL, found)
    return 200, found


class Handler(SimpleHTTPRequestHandler):
    timeout = 30  # seconds a client may stall mid-request before the socket is dropped
    server_version = "SyncaxisLeadgen"
    sys_version = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    # -- plumbing ---------------------------------------------------------- #

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.client_address[0], fmt % args)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def list_directory(self, path):
        self.send_error(404, "Not found")
        return None

    def json_response(self, status, payload, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    # -- access control ----------------------------------------------------- #

    def authorized(self):
        """True when no credentials are configured or the request carries them."""
        if not (AUTH_USER and AUTH_PASSWORD):
            return True
        header = self.headers.get("Authorization", "")
        if header.lower().startswith("basic "):
            try:
                supplied = base64.b64decode(header[6:].strip(), validate=True)
            except (ValueError, TypeError):
                supplied = b""
            expected = f"{AUTH_USER}:{AUTH_PASSWORD}".encode()
            if hmac.compare_digest(supplied, expected):
                return True
        self.json_response(
            401, {"error": "Authentication required"},
            headers={"WWW-Authenticate": 'Basic realm="Syncaxis Leadgen", charset="UTF-8"'},
        )
        return False

    def within_rate_limit(self):
        if RATE_LIMIT_PER_MIN <= 0:
            return True
        allowed, retry_after = RATE_LIMITER.check(self.client_address[0], RATE_LIMIT_PER_MIN)
        if not allowed:
            self.json_response(
                429, {"error": "Too many requests - please slow down"},
                headers={"Retry-After": str(retry_after)},
            )
        return allowed

    def read_json(self):
        """Parse the request body as a JSON object.

        Returns the dict, or None after having sent the error response.
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0:
            self.json_response(400, {"error": "Invalid Content-Length"})
            return None
        if length > MAX_BODY_BYTES:
            self.close_connection = True  # the oversized body is left unread
            self.json_response(413, {"error": "Request body too large"})
            return None
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            self.json_response(400, {"error": "Request body must be valid JSON"})
            return None
        if not isinstance(data, dict):
            self.json_response(400, {"error": "Request body must be a JSON object"})
            return None
        return data

    # -- scraper proxy ------------------------------------------------------- #

    def proxy(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{SCRAPER_URL}{path}", data=data, method=method,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=65) as response:
                payload = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                if path.endswith("/download"):
                    job_id = path.rstrip("/").split("/")[-2]
                    self.send_header("Content-Disposition", f'attachment; filename="syncaxis-leads-{job_id[:12]}.csv"')
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            log.warning("Scraper returned HTTP %s for %s %s: %s", error.code, method, path, detail)
            if 400 <= error.code < 500:
                # Client errors from the scraper are actionable (bad input); keep a short note.
                self.json_response(error.code, {"error": detail.strip()[:200] or f"Scraper rejected the request ({error.code})"})
            else:
                self.json_response(502, {"error": "Scraper service returned an error"})
        except (urllib.error.URLError, TimeoutError) as error:
            log.warning("Scraper unavailable for %s %s: %s", method, path, getattr(error, "reason", error))
            self.json_response(502, {"error": "Scraper service is unavailable"})

    # -- routes -------------------------------------------------------------- #

    def do_HEAD(self):
        if self.authorized():
            super().do_HEAD()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/healthz":  # liveness only; no auth, no upstream calls
            return self.json_response(200, {"status": "ok"})
        if not self.authorized():
            return None
        if parsed.path == "/api/time":
            return self.json_response(200, current_year())
        if parsed.path == "/api/health":
            return self.proxy("GET", "/api/v1/jobs")
        if parsed.path == "/api/geocode":
            if not self.within_rate_limit():
                return None
            query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0].strip()[:200]
            if not query:
                return self.json_response(400, {"error": "Location is required"})
            status, payload = geocode(query)
            return self.json_response(status, payload)
        job_match = JOB_PATH.match(parsed.path)
        if job_match:
            suffix = job_match.group(1) + (job_match.group(2) or "")
            return self.proxy("GET", f"/api/v1/jobs/{suffix}")
        if parsed.path.startswith("/api/jobs/"):
            return self.json_response(400, {"error": "Invalid job id"})
        if parsed.path == "/api/jobs":
            return self.proxy("GET", "/api/v1/jobs")
        if parsed.path == "/api/job-sources":
            return self.json_response(200, {"sources": available_sources()})
        if parsed.path == "/api/map-config":
            # MAP_PROVIDER = auto | google | here | osm. "auto" prefers Google,
            # then HERE, then keyless OpenStreetMap. A provider whose key is
            # missing falls back to OpenStreetMap. The key is sent to the browser
            # so the map library can load - restrict it by HTTP referrer / allowed
            # domain in the Google or HERE console. Use GOOGLE_MAPS_BROWSER_KEY
            # for a separate browser key so the server-side key never leaves here.
            requested = os.environ.get("MAP_PROVIDER", "auto").strip().lower()
            provider, key = "osm", ""
            if requested == "google" and GOOGLE_MAPS_BROWSER_KEY:
                provider, key = "google", GOOGLE_MAPS_BROWSER_KEY
            elif requested == "here" and HERE_API_KEY:
                provider, key = "here", HERE_API_KEY
            elif requested not in ("google", "here", "osm"):
                if GOOGLE_MAPS_BROWSER_KEY:
                    provider, key = "google", GOOGLE_MAPS_BROWSER_KEY
                elif HERE_API_KEY:
                    provider, key = "here", HERE_API_KEY
            return self.json_response(200, {"provider": provider, "key": key})
        return super().do_GET()

    def do_POST(self):
        if not self.authorized():
            return None
        path = urllib.parse.urlparse(self.path).path
        if path not in JOBS_PATHS and path not in SEARCH_PATHS:
            return self.json_response(404, {"error": "Not found"})
        if not self.within_rate_limit():
            return None
        incoming = self.read_json()
        if incoming is None:
            return None
        if path in JOBS_PATHS:
            return self.jobs_endpoint(path, incoming)
        try:
            keyword = str(incoming.get("keyword", "")).strip()[:200]
            lat = str(incoming.get("lat", "")).strip()
            lon = str(incoming.get("lon", "")).strip()
            depth = max(1, min(int(incoming.get("depth", 5)), 20))
            radius_km = float(incoming.get("radius_km", 10))
            if not keyword or not lat or not lon:
                raise ValueError("keyword, lat and lon are required")
            latitude = float(lat)
            longitude = float(lon)
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError("coordinates are outside the valid range")
            if not 1 <= radius_km <= 50:
                raise ValueError("radius must be between 1 and 50 km")
        except (ValueError, TypeError) as error:
            return self.json_response(400, {"error": str(error)})
        if path == "/api/alternative-search":
            return self.alternative_search(keyword, latitude, longitude, radius_km)
        location_name = str(incoming.get("location", "")).strip()[:200]
        search_term = f"{keyword} in {location_name}" if location_name else f"{keyword} near {latitude},{longitude}"
        payload = {
            "name": "maps-lead-finder", "keywords": [search_term], "lang": "en",
            "zoom": 15, "lat": str(latitude), "lon": str(longitude), "radius": int(radius_km * 1000),
            "depth": depth, "email": bool(incoming.get("email", False)),
            "fast_mode": False, "max_time": 600,
        }
        return self.proxy("POST", "/api/v1/jobs", payload)

    def jobs_endpoint(self, path, incoming):
        """Search job postings and infer a company's industry from them."""
        company = str(incoming.get("company", "")).strip()[:120]
        keyword = str(incoming.get("keyword", "")).strip()[:120]
        city = str(incoming.get("city", "") or incoming.get("location", "")).strip()[:120]
        sources = incoming.get("sources")
        if sources is not None and (
            not isinstance(sources, list) or len(sources) > 10
            or not all(isinstance(name, str) for name in sources)
        ):
            return self.json_response(400, {"error": "sources must be a list of source names"})
        try:
            pages = max(1, min(int(incoming.get("pages", 2)), 5))
            max_jobs = max(5, min(int(incoming.get("max_jobs", 40)), 200))
        except (ValueError, TypeError):
            return self.json_response(400, {"error": "pages and max_jobs must be integers"})
        if not city:
            return self.json_response(400, {"error": "city (or location) is required"})
        if path == "/api/job-search":
            if not keyword:
                return self.json_response(400, {"error": "keyword is required"})
            jobs, report = search_jobs(keyword, city, pages=pages, limit_per_source=max_jobs, sources=sources)
            summary = next((entry["summary"] for entry in report if "summary" in entry), {})
            return self.json_response(200, {
                "keyword": keyword, "city": city, "count": len(jobs),
                "unique": summary.get("unique", len(jobs)),
                "raw_total": summary.get("raw_total", len(jobs)),
                "duplicates_merged": summary.get("merged", 0),
                "sources": report, "jobs": jobs,
            })
        if not company:
            return self.json_response(400, {"error": "company is required"})
        jobs, report = search_jobs(
            company, city, pages=pages, limit_per_source=max_jobs, sources=sources, company=company,
        )
        summary = next((entry["summary"] for entry in report if "summary" in entry), {})
        matched = [job for job in jobs if company_matches(company, job.get("company") or "")]
        if path == "/api/company-match":
            if not keyword:
                return self.json_response(400, {"error": "keyword (the field) is required"})
            verdict = match_field(matched, keyword)
            return self.json_response(200, {
                "company": company, "keyword": keyword, "city": city,
                "matches": verdict["matches"],
                "confidence": verdict["confidence"],
                "matched_postings": verdict["matched_postings"],
                "title_matches": verdict["title_matches"],
                "total_postings": verdict["total_postings"],
                "match_ratio": verdict["match_ratio"],
                "field_terms": verdict["field_terms"],
                "reason": verdict.get("reason"),
                "evidence": verdict["evidence"][:10],
                "postings_found": summary.get("raw_total", len(jobs)),
                "postings_unique": len(jobs),
                "duplicates_merged": summary.get("merged", 0),
                "company_matched": len(matched),
                "sources": report,
            })
        corpus = matched
        if corpus:
            genre = infer_genre(corpus)
        else:
            genre = {
                "genre": None, "confidence": 0.0, "method": "none",
                "reason": "no postings found for this company in this city",
            }
        return self.json_response(200, {
            "company": company, "city": city, "genre": genre,
            "postings_found": summary.get("raw_total", len(jobs)),
            "postings_unique": len(jobs),
            "duplicates_merged": summary.get("merged", 0),
            "postings_used": len(corpus), "company_matched": len(matched),
            "sources": report,
            "sample_titles": [job.get("title", "") for job in corpus[:20]],
            "sample_jobs": corpus[:10],
        })

    def alternative_search(self, keyword, latitude, longitude, radius_km):
        """Keyless background search using OpenStreetMap (Nominatim, then Overpass)."""
        # Only letters and digits ever reach the Overpass query, so a keyword
        # cannot break out of its quoted regex.
        words = re.findall(r"[A-Za-z0-9]+", keyword)
        terms = [word.lower() for word in words if len(word) > 2 and word.lower() not in STOP_WORDS]
        pattern = "|".join(terms[:4]) or "|".join(word.lower() for word in words[:4])
        if not pattern:
            return self.json_response(400, {"error": "keyword must contain letters or digits"})
        # Nominatim can return nearby named businesses quickly and needs no key.
        try:
            delta_lat = radius_km / 111.0
            delta_lon = radius_km / (111.0 * max(0.1, math.cos(math.radians(latitude))))
            params = urllib.parse.urlencode({
                "q": " ".join(words[:4]), "format": "jsonv2",
                "limit": 40, "addressdetails": 1, "extratags": 1, "bounded": 1,
                "viewbox": f"{longitude - delta_lon},{latitude + delta_lat},{longitude + delta_lon},{latitude - delta_lat}",
            })
            places = nominatim_get(f"https://nominatim.openstreetmap.org/search?{params}", timeout=18)
            if places:
                results = []
                for place in places:
                    extra = place.get("extratags") or {}
                    results.append({
                        "title": place.get("name") or place.get("display_name", "").split(",")[0],
                        "category": str(place.get("type") or place.get("category") or "business").replace("_", " ").title(),
                        "phone": extra.get("contact:phone") or extra.get("phone", ""),
                        "emails": extra.get("contact:email") or extra.get("email", ""),
                        "website": extra.get("contact:website") or extra.get("website", ""),
                        "address": place.get("display_name", ""), "latitude": place.get("lat", ""),
                        "longitude": place.get("lon", ""), "review_rating": "", "review_count": "",
                        "provider": "OpenStreetMap",
                    })
                return self.json_response(200, {"results": results, "provider": "OpenStreetMap"})
        except Exception as error:  # noqa: BLE001 - fall through to Overpass
            log.warning("Nominatim business search failed (%s: %s); trying Overpass", type(error).__name__, error)
        category_queries = []
        lowered = keyword.lower()
        mappings = {
            "coffee": '["amenity"="cafe"]', "cafe": '["amenity"="cafe"]',
            "restaurant": '["amenity"="restaurant"]', "dentist": '["amenity"="dentist"]',
            "hospital": '["amenity"="hospital"]', "pharmacy": '["amenity"="pharmacy"]',
            "gym": '["leisure"="fitness_centre"]', "hotel": '["tourism"="hotel"]',
            "school": '["amenity"="school"]', "bank": '["amenity"="bank"]',
        }
        for word, selector in mappings.items():
            if word in lowered:
                category_queries.append(f'nwr(around:{int(radius_km * 1000)},{latitude},{longitude}){selector};')
        overpass = (
            f'[out:json][timeout:35];('
            f'nwr(around:{int(radius_km * 1000)},{latitude},{longitude})["name"~"{pattern}",i];'
            f'nwr(around:{int(radius_km * 1000)},{latitude},{longitude})["brand"~"{pattern}",i];'
            f'nwr(around:{int(radius_km * 1000)},{latitude},{longitude})["operator"~"{pattern}",i];'
            + "".join(category_queries) + ");out center tags 100;"
        )
        errors = []
        payload = None
        for endpoint in ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"):
            try:
                data = urllib.parse.urlencode({"data": overpass}).encode()
                request = urllib.request.Request(endpoint, data=data, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=15) as response:
                    payload = json.loads(response.read())
                break
            except Exception as error:  # noqa: BLE001 - try the next mirror
                errors.append(f"{endpoint}: {type(error).__name__}: {error}")
        if payload is None:
            log.warning("Overpass search failed: %s", "; ".join(errors) or "unknown error")
            return self.json_response(502, {"error": "Alternative map search unavailable"})
        results = []
        for element in payload.get("elements", []):
            tags = element.get("tags", {})
            title = tags.get("name") or tags.get("brand") or tags.get("operator")
            if not title:
                continue
            category = next((tags.get(key) for key in ("amenity", "shop", "office", "craft", "tourism", "leisure", "healthcare") if tags.get(key)), "business")
            street = " ".join(filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")]))
            address = ", ".join(filter(None, [street, tags.get("addr:suburb"), tags.get("addr:city"), tags.get("addr:postcode")]))
            center = element.get("center", element)
            results.append({
                "title": title, "category": category.replace("_", " ").title(),
                "phone": tags.get("contact:phone") or tags.get("phone", ""),
                "emails": tags.get("contact:email") or tags.get("email", ""),
                "website": tags.get("contact:website") or tags.get("website", ""),
                "address": address, "latitude": center.get("lat", ""), "longitude": center.get("lon", ""),
                "review_rating": "", "review_count": "", "provider": "OpenStreetMap",
            })
        return self.json_response(200, {"results": results, "provider": "OpenStreetMap"})


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if bool(AUTH_USER) != bool(AUTH_PASSWORD):
        log.warning("Set both AUTH_USER and AUTH_PASSWORD to enable authentication; it is currently OFF")
    server = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    log.info(
        "SYNCAXIS LEADGEN TOOL listening on %s:%s (auth %s, rate limit %s/min)",
        BIND_HOST, PORT, "on" if (AUTH_USER and AUTH_PASSWORD) else "off",
        RATE_LIMIT_PER_MIN or "off",
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
