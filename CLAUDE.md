# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

A Docker setup for running [Arti](https://gitlab.torproject.org/tpo/core/arti) — Tor reimplemented in Rust — as a SOCKS5 proxy on port 9150, with an optional Privoxy HTTP proxy layer. The build pulls source from the upstream GitLab repo at build time (no local Rust code).

## Commands

```bash
# Build and start (uses ARTI_VERSION and PLATFORM from .env)
docker compose up -d

# Build without cache (e.g. after changing VERSION)
docker compose build --no-cache

# Follow logs
docker compose logs -f

# Stop and remove containers (volumes are preserved)
docker compose down

# Nuke volumes (clears bootstrapped state — slow next start)
docker compose down -v

# Multi-arch build (requires QEMU binfmt registered)
docker run --privileged --rm tonistiigi/binfmt --install all
PLATFORM=linux/arm64 docker compose build --no-cache
```

## Configuration

`.env` exposes these knobs:

| Variable | Default | Notes |
|---|---|---|
| `ARTI_VERSION` | `latest` | `latest`, `arti-v2.0.0`, `main` |
| `PLATFORM` | `linux/amd64` | `linux/amd64`, `linux/arm64`, `linux/arm/v7` |
| `BIND_ADDR` | `127.0.0.1` | Interface address for published ports; set to LAN IP for network access |
| `SOCKS_PORT` | `9150` | Host port mapped to the container's SOCKS5 listener |
| `HTTP_PORT` | `8118` | Host port for Privoxy HTTP proxy (only used when http-proxy profile is active) |
| `COMPOSE_PROFILES` | *(unset)* | Set to `http-proxy` to enable the Privoxy service |

`latest` resolves to the most recent `arti-v*` release tag via `git ls-remote` at build time. Peeled tag entries (`^{}`) are excluded before sorting to avoid corrupting version resolution.

`SOCKS_PORT` is injected at container startup via `docker/entrypoint.sh`, which substitutes the value into `arti.toml` and writes the result to `/tmp/arti.toml` before exec-ing Arti. Changing `SOCKS_PORT` requires only a container restart, not a rebuild.

## Cargo Features Used

```
tokio,rustls,dns-proxy,harden,compression,bridge-client,onion-service-client,pt-client,vanguards,static-sqlite
```

- **`rustls`** (with `--no-default-features`): pure-Rust TLS — eliminates the OpenSSL/libssl system dependency on musl/Alpine
- **`static-sqlite`**: bundles and compiles SQLite from source — eliminates the `libsqlite3` system dependency
- Both are required for static linking on Alpine; without them the linker fails with `-lssl`/`-lcrypto`/`-lsqlite3` errors

## Runtime Image Notes

The runtime stage is `alpine:3.22` (not scratch) to include `sqlite-libs` and `ca-certificates`.

`_arti` system user mirrors Debian packaging: no home directory (`-H`), no login shell (`/sbin/nologin`).

Persistent Docker volumes:
- `arti-cache` → `/var/cache/arti` — bootstrapped directory info (microdescriptors, consensus)
- `arti-state` → `/var/lib/arti` — guard/circuit state, key material

## fs-mistrust File Permission Rules

Arti's `fs-mistrust` crate enforces strict checks on the config file. Both conditions must hold or Arti exits immediately:

1. The file must **not** be group- or world-writable → `chmod 0640`
2. The running user (`_arti`) must be able to **read** it → `chown root:_arti`

The Dockerfile applies both to the template: `chown root:_arti /etc/arti/arti.toml && chmod 0640 /etc/arti/arti.toml`. The runtime config written to `/tmp/arti.toml` by `entrypoint.sh` also gets `chmod 0640` and is owned by `_arti`, satisfying both conditions.

## arti.toml Notes

- `port_info_file` must be set explicitly to a path under `/var/lib/arti`. If omitted, Arti defaults to `${HOME}/.local/share/arti/public/port_info.json` — which fails because `_arti` has no home directory.
- `socks_listen = "0.0.0.0:${SOCKS_PORT}"` — template placeholder substituted at startup by `entrypoint.sh`; listens on all interfaces inside the container (required for the host port mapping to work)

## Privoxy HTTP Proxy (Optional)

Privoxy bridges HTTP proxy clients to Arti's SOCKS5 tunnel. Many tools honour `HTTP_PROXY`/`HTTPS_PROXY` env vars but don't speak SOCKS5; Privoxy handles the translation.

**Architecture**: `client → HTTP → Privoxy:8118 → SOCKS5 → Arti:9150 → Tor`

**Enable**: uncomment `COMPOSE_PROFILES=http-proxy` in `.env`, then `docker compose up -d`.

**Files**:
- `docker/Dockerfile.privoxy` — Alpine image with Privoxy
- `docker/privoxy.conf` — config template (`${HTTP_PORT}` / `${SOCKS_PORT}` placeholders)
- `docker/privoxy-entrypoint.sh` — substitutes vars at startup, exec's `privoxy --no-daemon`

`forward-socks5t` (not `forward-socks5`) is used so DNS resolution happens inside the Tor network (better privacy).

Privoxy runs with no action/filter files — pure pass-through forwarding only.

## Arti Architecture (relevant to configuration)

- **Multi-identity**: `IsolationToken` / `isolated_client()` provide stream isolation natively; multiple simultaneous identities don't require multiple processes
- **Multi-threaded**: Tokio thread pool (`TokioTp`), one thread per logical CPU core by default
- **Circuit rotation**: `max_dirtiness` defaults to 10 minutes; exits are selected with bandwidth-weighted randomness, family and /16-subnet exclusion
- **Vanguards**: enabled by the `vanguards` feature; three modes — `Disabled`, `Lite`, `Full`
