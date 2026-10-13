"""Tests for the HTTP gateway's hardening: limits, auth, error handling, injection.

Run from the repository root::

    python -m unittest discover -s tests -v

The real handler is served on a loopback port; the scraper, geocoders and
job sources are stubbed, so no network access is required.
"""

import base64
import http.client
import json
import os
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("JOBS_CACHE_TTL", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402

SECRET = "internal stack trace /srv/scraper/db.py line 42"


class StubScraper(BaseHTTPRequestHandler):
    """Minimal stand-in for the google-maps-scraper container."""

    def do_GET(self):
        if self.path == "/api/v1/jobs/boom":
            self._send(500, "text/plain", SECRET)
        elif self.path == "/api/v1/jobs/missing":
            self._send(404, "text/plain", "job not found")
        elif self.path == "/api/v1/jobs/abc-123/download":
            self._send(200, "text/csv", "title\nCafe\n")
        elif self.path == "/api/v1/jobs":
            self._send(200, "application/json", "[]")
        else:
            self._send(200, "application/json", '{"Status": "ok"}')

    def _send(self, status, content_type, body):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class GatewayTestCase(unittest.TestCase):
    """Starts the gateway (and a stub scraper) once per test class."""

    @classmethod
    def setUpClass(cls):
        cls.scraper = ThreadingHTTPServer(("127.0.0.1", 0), StubScraper)
        threading.Thread(target=cls.scraper.serve_forever, daemon=True).start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]
        cls._saved = {
            name: getattr(app, name)
            for name in (
                "SCRAPER_URL", "AUTH_USER", "AUTH_PASSWORD", "RATE_LIMIT_PER_MIN",
                "MAX_BODY_BYTES", "GOOGLE_MAPS_API_KEY", "GOOGLE_MAPS_BROWSER_KEY",
                "HERE_API_KEY",
            )
        }

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.scraper):
            server.shutdown()
            server.server_close()

    def setUp(self):
        for name, value in self._saved.items():
            setattr(app, name, value)
        app.SCRAPER_URL = f"http://127.0.0.1:{self.scraper.server_address[1]}"
        app.RATE_LIMIT_PER_MIN = 0
        app.RATE_LIMITER.reset()
        app._geocode_cache.clear()

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(app, name, value)

    def call(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            request_headers = dict(headers or {})
            if payload is not None:
                request_headers["Content-Type"] = "application/json"
            connection.request(method, path, body=payload, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            return response.status, data, response, raw
        finally:
            connection.close()


class RequestLimitTests(GatewayTestCase):
    def test_oversized_body_is_rejected_without_being_read(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.putrequest("POST", "/api/job-search")
        connection.putheader("Content-Length", str(app.MAX_BODY_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        connection.close()

    def test_invalid_content_length_is_a_400(self):
        for value in ("-5", "abc"):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            connection.putrequest("POST", "/api/job-search")
            connection.putheader("Content-Length", value)
            connection.endheaders()
            self.assertEqual(connection.getresponse().status, 400, value)
            connection.close()

    def test_body_must_be_a_json_object(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        for raw in (b"{not json", b"[1, 2]", b"\xff\xfe"):
            connection.request("POST", "/api/job-search", body=raw,
                               headers={"Content-Length": str(len(raw))})
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 400, raw)
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def test_unknown_post_path_is_404(self):
        self.assertEqual(self.call("POST", "/api/nope", {})[0], 404)

    def test_sources_must_be_a_short_list_of_strings(self):
        for sources in ("ats", [1, 2], ["ats"] * 11):
            status, _, _, _ = self.call(
                "POST", "/api/job-search", {"keyword": "x", "city": "Pune", "sources": sources})
            self.assertEqual(status, 400, sources)

    def test_non_finite_coordinates_are_rejected(self):
        for lat in ("nan", "inf", "91"):
            status, _, _, _ = self.call(
                "POST", "/api/alternative-search",
                {"keyword": "cafe", "lat": lat, "lon": "73.8", "radius_km": 5})
            self.assertEqual(status, 400, lat)


class JobIdAndProxyTests(GatewayTestCase):
    def test_only_uuid_like_ids_with_optional_download_are_proxied(self):
        for path in ("/api/jobs/../../etc/passwd", "/api/jobs/abc/def", "/api/jobs/abc/delete",
                     "/api/jobs/abc%2Fdef", "/api/jobs/" + "a" * 65, "/api/jobs/"):
            status, data, _, _ = self.call("GET", path)
            self.assertEqual(status, 400, path)
            self.assertEqual(data["error"], "Invalid job id")

    def test_valid_id_and_download_are_proxied(self):
        status, data, _, _ = self.call("GET", "/api/jobs/anything-1")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"Status": "ok"})
        status, _, response, raw = self.call("GET", "/api/jobs/abc-123/download")
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"title\nCafe\n")
        self.assertEqual(
            response.getheader("Content-Disposition"),
            'attachment; filename="syncaxis-leads-abc-123.csv"',
        )

    def test_scraper_server_errors_are_not_leaked(self):
        status, data, _, raw = self.call("GET", "/api/jobs/boom")
        self.assertEqual(status, 502)
        self.assertNotIn(SECRET.encode(), raw)
        self.assertEqual(data, {"error": "Scraper service returned an error"})

    def test_scraper_client_errors_keep_a_short_actionable_message(self):
        status, data, _, _ = self.call("GET", "/api/jobs/missing")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "job not found")

    def test_unreachable_scraper_gives_a_generic_message(self):
        app.SCRAPER_URL = "http://127.0.0.1:9"  # discard port: nothing listens
        status, data, _, raw = self.call("GET", "/api/jobs")
        self.assertEqual(status, 502)
        self.assertEqual(data, {"error": "Scraper service is unavailable"})
        self.assertNotIn(b"Connection refused", raw)


class AuthTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        app.AUTH_USER, app.AUTH_PASSWORD = "ops", "s3cret pass"

    @staticmethod
    def basic(user, password):
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def test_requests_without_credentials_are_challenged(self):
        status, _, response, _ = self.call("GET", "/api/job-sources")
        self.assertEqual(status, 401)
        self.assertIn("Basic", response.getheader("WWW-Authenticate"))

    def test_wrong_credentials_are_rejected(self):
        for headers in (self.basic("ops", "wrong"), self.basic("root", "s3cret pass"),
                        {"Authorization": "Basic !!!not-base64!!!"},
                        {"Authorization": "Bearer abc"}):
            self.assertEqual(self.call("GET", "/api/job-sources", headers=headers)[0], 401, headers)

    def test_correct_credentials_are_accepted(self):
        headers = self.basic("ops", "s3cret pass")
        self.assertEqual(self.call("GET", "/api/job-sources", headers=headers)[0], 200)
        self.assertEqual(self.call("GET", "/", headers=headers)[0], 200)

    def test_posts_static_files_and_head_all_require_credentials(self):
        self.assertEqual(self.call("POST", "/api/job-search", {})[0], 401)
        self.assertEqual(self.call("GET", "/index.html")[0], 401)
        self.assertEqual(self.call("HEAD", "/index.html")[0], 401)

    def test_liveness_probe_needs_no_credentials(self):
        status, data, _, _ = self.call("GET", "/healthz")
        self.assertEqual((status, data), (200, {"status": "ok"}))

    def test_auth_is_off_unless_both_values_are_set(self):
        app.AUTH_PASSWORD = ""
        self.assertEqual(self.call("GET", "/api/job-sources")[0], 200)


class RateLimitTests(GatewayTestCase):
    def test_limiter_window(self):
        limiter = app.RateLimiter()
        results = [limiter.check("a", 2, window=10, now=t)[0] for t in (0, 1, 2)]
        self.assertEqual(results, [True, True, False])
        allowed, retry_after = limiter.check("a", 2, window=10, now=3)
        self.assertFalse(allowed)
        self.assertGreaterEqual(retry_after, 1)
        self.assertTrue(limiter.check("a", 2, window=10, now=11)[0])  # window slid
        self.assertTrue(limiter.check("b", 2, window=10, now=3)[0])  # other client

    def test_expensive_endpoints_return_429_with_retry_after(self):
        app.RATE_LIMIT_PER_MIN = 3
        statuses = [self.call("POST", "/api/job-search", {})[0] for _ in range(3)]
        self.assertEqual(statuses, [400, 400, 400])  # validated, but counted
        status, _, response, _ = self.call("POST", "/api/job-search", {})
        self.assertEqual(status, 429)
        self.assertGreaterEqual(int(response.getheader("Retry-After")), 1)

    def test_cheap_reads_are_not_limited(self):
        app.RATE_LIMIT_PER_MIN = 1
        for _ in range(5):
            self.assertEqual(self.call("GET", "/api/job-sources")[0], 200)
            self.assertEqual(self.call("GET", "/healthz")[0], 200)

    def test_zero_disables_the_limit(self):
        app.RATE_LIMIT_PER_MIN = 0
        for _ in range(10):
            self.assertEqual(self.call("POST", "/api/job-search", {})[0], 400)


class HeaderAndStaticTests(GatewayTestCase):
    def test_security_headers_on_api_and_static_responses(self):
        for path in ("/api/job-sources", "/healthz", "/index.html"):
            _, _, response, _ = self.call("GET", path)
            self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff", path)
            self.assertEqual(response.getheader("X-Frame-Options"), "DENY", path)
            self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer", path)

    def test_directories_are_not_listed(self):
        for path in ("/assets/", "/vendor/", "/assets"):
            status, _, _, raw = self.call("GET", path)
            self.assertNotEqual(status, 200, path)
            self.assertNotIn(b"Directory listing", raw)

    def test_python_version_is_not_advertised(self):
        _, _, response, _ = self.call("GET", "/healthz")
        self.assertNotIn("Python", response.getheader("Server") or "")


class MapKeyTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self._provider = os.environ.get("MAP_PROVIDER")
        os.environ["MAP_PROVIDER"] = "google"

    def tearDown(self):
        super().tearDown()
        if self._provider is None:
            os.environ.pop("MAP_PROVIDER", None)
        else:
            os.environ["MAP_PROVIDER"] = self._provider

    def test_browser_gets_its_own_key_never_the_server_key(self):
        app.GOOGLE_MAPS_API_KEY = "server-geocoding-key"
        app.GOOGLE_MAPS_BROWSER_KEY = "browser-referrer-restricted-key"
        status, data, _, raw = self.call("GET", "/api/map-config")
        self.assertEqual(data, {"provider": "google", "key": "browser-referrer-restricted-key"})
        self.assertNotIn(b"server-geocoding-key", raw)

    def test_browser_key_falls_back_to_the_server_key(self):
        app.GOOGLE_MAPS_API_KEY = "only-key"
        app.GOOGLE_MAPS_BROWSER_KEY = "only-key"  # what the module computes when unset
        _, data, _, _ = self.call("GET", "/api/map-config")
        self.assertEqual(data["key"], "only-key")


class GeocodeTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        app.GOOGLE_MAPS_API_KEY = ""
        self._nominatim = app.nominatim_get
        self.calls = []

        def fake(url, timeout):
            self.calls.append(url)
            return [{"lat": "18.52", "lon": "73.85", "display_name": "Pune, India"}]

        app.nominatim_get = fake

    def tearDown(self):
        app.nominatim_get = self._nominatim
        super().tearDown()

    def test_repeat_lookups_are_cached(self):
        first = self.call("GET", "/api/geocode?q=Pune")
        second = self.call("GET", "/api/geocode?q=pune")
        self.assertEqual(first[0], 200)
        self.assertEqual(first[1], second[1])
        self.assertEqual(len(self.calls), 1)

    def test_failures_are_generic_and_not_cached(self):
        def boom(url, timeout):
            raise OSError("socket detail that should stay in the logs")
        app.nominatim_get = boom
        status, data, _, raw = self.call("GET", "/api/geocode?q=Nowhere")
        self.assertEqual((status, data), (502, {"error": "Geocoding failed"}))
        self.assertNotIn(b"socket detail", raw)
        self.assertNotIn("nowhere", app._geocode_cache)

    def test_missing_query_is_a_400(self):
        self.assertEqual(self.call("GET", "/api/geocode?q=")[0], 400)


class _FakeUrlopenResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class OverpassInjectionTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self._nominatim = app.nominatim_get
        self._urlopen = app.urllib.request.urlopen
        self.queries = []

        def no_nominatim(url, timeout):
            raise OSError("force the Overpass fallback")

        def fake_urlopen(request, timeout=0):
            data = urllib.parse.parse_qs(request.data.decode())["data"][0]
            self.queries.append(data)
            return _FakeUrlopenResponse({"elements": [
                {"tags": {"name": "Cafe One", "amenity": "cafe"}, "center": {"lat": 1, "lon": 2}}]})

        app.nominatim_get = no_nominatim
        app.urllib.request.urlopen = fake_urlopen

    def tearDown(self):
        app.nominatim_get = self._nominatim
        app.urllib.request.urlopen = self._urlopen
        super().tearDown()

    def search(self, keyword):
        return self.call("POST", "/api/alternative-search",
                         {"keyword": keyword, "lat": "18.5", "lon": "73.8", "radius_km": 5})

    def test_keyword_cannot_break_out_of_the_query(self):
        status, data, _, _ = self.search('x"];out;//')
        self.assertEqual(status, 200)
        query = self.queries[0]
        self.assertEqual(data["results"][0]["title"], "Cafe One")
        self.assertIn('["name"~"out",i]', query)
        self.assertNotIn("//", query)
        self.assertNotIn('"];out;', query)
        self.assertEqual(query.count('",i];'), 3)  # name / brand / operator, nothing extra

    def test_quotes_and_regex_syntax_never_reach_the_pattern(self):
        self.search('coffee"|.*|"(')
        pattern = self.queries[0].split('["name"~"')[1].split('",i]')[0]
        self.assertRegex(pattern, r"^[a-z0-9|]+$")

    def test_keyword_without_letters_or_digits_is_rejected(self):
        status, data, _, _ = self.search('"];//')
        self.assertEqual(status, 400)
        self.assertEqual(self.queries, [])

    def test_longitude_window_widens_with_latitude(self):
        captured = []

        def capture(url, timeout):
            captured.append(url)
            return []

        app.nominatim_get = capture
        self.call("POST", "/api/alternative-search",
                  {"keyword": "cafe", "lat": "60", "lon": "10", "radius_km": 10})
        viewbox = urllib.parse.parse_qs(urllib.parse.urlparse(captured[0]).query)["viewbox"][0]
        left, top, right, bottom = (float(part) for part in viewbox.split(","))
        self.assertGreater((right - left) / (top - bottom), 1.9)  # ~2x at 60 degrees


class CompanyEndpointTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self._search = app.search_jobs
        self.captured = []

        def fake_search(*args, **kwargs):
            self.captured.append((args, kwargs))
            return (
                [
                    {"title": "SMT Engineer", "company": "ABB India Ltd", "description": "soldering"},
                    {"title": "Pharmacist", "company": "Abbott Laboratories", "description": "gmp"},
                ],
                [{"source": "adzuna", "count": 2}],
            )

        app.search_jobs = fake_search

    def tearDown(self):
        app.search_jobs = self._search
        super().tearDown()

    def test_company_is_matched_on_whole_names(self):
        status, data, _, _ = self.call(
            "POST", "/api/company-genre", {"company": "ABB", "city": "Pune"})
        self.assertEqual(status, 200)
        self.assertEqual(data["company_matched"], 1)
        self.assertEqual(data["sample_titles"], ["SMT Engineer"])

    def test_company_lookups_pass_the_company_to_search(self):
        self.call("POST", "/api/company-match", {"company": "ABB", "keyword": "electronics", "city": "Pune"})
        self.assertEqual(self.captured[0][1]["company"], "ABB")

    def test_plain_job_search_has_no_company(self):
        self.call("POST", "/api/job-search", {"keyword": "welding", "city": "Pune"})
        self.assertNotIn("company", self.captured[0][1])

    def test_generic_field_keyword_is_reported_as_unknown(self):
        status, data, _, _ = self.call(
            "POST", "/api/company-match", {"company": "ABB", "keyword": "design engineer", "city": "Pune"})
        self.assertEqual(status, 200)
        self.assertIsNone(data["matches"])
        self.assertIn("generic", data["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
