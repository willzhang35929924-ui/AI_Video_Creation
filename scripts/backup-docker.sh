#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/docker-common.sh"
usage() {
  echo 'Usage: bash scripts/backup-docker.sh [--helper-image LOCAL_IMAGE] [DESTINATION] [compose.yaml] [project-name]'
  echo 'BACKUP_HELPER_IMAGE may also explicitly select the local helper. Default: current backend image; no images are pulled.'
}
helper_image="${BACKUP_HELPER_IMAGE:-}"
destination=backups
compose_file=compose.yaml
project_name=''
position=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --helper-image)
      [ "$#" -ge 2 ] && [ -n "$2" ] || { usage >&2; exit 2; }
      helper_image="$2"; shift 2; continue ;;
    --help|-h) usage; exit 0 ;;
    --*) usage >&2; exit 2 ;;
  esac
  case "$position" in
    0) destination="$1" ;;
    1) compose_file="$1" ;;
    2) project_name="$1" ;;
    *) usage >&2; exit 2 ;;
  esac
  position=$((position + 1))
  shift
done
cd "$DOCKER_PROJECT_ROOT"
backend="$(service_container backend)"
frontend="$(service_container frontend)"
[ -n "$backend" ] || { echo 'No existing backend container; nothing has been backed up.' >&2; exit 1; }
# Resolve to one immutable image and prove its CLI works before any stop/write.
archive_image="$(resolve_archive_image "$backend" "$helper_image")"
mkdir -p "$destination"
destination="$(cd "$destination" && pwd)"
archive="$destination/video-factory-$(date -u +%Y%m%d-%H%M%SZ)-$$.tar.gz"
partial="$archive.partial"
[ ! -e "$archive" ] && [ ! -e "$partial" ] || { echo 'Backup filename already exists.' >&2; exit 1; }
backend_stopped=false
frontend_stopped=false
cleanup() {
  local previous_status=$?
  trap - EXIT
  if [ "$backend_stopped" = true ]; then docker start "$backend" >&2 || previous_status=1; fi
  if [ "$frontend_stopped" = true ]; then docker start "$frontend" >&2 || previous_status=1; fi
  if [ -n "$partial" ] && [ -f "$partial" ]; then rm -f -- "$partial"; fi
  exit "$previous_status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM
if container_running "$frontend"; then
  frontend_stopped=true
  docker stop --timeout 30 "$frontend"
fi
if container_running "$backend"; then
  archive_helper "$backend" check-idle "$archive_image" </dev/null
  backend_stopped=true
  docker stop --timeout 60 "$backend"
fi
# Shell redirection preserves raw bytes on macOS/Linux; no host Python is needed.
archive_helper "$backend" backup "$archive_image" </dev/null >"$partial"
mv -- "$partial" "$archive"
partial=''
printf '%s  %s\n' "$(sha256_of "$archive")" "$(basename "$archive")" >"$archive.sha256"
echo "Private backup complete: $archive"
echo 'Contains database, master key and media. Never upload this archive or checksum to GitHub.'
