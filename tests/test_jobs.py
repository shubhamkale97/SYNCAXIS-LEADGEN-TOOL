"""Tests for the job-source and genre-inference features.

Run from the repository root::

    python -m unittest discover -s tests -v

No network access is required: HTTP calls are monkeypatched with fixtures.
"""

import json
import os
import sys
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import genre  # noqa: E402
import jobsources  # noqa: E402

LINKEDIN_HTML = """
<ul>
<li>
  <div class="base-card base-search-card">
    <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/3912345678">x</a>
    <h3 class="base-search-card__title">Design Engineer</h3>
    <h4 class="base-search-card__subtitle"><a href="https://www.linkedin.com/company/portescap">Portescap</a></h4>
    <div class="base-search-card__metadata">
      <span class="job-search-card__location">Pune, Maharashtra, India</span>
      <time class="job-search-card__listdate" datetime="2026-09-20">3 days ago</time>
    </div>
  </div>
</li>
<li>
  <div class="base-card">
    <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/3999999999">x</a>
    <h3 class="base-search-card__title">Servo Motor Winding Technician</h3>
    <h4 class="base-search-card__subtitle"><a href="#">Portescap</a></h4>
    <span class="job-search-card__location">Pune, India</span>
  </div>
</li>
</ul>
"""


class LinkedInParseTests(unittest.TestCase):
    def test_parses_cards(self):
        jobs = jobsources.parse_linkedin_cards(LINKEDIN_HTML)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["title"], "Design Engineer")
        self.assertEqual(jobs[0]["company"], "Portescap")
        self.assertEqual(jobs[0]["location"], "Pune, Maharashtra, India")
        self.assertIn("/jobs/view/3912345678", jobs[0]["url"])
        self.assertEqual(jobs[1]["title"], "Servo Motor Winding Technician")

    def test_empty_html_is_safe(self):
        self.assertEqual(jobsources.parse_linkedin_cards("<html></html>"), [])


class SourceAdapterTests(unittest.TestCase):
    def setUp(self):
        self._original_http = jobsources._http

    def tearDown(self):
        jobsources._http = self._original_http

    def test_linkedin_search_paces_and_caps(self):
        os.environ["LINKEDIN_DELAY"] = "0"
        os.environ["LINKEDIN_PAGES"] = "2"
        calls = []

        def fake_http(method, url, **kwargs):
            calls.append(url)
            return LINKEDIN_HTML

        jobsources._http = fake_http
        source = jobsources.LinkedInGuestSource()
        jobs = source.search("engineer", "Pune", pages=2, limit=3)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(len(calls), 2)
        self.assertIn("start=25", calls[1])

    def test_adzuna_parses_results(self):
        os.environ["ADZUNA_APP_ID"] = "id"
        os.environ["ADZUNA_APP_KEY"] = "key"
        payload = {
            "results": [
                {
                    "title": "SMT Process Engineer",
                    "company": {"display_name": "Acme Electronics"},
                    "location": {"display_name": "Pune, India"},
                    "description": "soldering and reflow experience",
                    "redirect_url": "https://adzuna.in/x",
                    "created": "2026-09-01T00:00:00Z",
                }
            ]
        }
        jobsources._http = lambda method, url, **kwargs: json.dumps(payload)
        source = jobsources.AdzunaSource()
        self.assertTrue(source.configured())
        jobs = source.search("smt", "Pune", pages=1)
        self.assertEqual(jobs[0]["company"], "Acme Electronics")
        self.assertEqual(jobs[0]["source"], "adzuna")

    def test_jooble_parses_results(self):
        os.environ["JOOBLE_API_KEY"] = "key"
        payload = {
            "totalCount": 1,
            "jobs": [
                {
                    "title": "CNC Machinist",
                    "company": "Precision Works",
                    "location": "Pune",
                    "snippet": "lathe and milling",
                    "link": "https://jooble.org/x",
                    "updated": "2026-09-02",
                }
            ],
        }
        jobsources._http = lambda method, url, **kwargs: json.dumps(payload)
        source = jobsources.JoobleSource()
        jobs = source.search("cnc", "Pune")
        self.assertEqual(jobs[0]["title"], "CNC Machinist")
        self.assertEqual(jobs[0]["source"], "jooble")

    def test_serpapi_parses_results(self):
        os.environ["SERPAPI_API_KEY"] = "key"
        payload = {
            "jobs_results": [
                {
                    "title": "Automation Engineer",
                    "company_name": "RoboCorp",
                    "location": "Pune, India",
                    "description": "PLC and SCADA",
                    "apply_options": [{"link": "https://x"}],
                    "detected_extensions": {"posted_at": "2 days ago"},
                }
            ],
            "serpapi_pagination": {},
        }
        jobsources._http = lambda method, url, **kwargs: json.dumps(payload)
        source = jobsources.SerpApiSource()
        jobs = source.search("automation", "Pune")
        self.assertEqual(jobs[0]["company"], "RoboCorp")
        self.assertEqual(jobs[0]["url"], "https://x")

    def test_unconfigured_sources_are_skipped(self):
        for name in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY", "JOOBLE_API_KEY", "SERPAPI_API_KEY"):
            os.environ.pop(name, None)
        chosen = jobsources.selected_sources(["adzuna", "jooble", "serpapi"])
        self.assertEqual(chosen, [])

    def test_search_jobs_reports_failures(self):
        class Boom(jobsources.JobSource):
            name = "boom"

            def search(self, *args, **kwargs):
                raise RuntimeError("kaboom")

        original = jobsources.selected_sources
        jobsources.selected_sources = lambda names=None: [Boom()]
        try:
            jobs, report = jobsources.search_jobs("x", "Pune")
        finally:
            jobsources.selected_sources = original
        self.assertEqual(jobs, [])
        self.assertEqual(report[0]["source"], "boom")
        self.assertIn("kaboom", report[0]["error"])


class GenreTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("LLM_API_KEY", None)

    def test_electronics_heuristic(self):
        jobs = [
            {"title": "SMT Process Engineer", "description": "soldering, reflow, PCB assembly"},
            {"title": "PCB Design Engineer", "description": "surface mount and component placement"},
            {"title": "AOI Test Technician", "description": "pcba inspection"},
        ]
        result = genre.infer_genre(jobs)
        self.assertEqual(result["genre"], "Electronics & Semiconductor Manufacturing")
        self.assertEqual(result["method"], "heuristics")

    def test_motion_control_heuristic(self):
        jobs = [
            {"title": "Servo Motor Design Engineer", "description": "brushless motor and encoder"},
            {"title": "Motion Control Specialist", "description": "actuator and gearbox sizing"},
        ]
        result = genre.infer_genre(jobs)
        self.assertEqual(result["genre"], "Precision Engineering & Motion Control")

    def test_empty_corpus(self):
        result = genre.infer_genre([])
        self.assertIsNone(result["genre"])
        self.assertEqual(result["method"], "none")

    def test_llm_fallback_used_when_weak(self):
        os.environ["LLM_API_KEY"] = "test"
        original = genre.llm_genre
        genre.llm_genre = lambda jobs: {"genre": "Robotics", "confidence": 0.7, "reasoning": "x"}
        try:
            result = genre.infer_genre([{"title": "Generalist", "description": "office admin"}])
        finally:
            genre.llm_genre = original
            os.environ.pop("LLM_API_KEY", None)
        self.assertEqual(result["method"], "llm")
        self.assertEqual(result["genre"], "Robotics")


class EndpointTests(unittest.TestCase):
    """Drive the real HTTP handler with the sources stubbed out."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, path, body):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as response:
            return json.loads(response.read())

    def test_job_sources_listing(self):
        payload = self._get("/api/job-sources")
        names = {source["name"] for source in payload["sources"]}
        self.assertEqual(names, {"linkedin", "adzuna", "jooble", "serpapi"})

    def test_company_genre_endpoint(self):
        original_search, original_infer = app.search_jobs, app.infer_genre
        app.search_jobs = lambda *a, **k: (
            [
                {"title": "SMT Engineer", "company": "Portescap", "description": "soldering"},
                {"title": "Other Role", "company": "Somebody Else", "description": ""},
            ],
            [{"source": "adzuna", "count": 2}],
        )
        app.infer_genre = lambda corpus: {"genre": "Electronics", "confidence": 0.8, "method": "heuristics"}
        try:
            payload = self._post("/api/company-genre", {"company": "Portescap", "city": "Pune"})
        finally:
            app.search_jobs, app.infer_genre = original_search, original_infer
        self.assertEqual(payload["company"], "Portescap")
        self.assertEqual(payload["genre"]["genre"], "Electronics")
        self.assertEqual(payload["company_matched"], 1)
        self.assertEqual(payload["postings_used"], 1)

    def test_job_search_requires_keyword(self):
        try:
            self._post("/api/job-search", {"city": "Pune"})
            self.fail("expected HTTP 400")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 400)

    def test_company_genre_requires_city(self):
        try:
            self._post("/api/company-genre", {"company": "X"})
            self.fail("expected HTTP 400")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
