#!/usr/bin/env python3
"""
tor-bench — Benchmark a Tor SOCKS5 or HTTP proxy.

Measures circuit-setup latency, exit-IP diversity, and throughput by
repeatedly fetching https://check.torproject.org/api/ip through the proxy.

Each request uses a unique credential pair so that Arti assigns it a fresh
Tor circuit (stream isolation), giving a true measure of circuit-establishment
cost rather than reuse latency.

  SOCKS5:  credentials embedded in the proxy URL  socks5h://id:id@host:port
  HTTP:    credentials sent as Proxy-Authorization: Basic header, which
           tor-http-proxy decodes and forwards to Arti as SOCKS5 auth

Usage:
  # sequential, fixed count
  python tor-bench.py --socks5 127.0.0.1:9150 --count 50

  # sequential, fixed duration
  python tor-bench.py --socks5 127.0.0.1:9150 --duration 120

  # concurrent — good for circuits/min throughput measurement
  python tor-bench.py --socks5 127.0.0.1:9150 --count 100 --concurrency 5

  # save raw data for further analysis
  python tor-bench.py --socks5 127.0.0.1:9150 --count 100 --csv results.csv
"""

import argparse
import csv
import statistics
import sys
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

TARGET          = "https://check.torproject.org/api/ip"
DEFAULT_COUNT   = 20
DEFAULT_TIMEOUT = 30
DEFAULT_DELAY   = 0

_print_lock = threading.Lock()


# ── per-request logic ─────────────────────────────────────────────────────────

def build_proxies(args, circuit_id: str) -> dict:
    """
    Return a requests proxies dict for one request.

    Both modes embed a unique circuit_id as credentials so Arti assigns each
    request its own fresh Tor circuit:

    SOCKS5: credentials in the URL  (socks5h://id:id@host:port)
    HTTP:   credentials in the URL  (http://id:id@host:port) — requests
            encodes these as a Proxy-Authorization: Basic header, which
            tor-http-proxy decodes and forwards to Arti as SOCKS5 auth
    """
    if args.socks5:
        return {"https": f"socks5h://{circuit_id}:{circuit_id}@{args.socks5}",
                "http":  f"socks5h://{circuit_id}:{circuit_id}@{args.socks5}"}
    else:
        return {"https": f"http://{circuit_id}:{circuit_id}@{args.http}",
                "http":  f"http://{circuit_id}:{circuit_id}@{args.http}"}


def one_request(args) -> dict:
    """
    Perform one proxied request.

    Returns a dict with keys:
      success  bool
      latency  float  — seconds from connect to response
      ip       str    — exit IP (only when success=True)
      error    str    — short description (only when success=False)
    """
    circuit_id = uuid.uuid4().hex
    proxies    = build_proxies(args, circuit_id)

    try:
        t0 = time.monotonic()
        r  = requests.get(TARGET, proxies=proxies, timeout=args.timeout)
        latency = time.monotonic() - t0
        r.raise_for_status()
        data = r.json()
        if not data.get("IsTor"):
            return {"success": False, "latency": latency,
                    "error": "not routed through Tor"}
        return {"success": True, "latency": latency, "ip": data["IP"]}

    except requests.exceptions.ConnectTimeout:
        return {"success": False, "latency": time.monotonic() - t0,
                "error": "connect timeout"}
    except requests.exceptions.ReadTimeout:
        return {"success": False, "latency": time.monotonic() - t0,
                "error": "read timeout"}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "latency": time.monotonic() - t0,
                "error": f"connection error: {e}"}
    except Exception as e:
        return {"success": False, "latency": time.monotonic() - t0,
                "error": str(e)}


# ── benchmark loops ───────────────────────────────────────────────────────────

