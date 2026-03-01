#!/usr/bin/env python3
"""
tor-bench — Benchmark a Tor SOCKS5 or HTTP proxy.

Measures circuit-setup latency, exit-IP diversity, and throughput by
repeatedly fetching https://check.torproject.org/api/ip through the proxy.

For SOCKS5 targets, each request uses a unique credential pair so that
Tor/Arti assigns it a fresh circuit (stream isolation), giving a true
picture of circuit-establishment cost.

Usage:
  python tor-bench.py --socks5 127.0.0.1:9150 --count 30
  python tor-bench.py --http   127.0.0.1:8118 --count 30
  python tor-bench.py --socks5 127.0.0.1:9150 --count 60 --csv results.csv
"""

import argparse
import csv
import json
import statistics
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

import requests

TARGET = "https://check.torproject.org/api/ip"
DEFAULT_COUNT   = 20
DEFAULT_TIMEOUT = 30
DEFAULT_DELAY   = 0


# ── per-request logic ─────────────────────────────────────────────────────────

def build_proxies(args, circuit_id: str) -> dict:
    """
    Return a requests proxies dict for one request.

    SOCKS5: embed a unique username:password so Tor treats this stream as
    isolated from all others, guaranteeing a fresh circuit each time.

    HTTP:   plain proxy URL — Privoxy handles CONNECT internally; circuit
            isolation is left to Arti's default policy.
    """
    if args.socks5:
        return {"https": f"socks5h://{circuit_id}:{circuit_id}@{args.socks5}",
                "http":  f"socks5h://{circuit_id}:{circuit_id}@{args.socks5}"}
    else:
        return {"https": f"http://{args.http}",
                "http":  f"http://{args.http}"}


def one_request(args) -> dict:
    """
    Perform one proxied request to the target URL.

    Returns a dict with keys:
      success  bool
      latency  float  — seconds from connect to response
      ip       str    — exit IP (only when success=True)
      error    str    — short description (only when success=False)
      ts       float  — monotonic timestamp at request start
    """
    circuit_id = uuid.uuid4().hex
    proxies    = build_proxies(args, circuit_id)
    ts         = time.monotonic()

    try:
        t0  = time.monotonic()
        r   = requests.get(TARGET, proxies=proxies, timeout=args.timeout)
        latency = time.monotonic() - t0
        r.raise_for_status()
        data = r.json()
        if not data.get("IsTor"):
            return {"success": False, "latency": latency, "ts": ts,
                    "error": "not routed through Tor"}
        return {"success": True, "latency": latency, "ts": ts, "ip": data["IP"]}

    except requests.exceptions.ConnectTimeout:
        return {"success": False, "latency": time.monotonic() - ts, "ts": ts,
                "error": "connect timeout"}
    except requests.exceptions.ReadTimeout:
        return {"success": False, "latency": time.monotonic() - ts, "ts": ts,
                "error": "read timeout"}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "latency": time.monotonic() - ts, "ts": ts,
                "error": f"connection error: {e}"}
    except Exception as e:
        return {"success": False, "latency": time.monotonic() - ts, "ts": ts,
                "error": str(e)}


# ── benchmark loop ────────────────────────────────────────────────────────────

def run(args) -> tuple[list[dict], float]:
    results = []
    width   = len(str(args.count))
    t_start = time.monotonic()

    for i in range(1, args.count + 1):
        r = one_request(args)
        results.append(r)

        if r["success"]:
            print(f"  [{i:{width}d}/{args.count}]  {r['latency']:6.2f}s  {r['ip']}")
        else:
            print(f"  [{i:{width}d}/{args.count}]  {r['latency']:6.2f}s  FAIL  {r['error']}")
        sys.stdout.flush()

        if args.delay > 0 and i < args.count:
            time.sleep(args.delay)

    return results, time.monotonic() - t_start


# ── report ────────────────────────────────────────────────────────────────────

def percentile(sorted_data: list[float], pct: float) -> float:
    if not sorted_data:
        return float("nan")
    k = (len(sorted_data) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_data) - 1)
    return sorted_data[lo] + (k - lo) * (sorted_data[hi] - sorted_data[lo])


def consecutive_runs(ips: list[str]) -> list[int]:
    """Return lengths of consecutive same-IP runs."""
    if not ips:
        return []
    runs, cur = [], 1
    for a, b in zip(ips, ips[1:]):
        if a == b:
            cur += 1
        else:
            runs.append(cur)
            cur = 1
    runs.append(cur)
    return runs


