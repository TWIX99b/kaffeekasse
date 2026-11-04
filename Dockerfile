FROM python:3.12-slim

# Versionsnummer für die Fußzeile – setzt der GitHub-Build aus dem Tag (v1.2.0 …)
ARG KAFFEEKASSE_VERSION=""
ENV KAFFEEKASSE_VERSION=${KAFFEEKASSE_VERSION}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TZ=Europe/Berlin

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py qr.py ./
COPY static ./static

RUN useradd --uid 1000 --create-home kaffeekasse \
 && mkdir -p /data && chown kaffeekasse:kaffeekasse /data
USER kaffeekasse
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)" || exit 1

# 1 Worker + Threads: reicht für eine Kaffeekasse, SQLite bleibt unkompliziert
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--access-logfile", "-", "app:app"]
