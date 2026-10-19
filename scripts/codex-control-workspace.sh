#!/usr/bin/env bash
# Create a Codex control workspace: a separate folder whose project config makes the
# outer assistant read-only and registers the Factory MCP server for one fixed target.
set -euo pipefail

usage() {
  echo "usage: $0 --target /abs/target --control /abs/control-dir [--storage /abs/factory-home]" >&2
  exit 2
}

target="" control="" storage=""
while [ $# -gt 0 ]; do
  case "$1" in
    --target) target="${2:-}"; shift 2 ;;
    --control) control="${2:-}"; shift 2 ;;
    --storage) storage="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$target" ] && [ -n "$control" ] || usage
case "$target$control$storage" in *'"'*|*\\*) echo "paths must not contain quotes or backslashes" >&2; exit 2 ;; esac
for path in "$target" "$control" ${storage:+"$storage"}; do
  case "$path" in /*) ;; *) echo "path must be absolute: $path" >&2; exit 2 ;; esac
done
[ -f "$target/.factory/factory.yaml" ] || { echo "target has no .factory/factory.yaml: $target" >&2; exit 1; }

repo="$(cd "$(dirname "$0")/.." && pwd)"
executable="$repo/.venv/bin/software-factory"
[ -x "$executable" ] || { echo "missing $executable; run uv sync first" >&2; exit 1; }
target="$(cd "$target" && pwd -P)"
case "$control/" in "$target"/*) echo "control folder must be outside the target" >&2; exit 1 ;; esac

args="\"mcp\", \"--workspace\", \"$target\""
[ -z "$storage" ] || args="\"--storage\", \"$storage\", $args"

mkdir -p "$control/.codex" "$control/.agents/skills"
# Legacy sandbox keys win over permission profiles whenever any loaded layer sets
# sandbox_mode, so a closer project layer must set sandbox_mode itself.
cat > "$control/.codex/config.toml" <<EOF
sandbox_mode = "read-only"
approval_policy = "never"

[mcp_servers.software-factory]
command = "$executable"
args = [$args]
startup_timeout_sec = 60
tool_timeout_sec = 120
# approval_policy "never" also denies MCP tool calls; pre-approve only these tools.
default_tools_approval_mode = "approve"
EOF
rm -rf "$control/.agents/skills/factory-control"
cp -R "$repo/skills/factory-control" "$control/.agents/skills/factory-control"
cat > "$control/AGENTS.md" <<EOF
# Software Factory control workspace

This folder controls Software Factory runs for \`$target\`.
Use the factory-control skill and the \`factory_*\` tools. Do not edit the target here.
EOF
echo "Control workspace ready: $control"
echo "Open it in Codex and trust the project so its .codex/config.toml loads."