def _print_row(seq: int, total: int | None, r: dict, cpm: float) -> None:
    label = f"{seq:{len(str(total or seq))}d}"
    total_s = f"/{total}" if total else ""
    rate = f"  ({cpm:.0f}/min)" if cpm else ""
    if r["success"]:
        print(f"  [{label}{total_s}]  {r['latency']:6.2f}s  {r['ip']}{rate}")
    else:
        print(f"  [{label}{total_s}]  {r['latency']:6.2f}s  FAIL  {r['error']}{rate}")
    sys.stdout.flush()


def run_sequential(args) -> tuple[list[dict], float]:
    """Sequential requests, supports both --count and --duration."""
    results  = []
    t_start  = time.monotonic()
    i        = 0
    use_count    = args.count is not None
    use_duration = args.duration is not None

    while True:
        if use_count and i >= args.count:
            break
        if use_duration and (time.monotonic() - t_start) >= args.duration:
            break

        r = one_request(args)
        i += 1
        results.append(r)

        elapsed = time.monotonic() - t_start
        n_ok    = sum(1 for x in results if x["success"])
        cpm     = n_ok / (elapsed / 60) if elapsed > 1 else 0
        _print_row(i, args.count, r, cpm)

        if args.delay > 0:
            time.sleep(args.delay)

    return results, time.monotonic() - t_start


def run_concurrent(args) -> tuple[list[dict], float]:
    """
    Concurrent requests using a thread pool.

    Results are collected in completion order for live display; the returned
    list is sorted back into submission order for the first-repeat analysis.
    """
    count    = args.count
    results  = [None] * count          # pre-allocate in submission order
    t_start  = time.monotonic()
    done_ctr = [0]

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(one_request, args): idx
                   for idx in range(count)}

        for future in as_completed(futures):
            idx = futures[future]
            r   = future.result()
            results[idx] = r

            with _print_lock:
                done_ctr[0] += 1
                n_done   = done_ctr[0]
                elapsed  = time.monotonic() - t_start
                n_ok     = sum(1 for x in results if x and x["success"])
                cpm      = n_ok / (elapsed / 60) if elapsed > 1 else 0
                _print_row(n_done, count, r, cpm)

    return results, time.monotonic() - t_start


def run(args) -> tuple[list[dict], float]:
    if args.concurrency > 1:
        return run_concurrent(args)
    return run_sequential(args)


# ── analysis helpers ──────────────────────────────────────────────────────────

def percentile(sorted_data: list[float], pct: float) -> float:
    if not sorted_data:
        return float("nan")
    k  = (len(sorted_data) - 1) * pct / 100
    lo = int(k)
    hi = min(lo + 1, len(sorted_data) - 1)
    return sorted_data[lo] + (k - lo) * (sorted_data[hi] - sorted_data[lo])


def find_first_repeat(ips: list[str]) -> tuple[int, str, int] | tuple[None, None, None]:
    """
    Scan IPs in order; return (repeat_at, ip, first_seen_at) for the first
    repetition, or (None, None, None) if no repeat was found.
    """
    seen: dict[str, int] = {}
    for i, ip in enumerate(ips, 1):
        if ip in seen:
            return i, ip, seen[ip]
        seen[ip] = i
    return None, None, None


def consecutive_runs(ips: list[str]) -> list[int]:
    """Lengths of consecutive same-IP runs."""
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


# ── report ────────────────────────────────────────────────────────────────────

