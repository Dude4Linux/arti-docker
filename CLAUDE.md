# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

A Docker setup that builds and runs three Rust binaries in a single container:

- **Arti** — Tor reimplemented in Rust; exposes a SOCKS5 proxy on port 9150
- **tor-http-proxy** — minimal HTTP CONNECT proxy (`proxy/src/main.rs`) that forwards `Proxy-Authorization` credentials to Arti as SOCKS5 auth, enabling per-request Tor circuit isolation over HTTP
- **health-probe** — minimal healthcheck binary (`proxy/src/bin/health-probe.rs`) that verifies the full stack by opening a CONNECT tunnel through tor-http-proxy; replaces the previous curl-based healthcheck, eliminating curl from the runtime image

All three are compiled from source inside the Docker builder stage. No Rust toolchain is required on the host for running the container.

## Repository Layout

```
Dockerfile              Multi-stage build: Rust builder → Alpine runtime
proxy/                  Cargo crate — tor-http-proxy + health-probe
  Cargo.toml
  Cargo.lock            Committed; enables --locked builds for reproducibility
  src/
    main.rs             tor-http-proxy binary
    bin/
      health-probe.rs   healthcheck binary (used by Docker HEALTHCHECK)
docker/
  arti.toml             Config template (${SOCKS_PORT} substituted at startup)
  entrypoint.sh         Validates ports; probes for Tor filtering; starts processes
  bridges.txt.template  Empty obfs4 bridge file template (committed)
  bridges.txt           Real bridges (gitignored, host-only, bind-mounted in)
bench/                  Rust benchmark crate (built locally, not in Docker)
  Cargo.toml
  Cargo.lock
  src/main.rs           tor-bench binary
.env.template           Canonical defaults — copy to .env on install
docker-compose.yml      Single arti service; publishes both ports
```

## Commands

```bash
# Build and start (uses ARTI_VERSION and PLATFORM from .env)
docker compose up -d

# Build without cache (e.g. after changing VERSION or proxy source)
docker compose build --no-cache

# Enable logging then follow logs (default log driver is 'none')
LOG_DRIVER=json-file docker compose up -d
docker compose logs -f

# Stop and remove containers (volumes are preserved)
docker compose down

# Nuke volumes (clears bootstrapped state — slow next start)
docker compose down -v

# Multi-arch build (requires QEMU binfmt registered)
docker run --privileged --rm tonistiigi/binfmt --install all
PLATFORM=linux/arm64 docker compose build --no-cache

# Build and run the benchmark locally
cd bench && cargo build --release
./target/release/tor-bench --socks5 127.0.0.1:9150 --count 50
./target/release/tor-bench --http   127.0.0.1:8118 --count 50
```

## Configuration

`.env` exposes these knobs:

| Variable | Default | Notes |
|---|---|---|
| `ARTI_VERSION` | `latest` | `latest`, `arti-v2.0.0`, `main` |
| `PLATFORM` | `linux/amd64` | `linux/amd64`, `linux/arm64`, `linux/arm/v7` |
| `BIND_ADDR` | `127.0.0.1` | Interface address for published ports; LAN IP for network access |
| `SOCKS_PORT` | `9150` | Host port mapped to the container's SOCKS5 listener |
| `HTTP_PORT` | `8118` | Host port mapped to the container's HTTP CONNECT listener |
| `LOG_DRIVER` | *(unset)* | `json-file` to enable logging; unset = `none` (no disk writes) |
| `TOR_MODE` | `auto` | `auto` probes for filtering; `direct` forces no bridges; `bridge` forces bridges |

`latest` resolves to the most recent `arti-v*` release tag via `git ls-remote` at build time.

Both `SOCKS_PORT` and `HTTP_PORT` are injected at container startup via `docker/entrypoint.sh`. Changing either requires only a container restart, not a rebuild.

## Cargo Features Used (Arti)

```
tokio,rustls,dns-proxy,harden,compression,bridge-client,onion-service-client,pt-client,vanguards,static-sqlite
```

- **`rustls`** (with `--no-default-features`): pure-Rust TLS — eliminates the OpenSSL/libssl dependency on musl/Alpine
- **`static-sqlite`**: bundles SQLite from source — eliminates the `libsqlite3` dependency
- Both required for static linking on Alpine; without them the linker fails with `-lssl`/`-lcrypto`/`-lsqlite3` errors

## tor-http-proxy (`proxy/src/main.rs`)

Minimal HTTP CONNECT proxy. Dependencies: `tokio`, `tokio-socks`, `base64`.

**Per-request flow:**
1. Accept TCP connection
2. Read headers with a 30-second timeout (Slowloris mitigation)
3. Verify method is `CONNECT`; extract `host:port` target
4. If `Proxy-Authorization: Basic <b64>` is present, decode to `user:pass`; reject credentials exceeding 255 bytes (SOCKS5 RFC 1929 limit — prevents silent truncation from collapsing circuit identities)
5. Open SOCKS5 connection to Arti (`127.0.0.1:SOCKS_PORT`) with those credentials
6. Send `HTTP/1.1 200 Connection Established`
7. `copy_bidirectional` for the tunnel lifetime

Without credentials, connects anonymously (shared circuit). Unique credentials per request → fresh circuit per request.

## health-probe (`proxy/src/bin/health-probe.rs`)

