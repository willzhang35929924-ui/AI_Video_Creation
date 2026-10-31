#!/usr/bin/env bash
# Sourced by Docker-only scripts; compatible with macOS's bundled Bash 3.2.
set -euo pipefail
DOCKER_PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

service_container() {
  local service="$1" result
  if [ -n "${project_name:-}" ]; then
    result="$(docker ps --all --quiet --filter "label=com.docker.compose.project=$project_name" --filter "label=com.docker.compose.service=$service")"
  else
    result="$(docker compose -f "$compose_file" ps --all --quiet "$service")"
  fi
  if [[ "$result" == *$'\n'* ]]; then
    echo "Multiple $service containers found; this SQLite deployment requires one backend." >&2
    return 1
  fi
  printf '%s' "$result"
}

container_running() {
  [ -n "$1" ] && [ "$(docker inspect --format '{{.State.Running}}' "$1")" = true ]
}

resolve_archive_image() {
  local container="$1" reference="${2:-}" metadata image os architecture engine help_text command
  local usage='Use --helper-image LOCAL_IMAGE (or BACKUP_HELPER_IMAGE) with an explicitly reviewed, locally installed compatible backend image. No image is pulled and no service has been stopped.'
  if [ -z "$reference" ]; then
    reference="$(docker inspect --format '{{.Image}}' "$container")" || { echo "Cannot resolve the existing backend image. $usage" >&2; return 1; }
  fi
  if ! metadata="$(docker image inspect --format '{{.Id}} {{.Os}} {{.Architecture}}' "$reference" 2>/dev/null)"; then
    echo "Backup helper image is unavailable locally. A running container may outlive its removed image. $usage" >&2
    return 1
  fi
  read -r image os architecture <<<"$metadata"
  if ! [[ "$image" =~ ^sha256:[a-f0-9]{64}$ ]] || [ "$os" != linux ] || { [ "$architecture" != amd64 ] && [ "$architecture" != arm64 ]; }; then
    echo "Backup helper must be a Linux AMD64 or ARM64 image. $usage" >&2
    return 1
  fi
  engine="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')" || { echo "A reachable Linux Docker engine is required. $usage" >&2; return 1; }
  case "$engine" in linux/amd64|linux/arm64) ;; *) echo "A Linux Docker engine is required. $usage" >&2; return 1 ;; esac
  [ -f "$DOCKER_PROJECT_ROOT/docker/volume_archive.py" ] || { echo 'Missing public docker/volume_archive.py helper.' >&2; return 1; }
  # No business mounts or application startup: test the actual archive CLI and
  # its Python stdlib. Cross-architecture images must genuinely run this probe.
  if ! help_text="$(docker run --rm --pull never --network none --read-only --user 10001:10001 \
      --cap-drop ALL --security-opt no-new-privileges:true \
      --mount "type=bind,\"source=$DOCKER_PROJECT_ROOT/docker/volume_archive.py\",target=/opt/volume_archive.py,readonly" \
      --entrypoint python "$image" /opt/volume_archive.py --help 2>/dev/null)"; then
    echo "Backup helper could not run the required archive CLI on this Docker engine. $usage" >&2
    return 1
  fi
  for command in check-idle check-empty backup restore; do
    case "$help_text" in *"$command"*) ;; *) echo "Backup helper lacks the required archive CLI. $usage" >&2; return 1 ;; esac
  done
  echo "Backup helper validated: $image ($os/$architecture); engine $engine." >&2
  printf '%s' "$image"
}

archive_helper() {
  local container="$1" command="$2" image="${3:-}"
  if [ -z "$image" ]; then image="$(docker inspect --format '{{.Image}}' "$container")"; fi
  # Docker's CSV --mount syntax handles spaces and commas without shell evaluation.
  docker run --rm -i --pull never --network none --read-only --user 10001:10001 \
    --cap-drop ALL --security-opt no-new-privileges:true --volumes-from "$container" \
    --mount "type=bind,\"source=$DOCKER_PROJECT_ROOT/docker/volume_archive.py\",target=/opt/volume_archive.py,readonly" \
    --entrypoint python "$image" /opt/volume_archive.py "$command"
}

sha256_of() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}