def print_report(args, results: list[dict], total_s: float) -> None:
    SEP  = "─" * 62
    SEP2 = "═" * 62

    ok    = [r for r in results if r and r["success"]]
    fail  = [r for r in results if r and not r["success"]]
    n, n_ok, n_fail = len(results), len(ok), len(fail)

    latencies  = sorted(r["latency"] for r in ok)
    ips        = [r["ip"] for r in ok]
    ip_counts  = Counter(ips)
    unique_ips = len(ip_counts)
    mins       = total_s / 60

    cpm      = n_ok   / mins if mins else 0
    fpm      = n_fail / mins if mins else 0
    avg_hits = n_ok / unique_ips if unique_ips else 0

    repeat_at, repeat_ip, first_seen_at = find_first_repeat(ips)

    runs       = consecutive_runs(ips)
    reuse_hits = sum(r - 1 for r in runs if r > 1)
    reuse_pct  = 100 * reuse_hits / n_ok if n_ok else 0

    print()
    print(SEP2)
    print("  BENCHMARK RESULTS")
    print(SEP2)
    proxy_type = "SOCKS5" if args.socks5 else "HTTP"
    proxy_addr = args.socks5 or args.http
    concur_note = f", {args.concurrency} concurrent" if args.concurrency > 1 else ""
    delay_note  = f", {args.delay}s delay" if args.delay else ""
    print(f"  Proxy          {proxy_type}  {proxy_addr}")
    print(f"  Target         {TARGET}")
    print(f"  Requests       {n}  "
          f"(timeout {args.timeout}s{concur_note}{delay_note})")
    print(f"  Wall time      {total_s:.1f}s")
    print()

    # Throughput
    print(f"  {SEP}")
    print(f"  THROUGHPUT")
    print(f"  {SEP}")
    print(f"  Circuits/min       {cpm:6.1f}")
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
        print(f"  LATENCY  (successful requests, seconds)")
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
    if repeat_at is not None:
        print(f"  First repeat       request #{repeat_at}  "
              f"({repeat_ip}, first seen at #{first_seen_at})")
    else:
        print(f"  First repeat       none in {n_ok} requests")
    print(f"  Consec. reuse      {reuse_pct:.1f}%  "
          f"({reuse_hits} consecutive same-IP hits)")
    if ip_counts:
        print(f"  Top exits:")
        for ip, count in ip_counts.most_common(10):
            bar = "█" * min(count, 30)
            pct = 100 * count / n_ok
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
            if r is None:
                continue
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

    # Proxy target (required, mutually exclusive)
    proxy = p.add_mutually_exclusive_group(required=True)
    proxy.add_argument("--socks5", metavar="HOST:PORT",
                       help="SOCKS5 proxy  (e.g. 127.0.0.1:9150 or 127.0.0.1:9050)")
    proxy.add_argument("--http", metavar="HOST:PORT",
                       help="HTTP proxy    (e.g. 127.0.0.1:8118)")

    # Termination (mutually exclusive)
    term = p.add_mutually_exclusive_group()
    term.add_argument("--count", type=int, metavar="N",
                      help=f"stop after N requests (default {DEFAULT_COUNT})")
    term.add_argument("--duration", type=float, metavar="S",
                      help="stop after S seconds (sequential only)")

    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="number of parallel requests (requires --count)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="S",
                   help="per-request timeout in seconds")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY, metavar="S",
                   help="pause between requests in seconds (sequential only)")
    p.add_argument("--csv", metavar="FILE",
                   help="write per-request data to a CSV file")

    args = p.parse_args()

    # Validate combinations
    if args.concurrency > 1:
        if args.duration:
            p.error("--duration is not supported with --concurrency > 1; use --count")
        if args.count is None:
            args.count = DEFAULT_COUNT
    elif args.count is None and args.duration is None:
        args.count = DEFAULT_COUNT

    proxy_type = "SOCKS5" if args.socks5 else "HTTP"
    proxy_addr = args.socks5 or args.http
    started    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    term_desc  = (f"{args.count} requests" if args.count is not None
                  else f"{args.duration}s")
    concur_desc = (f"  |  Concurrency: {args.concurrency}"
                   if args.concurrency > 1 else "")

    print(f"tor-bench  {started}")
    print(f"Proxy: {proxy_type} {proxy_addr}  |  "
          f"{term_desc}  |  Timeout: {args.timeout}s"
          + concur_desc
          + (f"  |  Delay: {args.delay}s" if args.delay else ""))
    print()

    results, total_s = run(args)
    print_report(args, results, total_s)

    if args.csv:
        write_csv(args.csv, results)


if __name__ == "__main__":
    main()
