# Hosted image: gunicorn, non-root, read-only root filesystem friendly.
# Built and run by deploy/docker-compose.yml; see deploy/README.md.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tzdata: the report's "generated" timestamp uses the container's TZ
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-hosted.txt .
RUN pip install -r requirements-hosted.txt

COPY app.py cli.py rba_refresh.py LICENSE ./
COPY engine/ engine/
COPY templates/ templates/
COPY static/ static/
COPY data/ data/
COPY demo/ demo/
COPY deploy/gunicorn.conf.py deploy/
RUN python -m compileall -q /app

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001:10001

ARG GIT_COMMIT=""
ENV IBKR_TAX_BUILD=${GIT_COMMIT}

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]
CMD ["gunicorn", "--config", "deploy/gunicorn.conf.py", "app:app"]
