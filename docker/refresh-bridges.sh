#!/usr/bin/env bash
# Fetch fresh obfs4 bridges from bridges.torproject.org and restart the arti container.
#
# Usage:
#   refresh-bridges.sh            — refresh only if all current bridges are TCP-unreachable
#   refresh-bridges.sh --force    — always refresh regardless of bridge status
#   refresh-bridges.sh --check    — report reachability of current bridges and exit

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGES_FILE="$SCRIPT_DIR/bridges.txt"
COMPOSE_DIR="$SCRIPT_DIR/.."

log() { echo "$(date -Iseconds) refresh-bridges: $*"; }

# --- Bridge reachability check ---
# Returns 0 (true) if every active bridge line is TCP-unreachable.
bridges_all_blocked() {
    local reachable=0 checked=0
    while IFS= read -r line; do
        # Match IPv4:port or [IPv6]:port (second field of the bridge line)
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
    [[ $checked -gt 0 && $reachable -eq 0 ]]
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

# -------------------------------------------------------------------------

MODE="${1:-}"

if [[ "$MODE" == "--check" ]]; then
    log "Checking current bridge reachability..."
    if bridges_all_blocked; then
        log "Result: all bridges are blocked."
        exit 1
    else
        log "Result: at least one bridge is reachable."
        exit 0
    fi
fi

if [[ "$MODE" != "--force" ]]; then
    log "Checking current bridge reachability..."
    if ! bridges_all_blocked; then
        log "At least one bridge is still reachable — no refresh needed."
        exit 0
    fi
    log "All bridges are blocked. Fetching fresh bridges..."
else
    log "Force refresh — fetching fresh bridges..."
fi

new_bridges=$(fetch_fresh_bridges)
count=$(echo "$new_bridges" | wc -l)
log "Fetched $count bridge(s):"
echo "$new_bridges" | sed 's/^/    /'

write_bridges "$new_bridges"
log "Updated $BRIDGES_FILE"

log "Restarting arti container..."
(cd "$COMPOSE_DIR" && docker compose restart)
log "Done."
