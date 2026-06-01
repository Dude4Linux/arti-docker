#!/bin/sh
set -e

SOCKS_PORT="${SOCKS_PORT:-9150}"
HTTP_PORT="${HTTP_PORT:-8118}"
TOR_MODE="${TOR_MODE:-auto}"

# Reject non-numeric port values before they reach the sed expression.
# A value containing sed metacharacters (e.g. "|") would otherwise inject
# additional sed commands into the config-file substitution below.
case "$SOCKS_PORT" in
    *[!0-9]*|'') echo "ERROR: SOCKS_PORT must be a positive integer (got '${SOCKS_PORT}')"; exit 1 ;;
esac
case "$HTTP_PORT" in
    *[!0-9]*|'') echo "ERROR: HTTP_PORT must be a positive integer (got '${HTTP_PORT}')"; exit 1 ;;
esac
case "$TOR_MODE" in
    auto|direct|bridge) ;;
    *) echo "ERROR: TOR_MODE must be auto, direct, or bridge (got '${TOR_MODE}')"; exit 1 ;;
esac

# Render base config (substitutes SOCKS_PORT placeholder)
sed "s|\${SOCKS_PORT}|${SOCKS_PORT}|g" /etc/arti/arti.toml > /tmp/arti.toml

# Probe known-stable Tor relay endpoints on :443.  A single TCP success is
# enough to assume direct connection works.  Total worst-case latency is
# 3 IPs * 3s timeout = 9s; on a healthy network the first probe returns
# within milliseconds.
detect_filtering() {
    for hp in 171.25.193.9:443 192.42.116.16:443 131.188.40.189:443; do
        host=${hp%:*}; port=${hp#*:}
        if nc -z -w 3 "$host" "$port" 2>/dev/null; then
            return 1
        fi
    done
    return 0
}

# Emit non-empty, non-comment lines from the bridges file.
read_bridges() {
    [ -r /etc/arti/bridges.txt ] || return 1
    grep -vE '^[[:space:]]*(#|$)' /etc/arti/bridges.txt
}

# Append a [bridges] section + lyrebird transport block to /tmp/arti.toml.
# Bridge lines are quoted as TOML basic strings; their contents (base64 +
# hex fingerprints + IP:port) never contain " or \ so no escaping is needed.
write_bridge_config() {
    {
        echo
        echo "[bridges]"
        echo "enabled = true"
        echo "bridges = ["
        printf '%s\n' "$1" | sed 's/.*/  "&",/'
        echo "]"
        echo
        echo "[[bridges.transports]]"
        echo 'protocols = ["obfs4"]'
        echo 'path = "/usr/bin/lyrebird"'
    } >> /tmp/arti.toml
}

USE_BRIDGES=0
case "$TOR_MODE" in
    direct)
        echo "entrypoint: TOR_MODE=direct — skipping filter probe, using direct connection."
        ;;
    bridge)
        echo "entrypoint: TOR_MODE=bridge — skipping filter probe, using bridges."
        USE_BRIDGES=1
        ;;
    auto)
        if detect_filtering; then
            echo "entrypoint: direct Tor connection appears filtered — switching to bridges."
            USE_BRIDGES=1
        else
            echo "entrypoint: direct Tor connection available — starting in direct mode."
        fi
        ;;
esac

if [ "$USE_BRIDGES" = "1" ]; then
    BRIDGES=$(read_bridges || true)
    if [ -z "$BRIDGES" ]; then
        cat >&2 <<EOF
ERROR: bridge mode requested but no bridges are configured.

Request bridges from https://bridges.torproject.org/ (or email
"get transport obfs4" to bridges@torproject.org from a Gmail/Riseup
address), then add the bridge lines to docker/bridges.txt and restart
the container with: docker compose restart
EOF
        exit 1
    fi
    write_bridge_config "$BRIDGES"
fi

chmod 0640 /tmp/arti.toml

# Terminate both children and exit; used by the TERM/INT trap and the
# monitor loop so Docker always gets a clean exit it can act on.
# Uses ${VAR:-} to handle the case where a signal arrives before a PID
# is assigned (early startup window).
cleanup() {
    kill "${ARTI_PID:-}" "${PROXY_PID:-}" 2>/dev/null || true
    wait "${ARTI_PID:-}" "${PROXY_PID:-}" 2>/dev/null || true
    exit "${1:-0}"
}

# Set the trap before starting child processes so a SIGTERM arriving
# at any point after this line is handled cleanly.
trap 'cleanup 0' TERM INT

# Start Arti
/usr/local/bin/arti --config /tmp/arti.toml proxy &
ARTI_PID=$!

# Start HTTP CONNECT proxy (forwards Proxy-Authorization creds to Arti SOCKS5)
HTTP_PORT="${HTTP_PORT}" SOCKS_PORT="${SOCKS_PORT}" \
    /usr/local/bin/tor-http-proxy &
PROXY_PID=$!

# If either process exits, bring down the other and let Docker restart
# the container (restart: unless-stopped).
while kill -0 "${ARTI_PID}" 2>/dev/null && kill -0 "${PROXY_PID}" 2>/dev/null; do
    sleep 5
done

cleanup 1
