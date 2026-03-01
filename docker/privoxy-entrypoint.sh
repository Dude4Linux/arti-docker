#!/bin/sh
set -e
HTTP_PORT="${HTTP_PORT:-8118}"
SOCKS_PORT="${SOCKS_PORT:-9150}"
sed -e "s|\${HTTP_PORT}|${HTTP_PORT}|g" \
    -e "s|\${SOCKS_PORT}|${SOCKS_PORT}|g" \
    /etc/privoxy/config.tpl > /tmp/privoxy.conf
exec privoxy --no-daemon /tmp/privoxy.conf
