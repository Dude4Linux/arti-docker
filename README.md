# arti-docker

A Docker setup for running [Arti](https://gitlab.torproject.org/tpo/core/arti) — Tor
reimplemented in Rust — as a SOCKS5 proxy. Optionally includes a
[Privoxy](https://www.privoxy.org/) HTTP proxy that forwards traffic through Arti,
bridging tools that only speak `HTTP_PROXY`/`HTTPS_PROXY`.

```
client → SOCKS5 → Arti:9150 → Tor
client → HTTP   → Privoxy:8118 → Arti:9150 → Tor
```

## Requirements

- Docker Engine 24+ with Compose V2 (`docker compose`)
- x86-64, arm64, or armv7 host (see [Configuration](#configuration))

## Installation

```bash
git clone <repo-url>
cd arti-docker
cp .env.template .env
```

Edit `.env` to suit your environment (all settings are optional — the defaults work
out of the box for local use):

```bash
$EDITOR .env
```

## Configuration

All configurable settings live in `.env`. The file is excluded from version control so
it won't be overwritten when you pull updates; `.env.template` is the canonical
reference for defaults.

| Variable | Default | Description |
|---|---|---|
| `ARTI_VERSION` | `latest` | `latest`, `arti-v2.0.0`, or `main` |
| `PLATFORM` | `linux/amd64` | `linux/amd64`, `linux/arm64`, `linux/arm/v7` |
| `BIND_ADDR` | `127.0.0.1` | Interface to bind published ports to (see below) |
| `SOCKS_PORT` | `9150` | Host port for the SOCKS5 proxy |
| `HTTP_PORT` | `8118` | Host port for the Privoxy HTTP proxy |
| `COMPOSE_PROFILES` | *(unset)* | Set to `http-proxy` to enable Privoxy |

### BIND_ADDR

By default ports are bound to `127.0.0.1` — only the machine running Docker can
connect. To allow access from other hosts on your local network, set `BIND_ADDR` to
your host's LAN IP:

```
BIND_ADDR=192.168.1.100
```

> **Warning:** Do not set `BIND_ADDR=0.0.0.0` unless you are behind a firewall.
> Tor proxy ports should never be exposed to the internet.

### Enabling the HTTP proxy (Privoxy)

Uncomment `COMPOSE_PROFILES` in `.env`:

```
COMPOSE_PROFILES=http-proxy
```

Privoxy will start alongside Arti and wait until Arti has a working Tor circuit before
accepting connections.

## Starting and stopping

```bash
# Build images and start (first run compiles Arti from source — takes several minutes)
docker compose up -d

# With Privoxy enabled
COMPOSE_PROFILES=http-proxy docker compose up -d

# Follow logs
docker compose logs -f

# Check service health
docker compose ps

# Stop containers (volumes are preserved)
docker compose down

# Stop and remove volumes (clears cached Tor directory — next start will re-bootstrap)
docker compose down -v
```

After the first build, subsequent starts are fast because the bootstrapped Tor
directory is cached in a Docker volume.

## Usage

### SOCKS5 proxy

Pass `--socks5-hostname` (not `--socks5`) so that DNS is resolved inside the Tor
network rather than locally:

```bash
# Verify traffic exits through Tor
curl --socks5-hostname 127.0.0.1:9150 https://check.torproject.org/api/ip

# Fetch a page
curl --socks5-hostname 127.0.0.1:9150 https://example.com

# Use with wget
wget -e "https_proxy=socks5h://127.0.0.1:9150" https://example.com
```

Configure applications via environment variables:

```bash
export ALL_PROXY=socks5h://127.0.0.1:9150
curl https://check.torproject.org/api/ip
```

### HTTP proxy (Privoxy)

Any tool that honours `HTTP_PROXY`/`HTTPS_PROXY` can use Privoxy without SOCKS5
support:

```bash
# Verify traffic exits through Tor
curl --proxy http://127.0.0.1:8118 https://check.torproject.org/api/ip

# Fetch a page
curl --proxy http://127.0.0.1:8118 https://example.com
```

Configure applications via environment variables:

```bash
export HTTP_PROXY=http://127.0.0.1:8118
export HTTPS_PROXY=http://127.0.0.1:8118
curl https://check.torproject.org/api/ip
```

## Shutdown and removal

```bash
# Stop and remove containers (keeps images and volumes)
docker compose down

# Also remove volumes (bootstrapped Tor state — slow next start)
docker compose down -v

# Also remove built images
docker compose down -v --rmi all
```

## Multi-architecture builds

Register QEMU binfmt handlers, then set `PLATFORM` in `.env` and rebuild:

```bash
docker run --privileged --rm tonistiigi/binfmt --install all
PLATFORM=linux/arm64 docker compose build --no-cache
```
