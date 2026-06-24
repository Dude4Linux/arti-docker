# arti-docker

A Docker setup for running [Arti](https://gitlab.torproject.org/tpo/core/arti) — Tor
reimplemented in Rust — as both a SOCKS5 proxy and an HTTP CONNECT proxy. Both
proxies run in a single container. The HTTP proxy (`tor-http-proxy`) is also written
in Rust and built in the same stage as Arti.

```
client → SOCKS5 → Arti:9150 → Tor
client → HTTP   → tor-http-proxy:8118 → Arti:9150 → Tor
```

**Circuit isolation**: both proxy paths support per-request Tor circuit isolation.
For SOCKS5, embed unique credentials in the proxy URL (`socks5h://user:pass@…`). For
HTTP, send a `Proxy-Authorization: Basic` header; `tor-http-proxy` extracts the
credentials and forwards them to Arti as SOCKS5 auth, so each unique
username/password gets its own Tor circuit.

## Requirements

- Docker Engine 24+ with Compose V2 (`docker compose`)
- x86-64, arm64, or armv7 host (see [Configuration](#configuration))

Rust and Cargo are **not** required on the host. The build uses a multi-stage
Dockerfile whose first stage is the official `rust` image; all compilation happens
inside Docker.

## Installation

```bash
git clone https://github.com/Dude4Linux/arti-docker.git
cd arti-docker
docker/refresh-bridges.sh
docker compose up -d
```

`refresh-bridges.sh` handles first-time setup automatically:

- Copies `.env.template` → `.env` if `.env` does not yet exist
- Copies `bridges.txt.template` → `docker/bridges.txt` if that file does not yet exist
- Fetches fresh obfs4 bridges from `bridges.torproject.org` and writes them into
  `docker/bridges.txt`

Both `.env` and `docker/bridges.txt` are gitignored, so your settings and bridges
survive `git pull`.

Edit `.env` to suit your environment before starting (all settings are optional —
the defaults work for local use):

```bash
$EDITOR .env
docker compose up -d
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
| `HTTP_PORT` | `8118` | Host port for the HTTP CONNECT proxy |
| `LOG_DRIVER` | *(none)* | Set to `json-file` to enable container logging |
| `TOR_MODE` | `auto` | `auto` probes for filtering; `direct` forces no bridges; `bridge` forces bridges |

### BIND_ADDR

By default ports are bound to `127.0.0.1` — only the machine running Docker can
connect. To allow access from other hosts on your local network, set `BIND_ADDR` to
your host's LAN IP:

```
BIND_ADDR=192.168.1.100
```

> **Warning:** Do not set `BIND_ADDR=0.0.0.0` unless you are behind a firewall.
> Exposing the proxy to the internet makes it an open relay — anyone can route
> arbitrary traffic through your Tor exit bandwidth.

## Censored networks (bridges)

Some ISPs — including T-Mobile Home Internet, several carrier-grade-NAT mobile
operators, and many corporate networks — block direct connections to known Tor
relay IPs. On such networks Arti pulls the consensus from a fallback directory
but then can't open channels to any guard, and the container stays unhealthy
with `Could not connect to guard` warnings in the log.

The container handles this automatically at every startup:

1. **Filter detection** — probe three well-known Tor relay IPs on `:443`.
2. If any probe succeeds → Arti runs in **direct mode** (no overhead).
3. If all probes fail → switch to **bridge mode** using `lyrebird` (obfs4), then:
   - If `docker/bridges.txt` has no active lines → fetch fresh bridges from
     `bridges.torproject.org` automatically.
   - If every configured bridge is TCP-unreachable → fetch fresh bridges
     automatically.
4. **Health monitoring** — run `health-probe` every 5 minutes. After 3
   consecutive failures the entrypoint exits, Docker restarts the container,
   and step 3 runs again with the failed bridges already known-blocked.

Bridges are not bundled because any default set would be blocked within days.
`refresh-bridges.sh` (used during installation) fetches them on demand, and the
container auto-refreshes them whenever they stop working.

To manually rotate bridges at any time:

```bash
docker/refresh-bridges.sh          # fetch if needed, restart if running
docker/refresh-bridges.sh --force  # always fetch fresh, restart
docker/refresh-bridges.sh --check  # report reachability only, no changes
```

`TOR_MODE` overrides the filter probe:

- `auto` *(default)* — probe and decide
- `direct` — never use bridges; fail loudly if direct is filtered
- `bridge` — always use bridges; skip the probe (saves ~3–9s at startup
  when you already know you're on a filtered network)

## Starting and stopping

```bash
# Build images and start (first run compiles Arti and tor-http-proxy from
# source — takes several minutes)
docker compose up -d

# Enable logging then follow logs from both processes
LOG_DRIVER=json-file docker compose up -d
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

Force a fresh Tor circuit per connection by embedding unique credentials:

```bash
export ALL_PROXY=socks5h://$(uuidgen):x@127.0.0.1:9150
```

### HTTP proxy (tor-http-proxy)

Any tool that honours `HTTP_PROXY`/`HTTPS_PROXY` can use the HTTP proxy without
SOCKS5 support:

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

Force a fresh Tor circuit per connection by sending unique proxy credentials:

```bash
curl --proxy http://$(uuidgen):x@127.0.0.1:8118 https://check.torproject.org/api/ip
```

## Benchmarking

`bench/tor-bench` measures circuit-setup latency, exit-IP diversity, and throughput.
It works against this project or the Tor proxy in any other Docker Compose stack —
just point it at the right address.

### Setup

Requires Rust and Cargo on the host (only for the benchmark tool — the container
itself has no such requirement):

```bash
cd bench
cargo build --release
```

The compiled binary is at `bench/target/release/tor-bench`.

### Running

```bash
# SOCKS5 — arti (this project), fixed count
./target/release/tor-bench --socks5 127.0.0.1:9150 --count 50

# SOCKS5 — arti, run for a fixed duration
./target/release/tor-bench --socks5 127.0.0.1:9150 --duration 120

# SOCKS5 — concurrent requests for throughput measurement
./target/release/tor-bench --socks5 127.0.0.1:9150 --count 40 --concurrency 5

# HTTP — tor-http-proxy → Arti (circuit isolation via Proxy-Authorization)
./target/release/tor-bench --http 127.0.0.1:8118 --count 30

# SOCKS5 — amass tor-proxy (port 9050)
./target/release/tor-bench --socks5 127.0.0.1:9050 --count 30

# Save raw per-request data to CSV for further analysis
./target/release/tor-bench --socks5 127.0.0.1:9150 --count 100 --csv results.csv
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

Each request fetches `https://check.torproject.org/api/ip` through the proxy. A
unique credential pair is sent with each connection (via SOCKS5 auth or
`Proxy-Authorization`) so that Arti assigns each request a fresh Tor circuit, giving
a true measure of circuit-establishment cost. Progress lines include a live
circuits/min counter that updates as results come in.

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
