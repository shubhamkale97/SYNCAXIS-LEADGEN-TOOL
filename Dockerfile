FROM python:3.12-slim
WORKDIR /app
COPY app.py network_year.py jobsources.py genre.py ./
COPY web ./web
COPY requirements.txt ./
# python-jobspy pulls pandas/numpy, which need a glibc base image (slim, not
# alpine). Set INSTALL_JOBSPY=false to skip the optional multi-board source.
ARG INSTALL_JOBSPY=true
RUN if [ "$INSTALL_JOBSPY" = "true" ]; then pip install --no-cache-dir -r requirements.txt; fi
ENV YEAR_CACHE_FILE=/var/lib/leadgen/year.json
ENV PORT=3000 SCRAPER_BASE_URL=http://google-maps-scraper:8080
EXPOSE 3000
CMD ["python3", "app.py"]
