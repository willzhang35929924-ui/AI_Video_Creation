#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/docker-common.sh"
case "${1:-}" in
  '') compose_file=compose.yaml ;;
  --dev) compose_file=compose.dev.yaml ;;
  *) echo 'Usage: bash scripts/start.sh [--dev]' >&2; exit 2 ;;
esac
cd "$DOCKER_PROJECT_ROOT"
engine_os="$(docker info --format '{{.OSType}}')" || {
  echo 'Start Docker Desktop and wait for its engine to be ready, then retry.' >&2; exit 1;
}
[ "$engine_os" = linux ] || { echo 'Switch Docker Desktop to Linux containers.' >&2; exit 1; }
docker compose -f "$compose_file" up -d --wait --wait-timeout 180
echo "Open http://$(docker compose -f "$compose_file" port frontend 8080)"
