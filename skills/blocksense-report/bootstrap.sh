#!/usr/bin/env bash
# bootstrap.sh — Dependency bootstrap for blocksense-report skill.
# Run once at arrival. Idempotent.
#
# Usage:
#   bash bootstrap.sh
#
# What it does:
#   1. Verifies python3 is available
#   2. Checks for jinja2
#   3. Installs any missing deps via `pip install --user`
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
for pkg in jinja2 json argparse; do
    if check_pkg "$pkg"; then
        if [ "$pkg" = "jinja2" ]; then
            VERSION=$(python3 -c "import jinja2; print(jinja2.__version__)" 2>/dev/null || echo "unknown")
            log "  jinja2 present (v$VERSION)"
        else
            log "  $pkg present (stdlib)"
        fi
    else
        warn "  $pkg NOT found"
        MISSING+=("$pkg")
    fi
done

# ----------------------------------------------------------------------------
# Step 3: Install if needed
# ----------------------------------------------------------------------------

if [ ${#MISSING[@]} -gt 0 ]; then
    # Map import names to pip package names
    PIP_PKGS=()
    for pkg in "${MISSING[@]}"; do
        case "$pkg" in
            jinja2) PIP_PKGS+=("Jinja2") ;;
            *)      PIP_PKGS+=("$pkg") ;;
        esac
    done

    log "Installing missing packages: ${PIP_PKGS[*]}"

    # Prefer --user install to avoid needing root. Fall back to system pip if
    # --user is rejected by the active environment (virtualenvs, PEP 668, etc).
    if python3 -m pip install --user "${PIP_PKGS[@]}"; then
        log "pip install --user succeeded"
    elif python3 -m pip install "${PIP_PKGS[@]}"; then
        log "pip install (no --user) succeeded"
    else
        err "pip install failed for: ${PIP_PKGS[*]}"
        err "Try one of:"
        err "  python3 -m pip install --break-system-packages ${PIP_PKGS[*]}"
        err "  python3 -m venv .venv && source .venv/bin/activate && pip install ${PIP_PKGS[*]}"
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
# Step 4: Jinja2 smoke test (simple template render)
# ----------------------------------------------------------------------------

log "Running Jinja2 smoke test..."
python3 - <<'PY'
from jinja2 import Environment
env = Environment()
tmpl = env.from_string("Hello {{ name }}, score {{ score }}/100")
result = tmpl.render(name="Brooklyn", score=72)
assert result == "Hello Brooklyn, score 72/100", f"unexpected: {result}"
print(f"  jinja2 version: {__import__('jinja2').__version__}")
print("  Template render OK")
PY

log "All dependencies present and working."
log "You can now run:"
log "    python3 scripts/extract_segments.py <analysis.json>"
log "    python3 scripts/render_report.py --segments <segments.json> --narratives <narratives.json> --template templates/report.html"
