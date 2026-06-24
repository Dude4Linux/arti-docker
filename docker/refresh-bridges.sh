#!/usr/bin/env bash
# Set up or refresh obfs4 bridges for the arti container.
#
# On first run this script bootstraps the two host-side files that are
# gitignored and must exist before the container starts:
#
#   .env              — copied from .env.template if absent
#   docker/bridges.txt — populated with fresh bridges if empty or all blocked
#
# Afterwards the container is restarted (if running) so the new bridges
# take effect immediately.
#
# Usage:
#   refresh-bridges.sh            — setup/refresh if needed (safe to re-run)
#   refresh-bridges.sh --force    — always fetch fresh bridges
#   refresh-bridges.sh --check    — report bridge reachability and exit

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGES_FILE="$SCRIPT_DIR/bridges.txt"
COMPOSE_DIR="$SCRIPT_DIR/.."

log() { echo "$(date -Iseconds) refresh-bridges: $*"; }

# --- Bootstrap .env ---
if [[ ! -f "$COMPOSE_DIR/.env" ]]; then
    log ".env not found — copying from .env.template"
    cp "$COMPOSE_DIR/.env.template" "$COMPOSE_DIR/.env"
    log "Review $COMPOSE_DIR/.env and adjust settings if needed (defaults work for local use)."
fi

# --- Bootstrap bridges.txt ---
if [[ ! -f "$BRIDGES_FILE" ]]; then
    log "bridges.txt not found — copying from template"
    cp "$SCRIPT_DIR/bridges.txt.template" "$BRIDGES_FILE"
fi

# --- Bridge reachability check ---
# Returns 0 (true) if there are no active bridge lines OR every active
# bridge is TCP-unreachable; 1 if at least one bridge responds.
needs_refresh() {
    local reachable=0 checked=0

    while IFS= read -r line; do
        if [[ "$line" =~ ^obfs4[[:space:]]\[([^\]]+)\]:([0-9]+) ]]; then
            ip="${BASH_REMATCH[1]}"; port="${BASH_REMATCH[2]}"
        elif [[ "$line" =~ ^obfs4[[:space:]]([0-9.]+):([0-9]+) ]]; then
            ip="${BASH_REMATCH[1]}"; port="${BASH_REMATCH[2]}"
        else
            continue
        fi
        ((checked++))
        if nc -z -w 5 "$ip" "$port" 2>/dev/null; then
            log "  $ip:$port REACHABLE"
            ((reachable++))
        else
            log "  $ip:$port unreachable"
        fi
    done < <(grep -v '^[[:space:]]*#' "$BRIDGES_FILE" | grep -v '^[[:space:]]*$')

    # Need refresh when: nothing configured yet, or everything is blocked.
    [[ $checked -eq 0 || $reachable -eq 0 ]]
}

# --- Fetch bridges from bridges.torproject.org ---
# Makes two requests and deduplicates, yielding up to 4 bridges.
fetch_fresh_bridges() {
    python3 - <<'PYEOF'
import urllib.request, re, html, sys

seen = []
for _ in range(2):
    try:
        with urllib.request.urlopen(
            "https://bridges.torproject.org/bridges?transport=obfs4", timeout=30
        ) as r:
            text = html.unescape(r.read().decode())
    except Exception as e:
        print(f"WARNING: fetch attempt failed: {e}", file=sys.stderr)
        continue
    for m in re.finditer(
        r'obfs4 [\w\[\].:]+:\d+ [0-9A-F]{40} cert=\S+ iat-mode=\d+', text
    ):
        if m.group() not in seen:
            seen.append(m.group())

if not seen:
    print("ERROR: no bridges returned from bridges.torproject.org", file=sys.stderr)
    sys.exit(1)

for line in seen:
    print(line)
PYEOF
}

# --- Rewrite bridges.txt: preserve comment header, replace active lines ---
write_bridges() {
    local new_bridges="$1"
    {
        grep '^[[:space:]]*#' "$BRIDGES_FILE" || true
        printf '\n'
        printf '%s\n' $new_bridges
        printf '\n'
    } > "${BRIDGES_FILE}.tmp"
    mv "${BRIDGES_FILE}.tmp" "$BRIDGES_FILE"
}

# --- Restart container if it is currently running ---
restart_if_running() {
    if (cd "$COMPOSE_DIR" && docker compose ps --format '{{.State}}' 2>/dev/null \
            | grep -q running); then
        log "Restarting arti container..."
        (cd "$COMPOSE_DIR" && docker compose restart)
        log "Done."
    else
        log "Container is not running — start it with: docker compose up -d"
    fi
}

# -------------------------------------------------------------------------

MODE="${1:-}"

if [[ "$MODE" == "--check" ]]; then
    log "Checking current bridge reachability..."
    if needs_refresh; then
        log "Result: no reachable bridges (refresh needed)."
        exit 1
    else
        log "Result: at least one bridge is reachable."
        exit 0
    fi
fi

if [[ "$MODE" != "--force" ]]; then
    log "Checking current bridge reachability..."
    if ! needs_refresh; then
        log "At least one bridge is reachable — no refresh needed."
        exit 0
    fi
    log "Fetching bridges..."
else
    log "Force refresh — fetching fresh bridges..."
fi

new_bridges=$(fetch_fresh_bridges)
count=$(echo "$new_bridges" | wc -l)
log "Fetched $count bridge(s):"
echo "$new_bridges" | sed 's/^/    /'

write_bridges "$new_bridges"
log "Updated $BRIDGES_FILE"

restart_if_running
