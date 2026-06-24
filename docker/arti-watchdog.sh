#!/usr/bin/env bash
# Watch for the arti container to become unhealthy and auto-refresh bridges.
# Intended to run as a long-lived process under systemd (arti-watchdog.service).
#
# When an "unhealthy" health_status event fires, calls refresh-bridges.sh which
# checks whether the current bridges are TCP-blocked before actually refreshing.
# A COOLDOWN (default 1800s / 30 min) prevents rapid cycling if new bridges
# also fail to work.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER="${CONTAINER:-arti-arti-1}"
COOLDOWN="${COOLDOWN:-1800}"   # seconds between auto-refresh attempts

log() { echo "$(date -Iseconds) arti-watchdog: $*"; }

last_refresh=0

log "Watching container '$CONTAINER' for health_status events (cooldown=${COOLDOWN}s)..."

docker events \
    --filter "container=$CONTAINER" \
    --filter "event=health_status" \
    --format '{{.Status}}' \
| while IFS= read -r status; do
    [[ "$status" == "health_status: unhealthy" ]] || continue

    now=$(date +%s)
    elapsed=$(( now - last_refresh ))

    if (( elapsed < COOLDOWN )); then
        remaining=$(( COOLDOWN - elapsed ))
        log "Unhealthy event received — cooldown active, ${remaining}s remaining. Skipping."
        continue
    fi

    log "Unhealthy event received — running refresh-bridges.sh..."
    if "$SCRIPT_DIR/refresh-bridges.sh"; then
        last_refresh=$(date +%s)
        log "Refresh completed. Next auto-refresh not before $(date -d "@$(( last_refresh + COOLDOWN ))" -Iseconds 2>/dev/null || date -r $(( last_refresh + COOLDOWN )) -Iseconds 2>/dev/null || echo "${COOLDOWN}s from now")."
    else
        log "refresh-bridges.sh exited non-zero (bridges may still be reachable, or fetch failed)."
    fi
done
