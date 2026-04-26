#!/usr/bin/env bash
# bootstrap.sh - Bootstrap the urban-dossier-analyst skill on DGX Spark.
#
# Creates a local virtualenv, installs Python dependencies, and prints a
# ready message. Idempotent: safe to re-run. Mirrors the structure of
# skills/nemoclaw-user-prep-data/bootstrap.sh for consistency.
#
# Exit codes:
#   0 - virtualenv created and dependencies installed
#   1 - python3 missing
#   2 - pip install failed

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[bootstrap] python3 not found on PATH. Install Python 3.10+ first." >&2
    exit 1
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "[bootstrap] python3 found: ${PY_VER} ($(command -v python3))"

if [ ! -d ".venv" ]; then
    echo "[bootstrap] Creating virtualenv at .venv"
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[bootstrap] Upgrading pip"
python -m pip install --upgrade pip >/dev/null

echo "[bootstrap] Installing requirements"
if ! python -m pip install -r requirements.txt; then
    echo "[bootstrap] pip install failed" >&2
    exit 2
fi

# Quick import check
python - <<'PY'
import openai, httpx, pydantic
print(f"  openai   {openai.__version__}")
print(f"  httpx    {httpx.__version__}")
print(f"  pydantic {pydantic.VERSION}")
PY

echo "urban-dossier-analyst skill ready"
