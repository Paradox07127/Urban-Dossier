#!/usr/bin/env bash
# bootstrap.sh — Dependency bootstrap for blocksense-poster.
# Run once at arrival. Idempotent.
#
# Usage:
#   bash bootstrap.sh
#
# What it does:
#   1. Verifies python3 is available
#   2. Checks for jinja2
#   3. Installs jinja2 via `pip install --user` if missing
#   4. Prints versions on success
#   5. Runs a one-shot Jinja2 smoke test to confirm the install works
#
# Exit codes:
#   0 — all dependencies present and importable
#   1 — python3 missing
#   2 — pip install failed
#   3 — post-install import still failing

set -euo pipefail

RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[0;33m'
NC=$'\033[0m'

log()  { printf '%s[bootstrap]%s %s\n' "$GREEN" "$NC" "$*"; }
warn() { printf '%s[bootstrap]%s %s\n' "$YELLOW" "$NC" "$*" >&2; }
err()  { printf '%s[bootstrap]%s %s\n' "$RED" "$NC" "$*" >&2; }

# ----------------------------------------------------------------------------
# Step 1: Verify python3
# ----------------------------------------------------------------------------

if ! command -v python3 >/dev/null 2>&1; then
    err "python3 not found on PATH. Install Python 3.9+ before running this script."
    exit 1
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
log "python3 found: $PY_VER ($(command -v python3))"

# ----------------------------------------------------------------------------
# Step 2: Check what's missing
# ----------------------------------------------------------------------------

check_pkg() {
    python3 -c "import $1" 2>/dev/null
}

MISSING=()
for pkg in jinja2; do
    if check_pkg "$pkg"; then
        VERSION=$(python3 -c "import $pkg; print($pkg.__version__)" 2>/dev/null || echo "unknown")
        log "  $pkg present (v$VERSION)"
    else
        warn "  $pkg NOT found"
        MISSING+=("$pkg")
    fi
done

# ----------------------------------------------------------------------------
# Step 3: Install if needed
# ----------------------------------------------------------------------------

if [ ${#MISSING[@]} -gt 0 ]; then
    log "Installing missing packages: ${MISSING[*]}"

    # Prefer --user install to avoid needing root. Fall back to system pip if
    # --user is rejected by the active environment (virtualenvs, PEP 668, etc).
    if python3 -m pip install --user "${MISSING[@]}"; then
        log "pip install --user succeeded"
    elif python3 -m pip install "${MISSING[@]}"; then
        log "pip install (no --user) succeeded"
    else
        err "pip install failed for: ${MISSING[*]}"
        err "Try one of:"
        err "  python3 -m pip install --break-system-packages ${MISSING[*]}"
        err "  python3 -m venv .venv && source .venv/bin/activate && pip install ${MISSING[*]}"
        exit 2
    fi

    # Re-verify
    for pkg in "${MISSING[@]}"; do
        if ! check_pkg "$pkg"; then
            err "$pkg still not importable after install — check PATH / PYTHONPATH"
            exit 3
        fi
    done
fi

# ----------------------------------------------------------------------------
# Step 4: Jinja2 smoke test
# ----------------------------------------------------------------------------

log "Running Jinja2 smoke test..."
python3 - <<'PY'
from jinja2 import Environment
env = Environment()
template = env.from_string("Hello {{ name }}!")
result = template.render(name="BlockSense")
assert result == "Hello BlockSense!", f"unexpected result: {result}"
print(f"  jinja2 version: {__import__('jinja2').__version__}")
print("  Template render smoke test OK")
PY

log "All dependencies present and working."
log "You can now run:"
log "    python3 scripts/extract_highlights.py <analysis.json>"
log "    python3 scripts/render_poster.py --highlights <highlights.json> --headline '...' --summary '...'"
