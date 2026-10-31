# syntax=docker/dockerfile:1.7
# The build context is the repository root; no local .env or runtime data is copied.
# Images: https://hub.docker.com/_/node and
# https://hub.docker.com/r/nginx/nginx-unprivileged
ARG NODE_IMAGE=node:24.22.1-alpine3.23
ARG NGINX_IMAGE=nginx/nginx-unprivileged:1.30.6-alpine

FROM ${NODE_IMAGE} AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund

COPY frontend/index.html frontend/tsconfig*.json frontend/vite.config.ts frontend/vitest.config.ts frontend/tailwind.config.ts frontend/postcss.config.cjs ./
COPY frontend/src ./src
COPY frontend/public ./public
# A same-origin API prevents Docker service names or API credentials entering JS.
ENV VITE_API_BASE_URL=""
RUN npm run build

FROM ${NGINX_IMAGE} AS runtime
COPY --chown=101:101 frontend/nginx.conf /etc/nginx/nginx.conf
COPY --from=build --chown=101:101 /app/dist /usr/share/nginx/html
COPY --chown=101:101 docker/licenses /usr/share/doc/video-factory
COPY --chown=101:101 licenses /usr/share/doc/video-factory/licenses
COPY --chown=101:101 THIRD_PARTY_NOTICES.md /usr/share/doc/video-factory/THIRD_PARTY_NOTICES.md
ARG APP_VERSION=unreleased
ARG VCS_REF=unknown
ARG VCS_SOURCE=""
ARG BUILD_DATE=""
ARG APP_LICENSE=MIT
LABEL org.opencontainers.image.title="Video Factory Frontend" \
    org.opencontainers.image.description="React application served by unprivileged Nginx; no model weights or private runtime data" \
    org.opencontainers.image.version="${APP_VERSION}" \
    org.opencontainers.image.revision="${VCS_REF}" \
    org.opencontainers.image.source="${VCS_SOURCE}" \
    org.opencontainers.image.created="${BUILD_DATE}" \
    org.opencontainers.image.licenses="${APP_LICENSE}"
USER 101:101
EXPOSE 8080
STOPSIGNAL SIGQUIT
HEALTHCHECK --start-period=10s --interval=30s --timeout=5s --retries=3 \
  CMD wget -q -O - http://127.0.0.1:8080/health | grep -qx 'video-factory-frontend' || exit 1
# No entrypoint template writes are needed, so the runtime can use a read-only rootfs.
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
