# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

A Docker setup that builds and runs two Rust binaries in a single container:

- **Arti** — Tor reimplemented in Rust, exposes a SOCKS5 proxy on port 9150
- **tor-http-proxy** — a minimal HTTP CONNECT proxy (written here, in `proxy/`) that forwards `Proxy-Authorization` credentials to Arti as SOCKS5 auth, enabling per-request Tor circuit isolation over HTTP

Both binaries are compiled from source inside the Docker builder stage. No Rust toolchain is required on the host.

## Repository Layout

```
Dockerfile              Multi-stage build: Rust builder → Alpine runtime
proxy/                  tor-http-proxy Rust crate (built alongside Arti)
  Cargo.toml
  src/main.rs
docker/
  arti.toml             Config template (${SOCKS_PORT} substituted at startup)
  entrypoint.sh         Starts both processes; supervision loop exits if either dies
bench/
  tor-bench.py          Latency/throughput benchmark; supports SOCKS5 and HTTP
  requirements.txt
.env.template           Canonical defaults — copy to .env on install
docker-compose.yml      Single arti service; publishes both ports
```

## Commands

```bash
# Build and start (uses ARTI_VERSION and PLATFORM from .env)
docker compose up -d

# Build without cache (e.g. after changing VERSION or proxy source)
docker compose build --no-cache

# Follow logs (both Arti and tor-http-proxy write to stdout/stderr)
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
| `HTTP_PORT` | `8118` | Host port mapped to the container's HTTP CONNECT listener |

`latest` resolves to the most recent `arti-v*` release tag via `git ls-remote` at build time. Peeled tag entries (`^{}`) are excluded before sorting to avoid corrupting version resolution.

Both `SOCKS_PORT` and `HTTP_PORT` are injected at container startup via `docker/entrypoint.sh`. Changing either requires only a container restart, not a rebuild.

## Cargo Features Used (Arti)

```
tokio,rustls,dns-proxy,harden,compression,bridge-client,onion-service-client,pt-client,vanguards,static-sqlite
```

- **`rustls`** (with `--no-default-features`): pure-Rust TLS — eliminates the OpenSSL/libssl system dependency on musl/Alpine
- **`static-sqlite`**: bundles and compiles SQLite from source — eliminates the `libsqlite3` system dependency
- Both are required for static linking on Alpine; without them the linker fails with `-lssl`/`-lcrypto`/`-lsqlite3` errors

## tor-http-proxy (proxy/)

Minimal HTTP CONNECT proxy (~130 lines of Rust). Dependencies: `tokio`, `tokio-socks`, `base64`.

**Flow for each request:**
1. Accept TCP connection from client
2. Read HTTP headers until `\r\n\r\n`
3. Verify method is `CONNECT`; extract `host:port` target
4. If `Proxy-Authorization: Basic <b64>` is present, decode to `user:pass`
5. Open SOCKS5 connection to Arti (`127.0.0.1:SOCKS_PORT`) with those credentials
6. Send `HTTP/1.1 200 Connection Established` to client
7. `copy_bidirectional` for the lifetime of the tunnel

Without credentials, connects to Arti anonymously (circuit shared with other anonymous streams). With unique credentials per request, Arti assigns a fresh circuit each time.

Configuration via environment variables: `HTTP_PORT` (listen port), `SOCKS_PORT` (upstream).

## Entrypoint and Process Supervision

`docker/entrypoint.sh` runs as `_arti`:
1. Substitutes `${SOCKS_PORT}` in the config template → `/tmp/arti.toml`
2. Starts Arti in the background; records PID
3. Starts tor-http-proxy in the background; records PID
4. Traps SIGTERM/SIGINT to cleanly terminate both children
5. Polls both PIDs every 5 seconds; if either exits, kills the other and exits with code 1 so Docker restarts the container

## Runtime Image Notes

The runtime stage is `alpine:3.22` (not scratch) to include `sqlite-libs`, `ca-certificates`, and `curl` (used by the healthcheck).

`_arti` system user mirrors Debian packaging: no home directory (`-H`), no login shell (`/sbin/nologin`). Both Arti and tor-http-proxy run as this user.

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

## Arti Architecture (relevant to configuration)

- **Multi-identity**: `IsolationToken` / `isolated_client()` provide stream isolation natively; multiple simultaneous identities don't require multiple processes
- **Multi-threaded**: Tokio thread pool (`TokioTp`), one thread per logical CPU core by default
- **Circuit rotation**: `max_dirtiness` defaults to 10 minutes; exits are selected with bandwidth-weighted randomness, family and /16-subnet exclusion
- **Vanguards**: enabled by the `vanguards` feature; three modes — `Disabled`, `Lite`, `Full`

## Git Tags

- `privoxy` — last commit before replacing Privoxy with tor-http-proxy; useful baseline for benchmarking comparison