def print_report(args, results: list[dict], total_s: float) -> None:
    SEP  = "─" * 62
    SEP2 = "═" * 62

    ok   = [r for r in results if r["success"]]
    fail = [r for r in results if not r["success"]]
    n, n_ok, n_fail = len(results), len(ok), len(fail)

    latencies   = sorted(r["latency"] for r in ok)
    ips         = [r["ip"] for r in ok]
    ip_counts   = Counter(ips)
    unique_ips  = len(ip_counts)
    mins        = total_s / 60

    cpm      = n_ok   / mins if mins else 0
    fpm      = n_fail / mins if mins else 0
    avg_hits = n_ok / unique_ips if unique_ips else 0

    # circuit-reuse: consecutive requests that hit the same exit
    runs          = consecutive_runs(ips)
    reuse_runs    = [r for r in runs if r > 1]
    reuse_pct     = 100 * sum(r - 1 for r in reuse_runs) / n_ok if n_ok else 0

    print()
    print(SEP2)
    print("  BENCHMARK RESULTS")
    print(SEP2)
    proxy_type = "SOCKS5" if args.socks5 else "HTTP"
    proxy_addr = args.socks5 or args.http
    print(f"  Proxy          {proxy_type}  {proxy_addr}")
    print(f"  Target         {TARGET}")
    print(f"  Requests       {n}  (timeout {args.timeout}s"
          + (f", {args.delay}s delay" if args.delay else "") + ")")
    print(f"  Wall time      {total_s:.1f}s")
    print()

    # Throughput
    print(f"  {SEP}")
    print(f"  THROUGHPUT")
    print(f"  {SEP}")
    print(f"  Connections/min    {cpm:6.1f}")
    print(f"  Failures/min       {fpm:6.1f}")
    print(f"  Success rate       {100*n_ok/n:5.1f}%  ({n_ok}/{n})")
    print()

    # Latency
    if latencies:
        mean_l   = statistics.mean(latencies)
        median_l = statistics.median(latencies)
        stdev_l  = statistics.stdev(latencies) if len(latencies) >= 2 else 0.0
        p95_l    = percentile(latencies, 95)
        p99_l    = percentile(latencies, 99)

        print(f"  {SEP}")
        print(f"  LATENCY  (successful requests only, seconds)")
        print(f"  {SEP}")
        print(f"  min       {min(latencies):6.2f}s")
        print(f"  mean      {mean_l:6.2f}s")
        print(f"  median    {median_l:6.2f}s")
        print(f"  stdev     {stdev_l:6.2f}s")
        print(f"  p95       {p95_l:6.2f}s")
        print(f"  p99       {p99_l:6.2f}s")
        print(f"  max       {max(latencies):6.2f}s")
        print()

    # Exit diversity
    print(f"  {SEP}")
    print(f"  EXIT IP DIVERSITY  ({n_ok} successful requests)")
    print(f"  {SEP}")
    print(f"  Unique exits       {unique_ips}")
    print(f"  Avg hits/exit      {avg_hits:.1f}x")
    print(f"  Circuit reuse      {reuse_pct:.1f}%  "
          f"({sum(r-1 for r in reuse_runs)} consecutive-same-IP hits)")
    if ip_counts:
        print(f"  Top exits:")
        for ip, count in ip_counts.most_common(10):
            bar  = "█" * min(count, 30)
            pct  = 100 * count / n_ok
            print(f"    {ip:<22}  {count:3d}x  {pct:4.1f}%  {bar}")
    print()

    # Failures
    if fail:
        print(f"  {SEP}")
        print(f"  FAILURES  ({n_fail} total)")
        print(f"  {SEP}")
        for err, count in Counter(r["error"] for r in fail).most_common():
            print(f"  {count:3d}x  {err}")
        print()

    print(SEP2)


# ── CSV output ────────────────────────────────────────────────────────────────

def write_csv(path: str, results: list[dict]) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["seq", "success", "latency_s",
                                           "exit_ip", "error"])
        w.writeheader()
        for i, r in enumerate(results, 1):
            w.writerow({
                "seq":       i,
                "success":   r["success"],
                "latency_s": f"{r['latency']:.3f}",
                "exit_ip":   r.get("ip", ""),
                "error":     r.get("error", ""),
            })
    print(f"  Raw results written to: {path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Benchmark a Tor SOCKS5 or HTTP proxy.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--socks5", metavar="HOST:PORT",
                       help="SOCKS5 proxy  (e.g. 127.0.0.1:9150 or 127.0.0.1:9050)")
    group.add_argument("--http", metavar="HOST:PORT",
                       help="HTTP proxy    (e.g. 127.0.0.1:8118)")
    p.add_argument("--count",   type=int,   default=DEFAULT_COUNT,
                   metavar="N", help="number of requests to make")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                   metavar="S", help="per-request timeout in seconds")
    p.add_argument("--delay",   type=float, default=DEFAULT_DELAY,
                   metavar="S", help="pause between requests in seconds")
    p.add_argument("--csv",     metavar="FILE",
                   help="write raw per-request data to a CSV file")

    args = p.parse_args()

    proxy_type = "SOCKS5" if args.socks5 else "HTTP"
    proxy_addr = args.socks5 or args.http
    started    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    print(f"tor-bench  {started}")
    print(f"Proxy: {proxy_type} {proxy_addr}  |  "
          f"Requests: {args.count}  |  "
          f"Timeout: {args.timeout}s"
          + (f"  |  Delay: {args.delay}s" if args.delay else ""))
    print()

    results, total_s = run(args)
    print_report(args, results, total_s)

    if args.csv:
        write_csv(args.csv, results)


if __name__ == "__main__":
    main()
