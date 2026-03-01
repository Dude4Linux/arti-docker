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

Rust and Cargo are **not** required on the host. The build uses a multi-stage
Dockerfile whose first stage is the official `rust` image; all compilation happens
inside Docker.

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

## Benchmarking

`bench/tor-bench.py` measures circuit-setup latency, exit-IP diversity, and
throughput. It works against this project or the Tor proxy in any other Docker
Compose stack — just point it at the right address.

### Setup

```bash
cd bench
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Running

```bash
# SOCKS5 — arti (this project), fixed count
python tor-bench.py --socks5 127.0.0.1:9150 --count 50

# SOCKS5 — arti, run for a fixed duration
python tor-bench.py --socks5 127.0.0.1:9150 --duration 120

# SOCKS5 — concurrent requests for throughput measurement
python tor-bench.py --socks5 127.0.0.1:9150 --count 40 --concurrency 5

# HTTP — Privoxy → Arti (requires http-proxy profile)
python tor-bench.py --http 127.0.0.1:8118 --count 30

# SOCKS5 — amass tor-proxy (port 9050, run from the amass network or after publishing the port)
python tor-bench.py --socks5 127.0.0.1:9050 --count 30

# Save raw per-request data to CSV for further analysis
python tor-bench.py --socks5 127.0.0.1:9150 --count 100 --csv results.csv
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--socks5 HOST:PORT` | — | SOCKS5 proxy target (mutually exclusive with `--http`) |
| `--http HOST:PORT` | — | HTTP proxy target (mutually exclusive with `--socks5`) |
| `--count N` | `20` | Stop after N requests (mutually exclusive with `--duration`) |
| `--duration S` | — | Stop after S seconds — sequential mode only (mutually exclusive with `--count`) |
| `--concurrency N` | `1` | Number of parallel workers — requires `--count` |
| `--timeout S` | `30` | Per-request timeout in seconds |
| `--delay S` | `0` | Pause between requests in seconds — sequential mode only |
| `--csv FILE` | — | Write per-request data to a CSV file |

### What is measured

Each request fetches `https://check.torproject.org/api/ip` through the proxy. For
SOCKS5 targets, a unique credential pair is sent with each connection so that
Tor/Arti assigns it a fresh circuit (stream isolation), giving a true measure of
circuit-establishment cost rather than reuse latency. Progress lines include a live
circuits/min counter that updates as results come in.

HTTP proxy targets (Privoxy) cannot carry per-request isolation credentials —
Privoxy's `forward-socks5t` directive supports static credentials only. Arti
therefore reuses circuits across connections. Expect near-100% circuit reuse and a
single exit IP in `--http` mode. There is no Privoxy configuration that changes this.

**Report includes:**

- Circuits/min and failures/min
- Success rate
- Latency distribution: min / mean / median / stdev / p95 / p99 / max
- Unique exit IP count and average hits per exit
- First repeat: which request number first returned a previously seen exit IP
- Consecutive circuit-reuse percentage
- Top exit IPs with hit counts
- Failure breakdown by error type

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
