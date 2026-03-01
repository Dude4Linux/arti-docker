# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

A Docker setup for running [Arti](https://gitlab.torproject.org/tpo/core/arti) — Tor reimplemented in Rust — as a SOCKS5 proxy on port 9150. The build pulls source from the upstream GitLab repo at build time (no local Rust code).

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

`.env` exposes two knobs:

| Variable | Default | Options |
|---|---|---|
| `ARTI_VERSION` | `latest` | `latest`, `arti-v2.0.0`, `main` |
| `PLATFORM` | `linux/amd64` | `linux/amd64`, `linux/arm64`, `linux/arm/v7` |

`latest` resolves to the most recent `arti-v*` release tag via `git ls-remote` at build time. Peeled tag entries (`^{}`) are excluded before sorting to avoid corrupting version resolution.

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

The Dockerfile applies both: `chown root:_arti /etc/arti/arti.toml && chmod 0640 /etc/arti/arti.toml`

## arti.toml Notes

- `port_info_file` must be set explicitly to a path under `/var/lib/arti`. If omitted, Arti defaults to `${HOME}/.local/share/arti/public/port_info.json` — which fails because `_arti` has no home directory.
- `socks_listen = "0.0.0.0:9150"` — listens on all interfaces inside the container (required for the host port mapping to work)

## Arti Architecture (relevant to configuration)

- **Multi-identity**: `IsolationToken` / `isolated_client()` provide stream isolation natively; multiple simultaneous identities don't require multiple processes
- **Multi-threaded**: Tokio thread pool (`TokioTp`), one thread per logical CPU core by default
- **Circuit rotation**: `max_dirtiness` defaults to 10 minutes; exits are selected with bandwidth-weighted randomness, family and /16-subnet exclusion
- **Vanguards**: enabled by the `vanguards` feature; three modes — `Disabled`, `Lite`, `Full`
