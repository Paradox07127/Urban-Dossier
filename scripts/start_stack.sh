#!/usr/bin/env bash
# Bring up the Urban Dossier local stack: one command, from any state.
#
#   scripts/start_stack.sh            backend (8090) + frontend (3456)
#   scripts/start_stack.sh --llm      ... and the production vLLM on :8000
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
STATUS_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --llm) WITH_LLM=1 ;;
    --status) STATUS_ONLY=1 ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
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
  say "installing units into ${UNIT_DIR}"
  mkdir -p "$UNIT_DIR"
  for unit in ud-backend-noagent.service ud-frontend.service ud-stack.target; do
    ln -sfn "$REPO_ROOT/deploy/systemd/$unit" "$UNIT_DIR/$unit"
    echo "  $unit"
  done
  systemctl --user daemon-reload

  # A transient unit of the same name left by an older session shadows the
  # installed one until it is gone.
  systemctl --user stop ud-backend-noagent.service ud-frontend.service 2>/dev/null
  systemctl --user reset-failed ud-backend-noagent.service ud-frontend.service 2>/dev/null

  say "starting backend + frontend"
  if ! systemctl --user start ud-backend-noagent.service ud-frontend.service; then
    echo "  start failed; see: journalctl --user -u ud-backend-noagent -u ud-frontend" >&2
  fi

  if [[ $WITH_LLM -eq 1 ]]; then
    say "starting vLLM (production Nano, :8000)"
    # The embeddings service shares this compose profile but has no weights on
    # this host (models/embedding is empty), so start the LLM service alone.
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

say "reachable from the LAN at http://192.168.1.199:3456"
if [[ $rc -ne 0 ]]; then
  echo "  one or more checks failed"
fi
exit $rc
