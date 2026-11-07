#!/usr/bin/env bash
# Clean release-image smoke test. Only Docker, curl (inside image), and host jq.
set -euo pipefail
image=${1:-software-factory:issue-25}
prefix="factory-native-$(date +%s)-$$"
service="$prefix-service"
data="$prefix-data"
workspace="$prefix-workspace"
cleanup() {
  docker rm -f "$service" >/dev/null 2>&1 || true
  docker volume rm "$data" "$workspace" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# The image itself has no interpreter, uv, Python package or source checkout.
docker run --rm --entrypoint sh "$image" -ec '
  ! command -v python; ! command -v python3; ! command -v uv
  software-factory --version | grep "Rust/ADK"
  test ! -e /build/src/software_factory
'
docker volume create "$data" >/dev/null
docker volume create "$workspace" >/dev/null
docker run --rm --entrypoint sh -v "$workspace:/workspace" "$image" -ec '
  software-factory init -w /workspace
  # Explicit fake-catalog model is solely for this protocol smoke test.
  sed -i "s/provider: codex/provider: codex, model: gpt-5.4/g" /workspace/.factory/factory.yaml
'
docker run -d --name "$service" --entrypoint software-factory \
  -v "$data:/data" -v "$workspace:/workspace" \
  -e FACTORY_HTTP_TOKEN=native-release-smoke-token-123456789 \
  -e FACTORY_ALLOW_FAKE_WORKER=1 -e FACTORY_CLIENT_HOME=/tmp/native-clients \
  "$image" web --storage /data -w /workspace >/dev/null
ready=0
for _ in $(seq 1 100); do
  if docker exec "$service" curl -fsS http://127.0.0.1:4317/health/ready >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.1
done
[ "$ready" = 1 ] || { docker logs "$service"; exit 1; }
docker exec "$service" software-factory doctor -w /workspace | jq -e '.runtime == "Rust/ADK"' >/dev/null
plan=$(docker exec "$service" software-factory plan 'Native release smoke' --request-id smoke -w /workspace)
sid=$(jq -er '.session_id' <<<"$plan")
docker exec "$service" software-factory plan-status "$sid" -w /workspace | jq -e --arg sid "$sid" '.session_id == $sid' >/dev/null
# Browser assets require the same auth; no Python source mount supplies them.
docker exec "$service" sh -ec '
  for path in / /static/app.js /static/app.css /static/geist-latin.woff2 /static/geist-LICENSE.txt; do
    curl -fsS -H "Authorization: Bearer $FACTORY_HTTP_TOKEN" "http://127.0.0.1:4317$path" -o /dev/null
  done
'
mcp=$(printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}' \
  "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"factory_plan_status\",\"arguments\":{\"session_id\":\"$sid\"}}}" \
  | docker exec -i "$service" software-factory mcp -w /workspace)
jq -se --arg sid "$sid" '.[1].result.isError == false and (.[1].result.content[0].text | fromjson | .session_id == $sid)' <<<"$mcp" >/dev/null
acp=$(printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}' \
  '{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/workspace","mcpServers":[]}}' \
  | docker exec -i "$service" software-factory acp -w /workspace)
chat=$(jq -ser '.[1].result.sessionId' <<<"$acp")
printf '%s\n' "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"session/prompt\",\"params\":{\"sessionId\":\"$chat\",\"prompt\":[{\"type\":\"text\",\"text\":\"/plan-status $sid\"}]}}" \
  | docker exec -i "$service" software-factory acp -w /workspace \
  | jq -se --arg sid "$sid" 'any(.[]; .method == "session/update" and (.params.update.content.text | contains($sid)))' >/dev/null
printf '%s\n' '{"native_release":true,"python":false,"uv":false,"cli":true,"browser":true,"acp":true,"mcp":true}'
