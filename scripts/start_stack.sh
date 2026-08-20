#!/usr/bin/env bash
# Bring up the Urban Dossier local stack: one command, from any state.
#
#   scripts/start_stack.sh            backend (8090) + frontend (3456)
#   scripts/start_stack.sh --llm      ... and the production vLLM on :8000
#   scripts/start_stack.sh --agent    use the agent-enabled backend instead
#   scripts/start_stack.sh --status   report only, change nothing
#
# Installs (or refreshes) the user units from deploy/systemd/ first, so the
# topology lives on disk instead of in a `systemd-run` command line that has
# to be reconstructed from memory after every reboot.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_ROOT="${URBAN_DOSSIER_STATE_ROOT:-/mnt/data/urban-dossier-state}"
UNIT_DIR="${HOME}/.config/systemd/user"
WITH_LLM=0
WITH_AGENT=0
STATUS_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --llm) WITH_LLM=1 ;;
    --agent) WITH_AGENT=1 ;;
    --status) STATUS_ONLY=1 ;;
    -h|--help) sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n== %s\n' "$1"; }

# probe <label> <url> <attempts> -- poll until HTTP 200, report either way.
probe() {
  local label="$1" url="$2" attempts="${3:-30}" code
  code=$(curl -s -o /dev/null -w '%{http_code}' \
    --retry "$attempts" --retry-delay 2 --retry-connrefused \
    --max-time $((attempts * 2 + 10)) "$url" 2>/dev/null)
  if [[ "$code" == "200" ]]; then
    printf '  ok    %-28s %s\n' "$label" "$url"
    return 0
  fi
  printf '  FAIL  %-28s %s (HTTP %s)\n' "$label" "$url" "${code:-000}"
  return 1
}

if [[ $STATUS_ONLY -eq 0 ]]; then
  # Order matters. A transient unit of the same name (from `systemd-run`, the
  # way this stack used to be started) lives in /run and shadows the installed
  # one; systemd only notices the file on disk on a daemon-reload that happens
  # AFTER the transient unit is gone. Reloading first and stopping second --
  # the obvious order -- leaves systemd insisting the unit does not exist.
  systemctl --user stop ud-backend-noagent.service ud-frontend.service 2>/dev/null
  systemctl --user reset-failed ud-backend-noagent.service ud-frontend.service 2>/dev/null

  say "installing units into ${UNIT_DIR}"
  mkdir -p "$UNIT_DIR"
  for unit in ud-backend-noagent.service ud-frontend.service ud-stack.target; do
    ln -sfn "$REPO_ROOT/deploy/systemd/$unit" "$UNIT_DIR/$unit"
    echo "  $unit"
  done
  systemctl --user daemon-reload

  # Both backends bind 8090, so exactly one of them runs. Stop the other
  # before starting the one that was asked for.
  local_backend=ud-backend-noagent.service
  other_backend=urban-dossier-backend.service
  if [[ $WITH_AGENT -eq 1 ]]; then
    local_backend=urban-dossier-backend.service
    other_backend=ud-backend-noagent.service
  fi
  systemctl --user stop "$other_backend" 2>/dev/null

  say "starting ${local_backend%.service} + frontend"
  if ! systemctl --user start "$local_backend" ud-frontend.service; then
    echo "  start failed; see: journalctl --user -u ${local_backend%.service} -u ud-frontend" >&2
    if [[ $WITH_AGENT -eq 1 ]]; then
      # This unit gates on the OpenClaw gateway token in ExecStartPre, so it
      # refuses to start whenever the gateway or the sandbox is down.
      echo "  --agent needs a healthy gateway and sandbox; check:" >&2
      echo "    openshell status && nemoclaw urban-dossier-agent status" >&2
    fi
  fi

  if [[ $WITH_LLM -eq 1 ]]; then
    say "starting vLLM (production Nano, :8000)"
    # `llm` by name rather than the whole profile: the profile also carries
    # candidate-model services that must not come up on a normal start.
    docker compose --env-file "$STATE_ROOT/runtime/gpu.env" \
      -f "$REPO_ROOT/deploy/compose.gpu.yml" --profile inference up -d llm
    echo "  weights take ~1-2 min to load; probed below"
  fi
fi

say "health"
rc=0
probe "backend  :8090" http://127.0.0.1:8090/api/health 30 || rc=1
probe "frontend :3456" http://127.0.0.1:3456/ 20 || rc=1
probe "proxy    /api/health" http://127.0.0.1:3456/api/health 5 || rc=1
if [[ $WITH_LLM -eq 1 ]] || curl -s -o /dev/null --max-time 2 http://127.0.0.1:8000/v1/models; then
  probe "vllm     :8000" http://127.0.0.1:8000/v1/models 90 || rc=1
fi

# Agent side. Informational unless --agent was asked for: the everyday
# topology runs without the sandbox, and for four days in August nobody
# noticed the gateway was dead because nothing here ever looked at it.
say "agent stack"
agent_rc=0

gw_state="$(timeout 20 openshell status 2>&1 | sed 's/\x1b\[[0-9;]*m//g')"
if grep -q 'Status:.*Connected' <<<"$gw_state" \
  && grep -q 'Authentication:.*Authenticated' <<<"$gw_state"; then
  printf '  ok    %-28s %s\n' "gateway" \
    "$(grep -m1 'Version:' <<<"$gw_state" | tr -s ' ' | sed 's/^ //')"
else
  printf '  FAIL  %-28s %s\n' "gateway" \
    "not connected/authenticated — try: openshell status"
  agent_rc=1
fi

# Name has varied across OpenShell versions (openshell-<name>-<uuid>, then
# openshell-default--<name>-<uuid>), so match the sandbox name, not a prefix.
sbx_line="$(docker ps -a --format '{{.Names}}\t{{.Status}}' 2>/dev/null \
  | grep -m1 'urban-dossier-agent' || true)"
if [[ -z "$sbx_line" ]]; then
  printf '  --    %-28s %s\n' "sandbox" "no container registered"
  agent_rc=1
elif grep -q 'Up .*healthy' <<<"$sbx_line"; then
  printf '  ok    %-28s %s\n' "sandbox" "${sbx_line#*$'\t'}"
else
  printf '  FAIL  %-28s %s\n' "sandbox" "${sbx_line#*$'\t'}"
  agent_rc=1
fi

if [[ $agent_rc -ne 0 ]]; then
  if [[ $WITH_AGENT -eq 1 ]]; then
    rc=1
  else
    echo "  (informational — the default backend runs with the agent disabled)"
  fi
fi

say "reachable from the LAN at http://192.168.1.199:3456"
if [[ $rc -ne 0 ]]; then
  echo "  one or more checks failed"
fi
exit $rc
