FROM python:3.12-slim
WORKDIR /app
COPY app.py network_year.py jobsources.py genre.py ./
COPY web ./web
COPY requirements.txt requirements-optional.txt ./
# Light runtime packages (curl_cffi, tenacity, bs4, lxml) are always installed.
RUN pip install --no-cache-dir -r requirements.txt
# python-jobspy pulls pandas/numpy (glibc wheels), so the base image is slim, not
# alpine. Skip it with `--build-arg INSTALL_JOBSPY=false`.
ARG INSTALL_JOBSPY=true
RUN if [ "$INSTALL_JOBSPY" = "true" ]; then pip install --no-cache-dir -r requirements-optional.txt; fi
ENV YEAR_CACHE_FILE=/var/lib/leadgen/year.json
ENV PORT=3000 SCRAPER_BASE_URL=http://google-maps-scraper:8080
EXPOSE 3000
CMD ["python3", "app.py"]
