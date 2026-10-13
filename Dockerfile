FROM python:3.14-slim

# Browsers live outside any home directory so the unprivileged user can run them.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

WORKDIR /app

# Dependencies first: these layers are reused until a requirements file changes,
# so editing application code does not reinstall packages (or Chromium).
COPY requirements.txt requirements-optional.txt requirements-heavy.txt ./
# Light runtime packages (curl_cffi, tenacity, bs4, lxml) are always installed.
RUN pip install -r requirements.txt
# Heaviest tier: headless Chromium for the LinkedIn source. Skip with
# `--build-arg INSTALL_PLAYWRIGHT=false` to fall back to curl_cffi.
ARG INSTALL_PLAYWRIGHT=true
RUN if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
      pip install -r requirements-heavy.txt && \
      playwright install --with-deps chromium; \
    fi
# Optional: python-jobspy pulls pandas/numpy (glibc wheels). Skip with
# `--build-arg INSTALL_JOBSPY=false`.
ARG INSTALL_JOBSPY=true
RUN if [ "$INSTALL_JOBSPY" = "true" ]; then pip install -r requirements-optional.txt; fi

# Unprivileged runtime user. /var/lib/leadgen holds the copyright-year and
# job-result caches (the leadgen_meta volume is mounted there).
RUN useradd --system --create-home --uid 10001 leadgen \
    && mkdir -p /var/lib/leadgen \
    && chown leadgen:leadgen /var/lib/leadgen

COPY *.py ./
COPY web ./web

# BIND_HOST is the address *inside* the container; which host address the port
# is published on is controlled by HOST_BIND in docker-compose.yml.
ENV YEAR_CACHE_FILE=/var/lib/leadgen/year.json \
    JOBS_CACHE_DB=/var/lib/leadgen/jobs-cache.sqlite \
    PORT=3000 \
    BIND_HOST=0.0.0.0 \
    SCRAPER_BASE_URL=http://google-maps-scraper:8080 \
    LINKEDIN_ENGINE=playwright

USER leadgen
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '3000'), timeout=3)"
CMD ["python3", "app.py"]
