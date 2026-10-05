#!/usr/bin/env python3
"""Small dependency-free web gateway for the local Google Maps scraper."""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from network_year import current_year
from genre import infer_genre, match_field
from jobsources import available_sources, search_jobs

ROOT = Path(__file__).parent / "web"
SCRAPER_URL = os.environ.get("SCRAPER_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
PORT = int(os.environ.get("PORT", "3000"))
USER_AGENT = "maps-lead-finder/1.0 (local development app)"
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} - {fmt % args}")

    def json_response(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
                    self.send_header("Content-Disposition", f'attachment; filename="atlas-results-{job_id[:12]}.csv"')
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as error:
            self.json_response(error.code, {"error": error.read().decode(errors="replace")[:500]})
        except (urllib.error.URLError, TimeoutError) as error:
            self.json_response(502, {"error": "Scraper service is unavailable", "detail": str(error.reason if hasattr(error, "reason") else error)})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/time":
            return self.json_response(200, current_year())
        if parsed.path == "/api/health":
            return self.proxy("GET", "/api/v1/jobs")
        if parsed.path == "/api/geocode":
            query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0].strip()
            if not query:
                return self.json_response(400, {"error": "Location is required"})
            try:
                if GOOGLE_MAPS_API_KEY:
                    url = "https://maps.googleapis.com/maps/api/geocode/json?" + urllib.parse.urlencode({
                        "address": query, "key": GOOGLE_MAPS_API_KEY,
                    })
                else:
                    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
                        "q": query, "format": "jsonv2", "limit": 1,
                    })
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=20) as response:
                    payload = json.loads(response.read())
                if GOOGLE_MAPS_API_KEY:
                    if payload.get("status") != "OK" or not payload.get("results"):
                        detail = payload.get("error_message") or payload.get("status", "No result")
                        return self.json_response(502, {"error": "Google geocoding failed", "detail": detail})
                    result = payload["results"][0]
                    point = result["geometry"]["location"]
                    return self.json_response(200, {
                        "lat": str(point["lat"]), "lon": str(point["lng"]),
                        "label": result["formatted_address"], "provider": "google",
                    })
                if not payload:
                    return self.json_response(404, {"error": "Location not found"})
                hit = payload[0]
                return self.json_response(200, {
                    "lat": hit["lat"], "lon": hit["lon"], "label": hit["display_name"],
                    "provider": "nominatim",
                })
            except Exception as error:
                return self.json_response(502, {"error": "Geocoding failed", "detail": str(error)})
        if parsed.path.startswith("/api/jobs/"):
            suffix = parsed.path.removeprefix("/api/jobs/")
            if not suffix.replace("/", "").replace("-", "").isalnum():
                return self.json_response(400, {"error": "Invalid job id"})
            return self.proxy("GET", f"/api/v1/jobs/{suffix}")
        if parsed.path == "/api/jobs":
            return self.proxy("GET", "/api/v1/jobs")
        if parsed.path == "/api/job-sources":
            return self.json_response(200, {"sources": available_sources()})
        return super().do_GET()

    def do_POST(self):
        if self.path in {"/api/company-genre", "/api/job-search", "/api/company-match"}:
            return self.jobs_endpoint()
        if self.path not in {"/api/jobs", "/api/alternative-search"}:
            return self.json_response(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            incoming = json.loads(self.rfile.read(length) or b"{}")
            keyword = str(incoming.get("keyword", "")).strip()
            lat = str(incoming.get("lat", "")).strip()
            lon = str(incoming.get("lon", "")).strip()
            depth = max(1, min(int(incoming.get("depth", 5)), 20))
            radius_km = float(incoming.get("radius_km", 10))
            if not keyword or not lat or not lon:
                raise ValueError("keyword, lat and lon are required")
            latitude = float(lat); longitude = float(lon)
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError("coordinates are outside the valid range")
            if not 1 <= radius_km <= 50:
                raise ValueError("radius must be between 1 and 50 km")
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            return self.json_response(400, {"error": str(error)})
        if self.path == "/api/alternative-search":
            return self.alternative_search(keyword, latitude, longitude, radius_km)
        location_name = str(incoming.get("location", "")).strip()[:200]
        search_term = f"{keyword} in {location_name}" if location_name else f"{keyword} near {latitude},{longitude}"
        payload = {
            "name": "maps-lead-finder", "keywords": [search_term], "lang": "en",
            "zoom": 15, "lat": lat, "lon": lon, "radius": int(radius_km * 1000),
            "depth": depth, "email": bool(incoming.get("email", False)),
            "fast_mode": False, "max_time": 600,
        }
        return self.proxy("POST", "/api/v1/jobs", payload)

    def jobs_endpoint(self):
        """Search job postings and infer a company's industry from them."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            incoming = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as error:
            return self.json_response(400, {"error": str(error)})
        company = str(incoming.get("company", "")).strip()[:120]
        keyword = str(incoming.get("keyword", "")).strip()[:120]
        city = str(incoming.get("city", "") or incoming.get("location", "")).strip()[:120]
        sources = incoming.get("sources")
        if sources is not None and not isinstance(sources, list):
            return self.json_response(400, {"error": "sources must be a list of source names"})
        try:
            pages = max(1, min(int(incoming.get("pages", 2)), 5))
            max_jobs = max(5, min(int(incoming.get("max_jobs", 40)), 200))
        except (ValueError, TypeError):
            return self.json_response(400, {"error": "pages and max_jobs must be integers"})
        if not city:
            return self.json_response(400, {"error": "city (or location) is required"})
        if self.path == "/api/job-search":
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
        jobs, report = search_jobs(company, city, pages=pages, limit_per_source=max_jobs, sources=sources)
        summary = next((entry["summary"] for entry in report if "summary" in entry), {})
        matched = [job for job in jobs if company.lower() in (job.get("company") or "").lower()]
        if self.path == "/api/company-match":
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
        """Keyless background search using OpenStreetMap's Overpass API."""
        stop_words = {"a", "an", "and", "business", "businesses", "company", "companies", "in", "near", "the"}
        terms = [re.escape(term) for term in re.findall(r"[A-Za-z0-9]+", keyword.lower()) if len(term) > 2 and term not in stop_words]
        pattern = "|".join(terms[:4]) or re.escape(keyword[:40])
        # Nominatim can return nearby named businesses quickly and needs no key.
        try:
            delta = radius_km / 111
            params = urllib.parse.urlencode({
                "q": " ".join(re.findall(r"[A-Za-z0-9]+", keyword)[:4]), "format": "jsonv2",
                "limit": 40, "addressdetails": 1, "extratags": 1, "bounded": 1,
                "viewbox": f"{longitude-delta},{latitude+delta},{longitude+delta},{latitude-delta}",
            })
            request = urllib.request.Request(f"https://nominatim.openstreetmap.org/search?{params}", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=18) as response:
                places = json.loads(response.read())
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
        except Exception:
            pass
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
            except Exception as error:
                errors.append(str(error))
        if payload is None:
            return self.json_response(502, {"error": "Alternative map search unavailable", "detail": errors[-1] if errors else "Unknown error"})
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


if __name__ == "__main__":
    print(f"SYNCAXIS LEADGEN TOOL: http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