Replaces the former `curl` healthcheck. Uses only `tokio` — no new dependencies.

Sends `CONNECT check.torproject.org:443` to tor-http-proxy; a `200` response confirms the full stack (tor-http-proxy → Arti → Tor relay → exit node → target) is functional. Exits 0 (healthy) or 1 (unhealthy). Configured via `HTTP_PORT` and `HEALTH_TIMEOUT` env vars.

## Entrypoint and Process Supervision

`docker/entrypoint.sh` runs as `_arti`:
1. Validates `SOCKS_PORT`, `HTTP_PORT` are numeric (prevents sed injection) and `TOR_MODE` is `auto|direct|bridge`
2. Substitutes `${SOCKS_PORT}` in the config template → `/tmp/arti.toml`
3. If `TOR_MODE=auto`, probes three known Tor relay IPs on :443 (`171.25.193.9`, `192.42.116.16`, `131.188.40.189`) with `nc -z -w 3`; a single TCP success means direct mode, all-fail means bridge mode. Worst-case latency: 9s. If `TOR_MODE=direct|bridge`, the probe is skipped.
4. In bridge mode, reads non-comment lines from `/etc/arti/bridges.txt` (bind-mounted from `docker/bridges.txt`) and appends a `[bridges]` + `[[bridges.transports]]` block to `/tmp/arti.toml` with `path = "/usr/bin/lyrebird"`. Exits with a clear error if no bridges are configured.
5. Installs SIGTERM/SIGINT trap **before** starting children (closes race window)
6. Starts Arti in the background; records PID
7. Starts tor-http-proxy in the background; records PID
8. Polls both PIDs every 5 seconds; if either exits, kills the other and exits with code 1 so Docker restarts the container

## Filter Detection and Bridges

Some ISPs (notably T-Mobile Home Internet, some carrier-grade NAT operators, certain corporate networks) block direct connections to known Tor relay IPs. Arti fetches the consensus from a fallback directory then can't open channels to any guard relay — symptom is endless `Could not connect to guard` warnings in the log.

The entrypoint's startup probe distinguishes this from a general network problem by checking whether *Tor-specific* IPs are blocked while general internet works. When filtered, it switches to bridges (obfs4 pluggable transport via `lyrebird`).

Bridges must be supplied by the user — Tor bridges are intentionally not in the consensus and bundled defaults would be burned quickly. Request from `https://bridges.torproject.org/` or `bridges@torproject.org`, paste into `docker/bridges.txt`, restart container.

The `lyrebird` package (Alpine 3.22 community) replaces the obfs4proxy package — same wire protocol, includes `meek_lite` and `webtunnel` support.

## Docker Compose Hardening

Current hardening applied:

| Control | Value |
|---|---|
| `cap_drop` | ALL (no Linux capabilities) |
| `no-new-privileges` | true |
| `USER` | `_arti` (non-root) |
| `read_only` | true (root filesystem read-only) |
| `/tmp` | tmpfs (10 MiB) — only writable path outside named volumes |
| `BIND_ADDR` | `127.0.0.1` by default (loopback only) |
| CPU / memory / PID | limits applied |
| `nofile` ulimit | 16384 |
| Log driver | `none` by default (no disk writes) |
| Network | isolated `proxy-net` bridge (not on Docker's default bridge) |

**Network note**: the container is on a dedicated `proxy-net` bridge. When integrating with another stack (e.g. Amass), remove the `proxy-net` definition from this compose file and declare it as `external: true`, pointing to the shared network in the other stack.

## Runtime Image Notes

The runtime stage is `alpine:3.22` to include `sqlite-libs` and `ca-certificates`. `curl` is **not** installed — the healthcheck uses the compiled `health-probe` binary instead.

`_arti` system user mirrors Debian packaging: no home directory (`-H`), no login shell (`/sbin/nologin`).

Persistent Docker volumes:
- `arti-cache` → `/var/cache/arti` — bootstrapped directory info (microdescriptors, consensus)
- `arti-state` → `/var/lib/arti` — guard/circuit state, key material

## fs-mistrust File Permission Rules

Arti's `fs-mistrust` crate enforces strict checks on the config file:

1. File must **not** be group- or world-writable → `chmod 0640`
2. Running user (`_arti`) must be able to **read** it → `chown root:_arti`

The Dockerfile applies both to the template. The runtime config at `/tmp/arti.toml` also gets `chmod 0640` and is owned by `_arti`.

## arti.toml Notes

- `port_info_file` must be set explicitly to `/var/lib/arti/port_info.json`; if omitted, Arti defaults to `${HOME}/.local/share/…` which fails because `_arti` has no home directory
- `socks_listen = "0.0.0.0:${SOCKS_PORT}"` — placeholder substituted at startup; listens on all interfaces inside the container (required for host port mapping)

## Arti Architecture

- **Multi-identity**: `IsolationToken` / `isolated_client()` provide stream isolation natively
- **Multi-threaded**: Tokio thread pool, one thread per CPU core by default
- **Circuit rotation**: `max_dirtiness` defaults to 10 minutes; exits selected with bandwidth-weighted randomness, family and /16-subnet exclusion
- **Vanguards**: enabled; three modes — `Disabled`, `Lite`, `Full`

## Git Tags

- `privoxy` — last commit before replacing Privoxy with tor-http-proxy
