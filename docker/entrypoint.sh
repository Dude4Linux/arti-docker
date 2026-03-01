#!/bin/sh
set -e

SOCKS_PORT="${SOCKS_PORT:-9150}"
HTTP_PORT="${HTTP_PORT:-8118}"

# Write runtime config (substitutes SOCKS_PORT placeholder)
sed "s|\${SOCKS_PORT}|${SOCKS_PORT}|g" /etc/arti/arti.toml > /tmp/arti.toml
chmod 0640 /tmp/arti.toml

# Terminate both children and exit; used by the TERM/INT trap and the
# monitor loop so Docker always gets a clean exit it can act on.
cleanup() {
    kill "${ARTI_PID}" "${PROXY_PID}" 2>/dev/null
    wait "${ARTI_PID}" "${PROXY_PID}" 2>/dev/null
    exit "${1:-0}"
}

# Start Arti
/usr/local/bin/arti --config /tmp/arti.toml proxy &
ARTI_PID=$!

# Start HTTP CONNECT proxy (forwards Proxy-Authorization creds to Arti SOCKS5)
HTTP_PORT="${HTTP_PORT}" SOCKS_PORT="${SOCKS_PORT}" \
    /usr/local/bin/tor-http-proxy &
PROXY_PID=$!

trap 'cleanup 0' TERM INT

# If either process exits, bring down the other and let Docker restart
# the container (restart: unless-stopped).
while kill -0 "${ARTI_PID}" 2>/dev/null && kill -0 "${PROXY_PID}" 2>/dev/null; do
    sleep 5
done

cleanup 1
