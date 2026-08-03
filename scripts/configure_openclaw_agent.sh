#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX_NAME="${NEMOCLAW_SANDBOX:-urban-dossier-agent}"
NEMOCLAW_BIN="${NEMOCLAW_BIN:-$(command -v nemoclaw || true)}"

if [[ -z "$NEMOCLAW_BIN" || ! -x "$NEMOCLAW_BIN" ]]; then
  echo "ERROR: nemoclaw not found; set NEMOCLAW_BIN to its executable path" >&2
  exit 2
fi

"$NEMOCLAW_BIN" "$SANDBOX_NAME" agents apply \
  -f "$REPO_ROOT/deploy/openclaw/agents.yaml" --yes --non-interactive
"$NEMOCLAW_BIN" "$SANDBOX_NAME" upload \
  "$REPO_ROOT/deploy/openclaw/urban-dossier/." \
  /sandbox/.openclaw/workspace-urban-dossier

oc_set() {
  "$NEMOCLAW_BIN" "$SANDBOX_NAME" exec -- \
    openclaw config set "$1" "$2" --strict-json
}

oc_set agents.list.0.default false
oc_set agents.list.1.default true
oc_set agents.list.1.thinkingDefault '"off"'
oc_set agents.list.1.reasoningDefault '"off"'
oc_set agents.list.1.skills '[]'
oc_set agents.list.1.tools \
  '{"profile":"minimal","allow":["session_status"],"deny":["group:fs","group:runtime","group:web","group:messaging","browser","canvas","sessions_spawn","sessions_send","agents_list","subagents","tool_search"]}'
oc_set tools.toolSearch false
oc_set plugins.allow '["nemoclaw"]'
oc_set gateway.http.endpoints.responses \
  '{"enabled":true,"files":{"allowUrl":false},"images":{"allowUrl":false}}'

"$NEMOCLAW_BIN" "$SANDBOX_NAME" gateway restart --quiet
"$NEMOCLAW_BIN" "$SANDBOX_NAME" recover
"$REPO_ROOT/scripts/refresh_openclaw_gateway_token.sh"
