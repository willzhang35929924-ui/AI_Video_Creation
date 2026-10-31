#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/docker-common.sh"
[ "$#" -ge 1 ] || { echo 'Usage: bash scripts/restore-docker.sh ARCHIVE [compose.yaml] [project-name]' >&2; exit 2; }
archive="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
compose_file="${2:-compose.yaml}"
project_name="${3:-}"
[ -f "$archive.sha256" ] || { echo 'Missing .sha256 sidecar; refuse an unverified restore.' >&2; exit 1; }
expected="$(awk 'NR==1 {print $1}' "$archive.sha256")"
[[ "$expected" =~ ^[a-fA-F0-9]{64}$ ]] && [ "$(sha256_of "$archive")" = "$(printf '%s' "$expected" | tr 'A-F' 'a-f')" ] || {
  echo 'Archive SHA256 does not match. Nothing has been restored.' >&2; exit 1;
}
cd "$DOCKER_PROJECT_ROOT"
backend="$(service_container backend)"
frontend="$(service_container frontend)"
if container_running "$backend" || container_running "$frontend"; then
  echo 'Restore requires stopped, empty target containers. Choose a NEW unused COMPOSE_PROJECT_NAME; existing data will never be overwritten.' >&2
  exit 1
fi
if [ -z "$backend" ]; then
  if [ -n "$project_name" ]; then
    docker compose -f "$compose_file" -p "$project_name" create
  else
    docker compose -f "$compose_file" create
  fi
  backend="$(service_container backend)"
fi
[ -n "$backend" ] || { echo 'Could not create the empty backend target.' >&2; exit 1; }
archive_helper "$backend" check-empty </dev/null
archive_helper "$backend" restore <"$archive"
echo 'Restore complete. Start the same Compose project normally; required migrations run automatically.'
