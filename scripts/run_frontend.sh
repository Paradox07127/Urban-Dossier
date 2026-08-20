#!/usr/bin/env bash
# Launch server.js under a Node that can actually load better-sqlite3.
#
# better-sqlite3 is a native addon: it loads only under the Node ABI
# (NODE_MODULE_VERSION) it was compiled against.  This box has four Node
# installs -- /usr/bin/node is v18 (ABI 108) while node_modules was built
# under nvm's v22 (ABI 127) -- so a unit that hardcodes `node` picks the
# wrong one and dies with ERR_DLOPEN_FAILED before it ever binds 3456.
# Rather than pin an nvm path that the next `nvm install` invalidates, probe
# the candidates and take the first one that can require the addon.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

candidates=()
[[ -n "${URBAN_DOSSIER_NODE:-}" ]] && candidates+=("$URBAN_DOSSIER_NODE")
# Newest nvm version first -- reverse version sort, not lexical.
while IFS= read -r n; do candidates+=("$n"); done < <(
  ls -d "$HOME"/.nvm/versions/node/*/bin/node 2>/dev/null | sort -Vr
)
candidates+=(/usr/local/bin/node)
while IFS= read -r n; do candidates+=("$n"); done < <(
  ls -d /opt/node-*/bin/node 2>/dev/null | sort -Vr
)
candidates+=(/usr/bin/node)

for node in "${candidates[@]}"; do
  [[ -x "$node" ]] || continue
  if "$node" -e 'require("better-sqlite3")' >/dev/null 2>&1; then
    echo "run_frontend: using $node ($("$node" -v))"
    exec "$node" "$REPO_ROOT/server.js"
  fi
done

echo "run_frontend: no Node on this host can load better-sqlite3." >&2
echo "  Tried: ${candidates[*]}" >&2
echo "  Fix with 'npm rebuild better-sqlite3' under the Node you intend to" >&2
echo "  run, or point URBAN_DOSSIER_NODE at a matching binary." >&2
exit 1
