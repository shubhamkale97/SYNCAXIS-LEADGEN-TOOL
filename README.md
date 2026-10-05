# SYNCAXIS LEADGEN TOOL

Syncaxis Leadgen Tool is a local web interface for the MIT-licensed [`gosom/google-maps-scraper`](https://github.com/gosom/google-maps-scraper). It geocodes a place, starts a conservative search job, shows the results, and exports the original CSV. Developed by [SNIPER](https://www.linkedin.com/in/sniper97/).

## Branding and copyright year

The responsive layout uses a locally bundled Bootstrap 5.3.8. The Syncaxis logo is
served locally. `network_year.py` obtains the copyright year from TimeAPI over HTTPS,
with Cloudflare internet time as a fallback, using the Asia/Kolkata time zone.
`/api/time` refreshes that year hourly; `web/branding.js` updates open pages hourly
and when a page becomes visible. Neither the browser nor host wall clock supplies
the copyright year. If internet time is unavailable, the last verified year is
retained in the `leadgen_meta` Docker volume; a first-time offline start shows 2026.

## Run

```bash
docker compose up --build -d
curl http://127.0.0.1:3000/api/health
```

Open `http://127.0.0.1:3000`. Stop it with `docker compose down`.

### LAN access

The safe default listens only on localhost. To serve the UI from a specific LAN address, create a `.env` file next to `docker-compose.yml`:

```dotenv
HOST_BIND=192.168.3.9
```

Then recreate the web container:

```bash
docker compose up --build -d --force-recreate
```

Other devices on the same network can open `http://192.168.3.9:3000`. Allow inbound TCP port 3000 in the host firewall for the private network profile. The scraper API remains internal to Docker and is not published.

`GOOGLE_MAPS_API_KEY` is optional. When present with the **Geocoding API** enabled, it resolves typed locations server-side and is never sent to the browser. Without a key, Atlas tries OpenStreetMap Nominatim. If public geocoding is blocked or offline, users can choose **Use my location** (browser geolocation) or enter latitude/longitude manually; those paths require no geocoding API or key.

Business results use a scraper rather than Google's official Places API and can be rate-limited. Start with depth 3–5, avoid repeated bulk jobs, follow Google's terms and applicable privacy/marketing laws, and verify results before using them.

## Architecture

- `app.py`: dependency-free Python web server, API gateway, input validation, and Nominatim geocoding.
- `web/`: responsive frontend.
- `google-maps-scraper`: server-side scraper container; it is not directly exposed to the host.

The app itself is bound to `127.0.0.1` by default. `HOST_BIND` can expose it to a trusted LAN. Add authentication and TLS before exposing it to the public Internet.

## Company industry from job postings

The tool can infer a company's industry (genre) from the job postings it has
advertised in a given city. It searches one or more job sources, then classifies
the company from the roles' titles and descriptions - keyword heuristics first,
with an optional LLM fallback for ambiguous cases.

Every source has a free tier, so the tool can run entirely on free options: the ATS boards, LinkedIn's guest endpoint and SerpAPI need no payment (SerpAPI gives 250 searches/month free), and Adzuna and Jooble issue free keys.

### Sources

Selected by `JOB_SOURCES` (comma separated) or per request via the `sources`
field. Credentials live in the environment:

| Source | Needs | Notes |
| --- | --- | --- |
| `ats` | nothing | Keyless public Greenhouse/Lever/Ashby boards - no scraping, no ToS issue. Needs the company slug. |
| `linkedin` | nothing | Scrapes LinkedIn's public guest endpoint. Rate limited and **against LinkedIn's User Agreement** - prefer an API source. |
| `adzuna` | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | Free tier; India via `ADZUNA_COUNTRY=in`. |
| `jooble` | `JOOBLE_API_KEY` | Free key on request; 60+ countries. |
| `jobspy` | `python-jobspy` package | Optional multi-board library: LinkedIn/Indeed/Glassdoor/Google/Naukri. Same ToS caveats as scraping. |
| `serpapi` | `SERPAPI_API_KEY` | Google Jobs panel; aggregates LinkedIn/Indeed listings. |

### Endpoints

- `GET /api/job-sources` - which sources are configured, with their limits.
- `POST /api/job-search` - raw search: `{"keyword": "design engineer", "city": "Pune", "pages": 2, "sources": ["adzuna"]}`.
- `POST /api/company-genre` - `{"company": "Portescap", "city": "Pune"}` returns the inferred genre, confidence, the method used, and the sample titles behind it.
- `POST /api/company-match` - `{"company": "Hinduja Tech", "keyword": "hardware", "city": "Pune"}` returns a yes/no verdict on whether the company is actually in that field, with the evidence behind it.

### How much the bot can pull per city

LinkedIn's guest endpoint returns ~25 results per page and caps a single
keyword+city query at ~1,000 results (40 pages). Pace it: keep to 2-5s jittered
delays and under 10-12 pages/minute, or you will start seeing HTTP 429s. On one
residential IP, practitioners keep to ~20-30 requests/day (or ~500/hour with a
rotating proxy pool), so a full 1,000-result city window should be spread out or
split by posted-date/seniority/function. The API sources replace this ban risk
with a quota: Adzuna allows 25 calls/min and 250/day, Jooble sets per-key limits,
and SerpAPI allows 250 searches/month on the free plan.

### LLM fallback (optional)

Set `LLM_API_KEY` (and optionally `LLM_BASE_URL`, `LLM_MODEL`) to enable an
OpenAI-compatible classification call when the heuristic confidence is low.
Without it, the heuristic answer is used.

### UI

The field check is built into the main search form: enter a **Company**
(optional) alongside your keyword and location, and the same "Find businesses"
run also checks whether that company is actually in the field your keyword
describes. The verdict (yes / no / unknown, with confidence and the matching job
titles) appears under the form, and a matching company is dropped on the map.

### Map view

The map shows the whole search visually:

- the **selected location** as a marker, with the **search radius** drawn as a
  circle that follows the radius slider;
- every collected lead as a **numbered marker** - the number matches the `#`
  column in the results table, so a row and its pin are easy to pair up;
- the **shortlisted company** (when the field check returns a match) as a
  separate numbered marker, geocoded from its posting location.

The provider is chosen by `GET /api/map-config` from `MAP_PROVIDER`
(`auto` | `google` | `here` | `osm`):

- **google** - needs `GOOGLE_MAPS_API_KEY` with the Maps JavaScript API enabled;
- **here** - needs `HERE_API_KEY`; HERE raster tiles are served through the
  locally vendored Leaflet;
- **osm** - keyless OpenStreetMap through the same Leaflet.

`auto` prefers Google, then HERE, then OpenStreetMap; a provider whose key is
missing falls back to OpenStreetMap. The map key is sent to the browser (the map
library needs it), so restrict it by HTTP referrer / allowed domain in the
provider console. Any provider needs outbound internet at runtime - the same
requirement as geocoding.

**HERE free tier:** a HERE platform account includes a free monthly allowance
(250,000 requests). Sign up, register an app in Access Manager, and generate an
API key; a payment method is required on file but you are not charged while
usage stays inside the free tier.

### Field check

`POST /api/company-match` answers the question "is this company actually in the
field my keyword describes?". It searches the company's postings in the city,
then checks whether the keyword - or, when the keyword names a whole industry,
that industry's terms - appears in them. A posting naming the field in its
**title** is the strongest signal; two or more mentions anywhere also count. A
specific term such as "welding" is not broadened to its whole category, so it
will not match unrelated roles.

The response gives `matches` (true / false / null when there are no postings),
`confidence`, `matched_postings`, `title_matches`, the expanded `field_terms`,
and an `evidence` list of the postings that matched.

Matching is title-and-text based. LinkedIn's guest endpoint returns no
description, so set `JOBSPY_FETCH_DESCRIPTION=true` (slower) to give the matcher
more text, and prefer the `ats` source where descriptions are included.

### De-duplication

`search_jobs` merges the same posting seen on more than one source into a single
combined record: the longest description wins, the other fields fill in from
whichever source had them, and every source is listed under `sources`. The API
reports `postings_found` (raw), `postings_unique`, and `duplicates_merged`.

### Building

Three requirement files, all optional in code:

- `requirements.txt` (always installed) - the robustness layer: `curl_cffi`
  (browser TLS/JA3 impersonation), `tenacity` (retry with backoff on 429/5xx),
  and `beautifulsoup4` + `lxml` (HTML parsing).
- `requirements-heavy.txt` - `playwright`, the heaviest tier: headless Chromium
  that renders the LinkedIn guest endpoint in a real browser. The image also
  runs `playwright install --with-deps chromium`. Skip it with
  `--build-arg INSTALL_PLAYWRIGHT=false` to fall back to curl_cffi.
- `requirements-optional.txt` - `python-jobspy` (pulls pandas/numpy). Skip it
  with `--build-arg INSTALL_JOBSPY=false`.

The base image is `python:3.12-slim` (native wheels need glibc, not alpine).
Every package is optional in code: `jobsources.py` degrades to the standard
library if one is missing, so the app runs regardless.

### Deploying from GitHub Actions

`.github/workflows/deploy.yml` builds the image on every push to `main` and
publishes it to GHCR. It also has a manual **deploy** job that runs on a
self-hosted runner and injects the map key from a repository secret:

1. Repo -> Settings -> Secrets and variables -> Actions -> add `HERE_API_KEY`.
2. Register a self-hosted runner on the server and give it the label `syncaxis`.
3. Actions -> "Build and deploy" -> Run workflow -> tick **deploy**.

The deploy job writes `HERE_API_KEY` into `.env` (which is gitignored) and runs
`docker compose up -d --build`. The secret value is never written to a tracked
file and never appears in the repository. Without a self-hosted runner the
deploy job stays queued, so leave the **deploy** box unticked if you deploy by
hand.


## Credits

Inspired by the MIT-licensed [Google Maps Scraper Kit](https://github.com/Mahanaicoach/google-maps-scraper-kit). The underlying scraper is by Georgios Komninos and is also MIT-licensed.
