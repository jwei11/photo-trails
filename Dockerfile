# One image for every app process: the API (the default command), the worker, migrations,
# and the place-data load, which compose.yaml runs with different commands.
#
# Three stages. Only the last becomes the image; the first two are thrown away, along with
# Node, npm's cache, and the C compiler. Base images are pinned by digest (each covers amd64 and arm64)
# so a rebuild months from now gets the same bytes, not whatever the tag points to then.

# ---- 1. Build the React app. Its output is static files, so Node never reaches the image.
FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS frontend
WORKDIR /src
# Dependencies first: this layer is reused until package-lock.json changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 2. Install Python packages into a virtualenv that the last stage copies whole.
FROM python:3.13-slim-bookworm@sha256:5024f48ba9441d4b13a95d3945abc6365538e3a31109833367a1923523c6efed AS python-deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# A C compiler, for this stage only: timezonefinder publishes Linux wheels for x86_64 but
# not arm64, so its small C extension is built from source here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
COPY requirements.txt .
# Prebuilt wheels for everything except the packages named in --no-binary. A new
# dependency that needs compiling fails the build here, rather than slipping through.
RUN /opt/venv/bin/pip install --only-binary=:all: --no-binary=timezonefinder -r requirements.txt

# ---- 3. The image that runs.
FROM python:3.13-slim-bookworm@sha256:5024f48ba9441d4b13a95d3945abc6365538e3a31109833367a1923523c6efed
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# A non-root user with a fixed ID, so file ownership on volumes is predictable.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=python-deps /opt/venv /opt/venv
COPY alembic.ini ./
COPY migrations/ migrations/
COPY app/ app/
COPY --from=frontend /src/dist frontend/dist/
# Code stays owned by root, so the app can't modify itself; it's compiled to bytecode now
# because it can't write __pycache__ at runtime. The one writable folder is for downloads
# of place data (a volume in compose).
RUN python -m compileall -q app migrations \
    && mkdir -p data/geo && chown app:app data/geo
USER app

EXPOSE 8000
# Python's own urllib, since slim has no curl. The worker has its own check in compose.yaml.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
# Exec form (a JSON list): uvicorn is PID 1 and receives docker stop's SIGTERM itself.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
