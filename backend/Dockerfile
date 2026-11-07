# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/backend \
    VIDEO_FACTORY_CONTAINER=1 \
    APP_ENV=production \
    APP_HOST=0.0.0.0 \
    APP_PORT=8765 \
    DATABASE_URL=sqlite:////app/data/video_factory.db \
    MEDIA_ROOT=/app/data/media \
    VIDEO_FACTORY_SECRET_KEY_FILE=/app/data/.secrets/master.key \
    SEED_DEMO_DATA=false \
    SEED_LOCAL_MODEL_SERVICES=false \
    H3_DEMO_MODE=false \
    FFMPEG_PATH=/usr/bin/ffmpeg \
    FFPROBE_PATH=/usr/bin/ffprobe

COPY docker/collect-corresponding-source.sh /usr/local/lib/video-factory-collect-source.sh
COPY docker/configure_apt.py /usr/local/lib/video-factory-configure-apt.py
ARG DEBIAN_MIRROR=https://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=https://deb.debian.org/debian-security
RUN python /usr/local/lib/video-factory-configure-apt.py \
    && apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates ffmpeg fonts-noto-cjk \
    && sh /usr/local/lib/video-factory-collect-source.sh \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 factory \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/factory factory

WORKDIR /app/backend
COPY backend/requirements-docker.lock ./requirements-docker.lock
ARG PYPI_INDEX_URL=https://pypi.org/simple
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import os,sys; from urllib.parse import urlsplit; u=urlsplit(os.environ['PYPI_INDEX_URL']); valid=u.scheme=='https' and bool(u.hostname) and not(u.username or u.password or u.query or u.fragment); sys.exit(0 if valid else 'PYPI_INDEX_URL must be an HTTPS package index without credentials')" \
    && python -m pip install --index-url "$PYPI_INDEX_URL" --timeout 60 --retries 5 --require-hashes --only-binary=:all: --requirement requirements-docker.lock

COPY backend/app ./app
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./alembic.ini
COPY workflows /app/workflows
COPY schemas /app/schemas
COPY prompts /app/prompts
COPY docs/templates /app/docs/templates
COPY docs/cinematic-production-standard.md /app/docs/cinematic-production-standard.md
COPY frontend/src /app/frontend/src
COPY tests/backend/test_cinematic.py /app/tests/backend/test_cinematic.py
# Fixed adapters are source only; no model, environment or local manifest is
# copied. Optional host-model deployment remains outside this CPU image.
COPY scripts /app/scripts
COPY docker /app/docker
COPY licenses /app/licenses
COPY LICENSE /app/LICENSE
COPY THIRD_PARTY_NOTICES.md /app/THIRD_PARTY_NOTICES.md

RUN mkdir -p /app/data/media /app/data/.secrets /app/logs /app/reports \
    && chown -R factory:factory /app/data /app/logs /app/reports \
    && chmod 700 /app/data/.secrets

# Release metadata is deliberately after dependencies: a new tag/timestamp must
# not invalidate apt, corresponding-source downloads or pip installation.
ARG APP_VERSION=unreleased
ARG VCS_REF=unknown
ARG VCS_SOURCE=""
ARG BUILD_DATE=""
ARG APP_LICENSE=Apache-2.0
LABEL org.opencontainers.image.title="Video Factory Backend" \
    org.opencontainers.image.description="FastAPI, SQLite, workflow workers and FFmpeg; model inference runs in separately configured services" \
    org.opencontainers.image.version="${APP_VERSION}" \
    org.opencontainers.image.revision="${VCS_REF}" \
    org.opencontainers.image.source="${VCS_SOURCE}" \
    org.opencontainers.image.created="${BUILD_DATE}" \
    org.opencontainers.image.licenses="${APP_LICENSE}"
ENV VIDEO_FACTORY_VERSION=${APP_VERSION} VIDEO_FACTORY_REVISION=${VCS_REF}
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=20s --timeout=8s --start-period=60s --retries=3 \
    CMD ["python", "-m", "app.container_entrypoint", "healthcheck"]
ENTRYPOINT ["python", "-m", "app.container_entrypoint"]
