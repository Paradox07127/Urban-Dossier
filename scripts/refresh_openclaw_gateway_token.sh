#!/usr/bin/env bash
set -euo pipefail

SANDBOX_NAME="${NEMOCLAW_SANDBOX:-urban-dossier-agent}"
NEMOCLAW_BIN="${NEMOCLAW_BIN:-$(command -v nemoclaw || true)}"
TOKEN_FILE="${OPENCLAW_GATEWAY_TOKEN_FILE:-/mnt/data/urban-dossier/runtime/openclaw-gateway.token}"

if [[ -z "$NEMOCLAW_BIN" || ! -x "$NEMOCLAW_BIN" ]]; then
  echo "ERROR: nemoclaw not found; set NEMOCLAW_BIN to its executable path" >&2
  exit 2
fi

mkdir -p "$(dirname "$TOKEN_FILE")"
token_tmp="$(mktemp "${TOKEN_FILE}.tmp.XXXXXX")"
trap 'rm -f "$token_tmp"' EXIT
chmod 600 "$token_tmp"
"$NEMOCLAW_BIN" "$SANDBOX_NAME" gateway-token --quiet >"$token_tmp"
chmod 600 "$token_tmp"
mv -f "$token_tmp" "$TOKEN_FILE"
trap - EXIT
