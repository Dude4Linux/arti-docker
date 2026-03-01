#!/bin/sh
set -e
SOCKS_PORT="${SOCKS_PORT:-9150}"
sed "s|\${SOCKS_PORT}|${SOCKS_PORT}|g" /etc/arti/arti.toml > /tmp/arti.toml
chmod 0640 /tmp/arti.toml
exec /usr/local/bin/arti --config /tmp/arti.toml proxy
