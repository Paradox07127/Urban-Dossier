#!/usr/bin/env bash
# health-check.sh — verify every Urban Dossier service is reachable and responsive.
# Run after `bash scripts/vllm/start_vllm.sh` + uvicorn + node server.js are up.
# Exit code is 0 if all services pass, 1 otherwise.

set -uo pipefail

VLLM_HOST="${VLLM_HOST:-http://localhost:8000}"
OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"
BACKEND_HOST="${BACKEND_HOST:-http://localhost:8090}"
FRONTEND_HOST="${FRONTEND_HOST:-http://localhost:3456}"

PASS=0
FAIL=0

check() {
  local name="$1" url="$2" expect="$3"
  local body
  body="$(curl -fsS --max-time 5 "$url" 2>/dev/null || echo "__FAIL__")"
  if [[ "$body" == "__FAIL__" ]]; then
    printf "  [FAIL] %-30s %s — unreachable\n" "$name" "$url"
    FAIL=$((FAIL + 1))
    return
  fi
  if [[ -n "$expect" && "$body" != *"$expect"* ]]; then
    printf "  [FAIL] %-30s %s — missing expected token '%s'\n" "$name" "$url" "$expect"
    FAIL=$((FAIL + 1))
    return
  fi
  printf "  [ OK ] %-30s %s\n" "$name" "$url"
  PASS=$((PASS + 1))
}

echo "Urban Dossier — health check"
echo "----------------------------"

check "vLLM models"    "${VLLM_HOST}/v1/models"           "data"
check "Ollama tags"    "${OLLAMA_HOST}/api/tags"          "models"
check "Backend health" "${BACKEND_HOST}/api/health"       "ok"
check "Backend agent"  "${BACKEND_HOST}/api/agent/status" ""
check "Frontend"       "${FRONTEND_HOST}/api/health"      ""

echo "----------------------------"
printf "Result: %d passed, %d failed\n" "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
