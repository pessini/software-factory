#!/usr/bin/env bash
# Run the real repository quality command under the worker's isolation settings.
# No provider credentials, Docker socket, or controller data enter the container.
set -euo pipefail
cd "$(dirname "$0")/.."
image="${1:-software-factory-rust-worker:dev}"
mkdir -p .checks
fixture="$(mktemp -d "$PWD/.checks/rust-project-worker-XXXXXX")"
trap 'rm -rf -- "$fixture"' EXIT
mkdir "$fixture/workspace" "$fixture/home"
git archive HEAD | tar -x -C "$fixture/workspace"
image_id="$(docker image inspect --format '{{.Id}}' "$image")"
source_sha="$(git rev-parse HEAD)"
docker run --rm --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  --security-opt "seccomp=$PWD/containers/worker-seccomp.json" \
  --pids-limit 256 --memory 2g --cpus 2 --user "$(id -u):$(id -g)" \
  --tmpfs /tmp:rw,nosuid,nodev,mode=1777,size=512m \
  --mount "type=bind,src=$fixture/workspace,dst=/workspace,readonly" \
  --mount "type=bind,src=$fixture/home,dst=/factory/provider" \
  --env HOME=/factory/provider --env GIT_OPTIONAL_LOCKS=0 \
  --env PYTHONDONTWRITEBYTECODE=1 --workdir /workspace \
  --entrypoint factory-rust-check "$image_id"
printf '{"passed":true,"source":"%s","image":"%s","network":"none","workspace_read_only":true}\n' \
  "$source_sha" "$image_id"
