//! tor-bench — Benchmark a Tor SOCKS5 or HTTP proxy.
//!
//! Measures circuit-setup latency, exit-IP diversity, and throughput by
//! repeatedly fetching https://check.torproject.org/api/ip through the proxy.
//!
//! Each request embeds a unique credential pair so that Arti assigns it a
//! fresh Tor circuit (stream isolation), giving a true measure of
//! circuit-establishment cost rather than connection-reuse latency:
//!
//!   SOCKS5  — unique user:pass embedded in the proxy URL  (socks5h://…)
//!   HTTP    — unique user:pass embedded in the proxy URL  (http://…);
//!             reqwest encodes them as Proxy-Authorization: Basic, which
//!             tor-http-proxy decodes and forwards to Arti as SOCKS5 auth.
//!
//! Usage:
//!   tor-bench --socks5 127.0.0.1:9150 --count 50
//!   tor-bench --http   127.0.0.1:8118 --count 50 --concurrency 5
//!   tor-bench --socks5 127.0.0.1:9150 --duration 120
//!   tor-bench --socks5 127.0.0.1:9150 --count 100 --csv results.csv

use clap::Parser;
use reqwest::{Client, Proxy};
use std::collections::HashMap;
use std::io::Write as _;
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};
use std::time::{Duration, Instant};
use tokio::sync::{Mutex, Semaphore};
use tokio::task::JoinSet;

const TARGET: &str = "https://check.torproject.org/api/ip";
const SEP:    &str = "──────────────────────────────────────────────────────────────";
const SEP2:   &str = "══════════════════════════════════════════════════════════════";

// ── CLI ───────────────────────────────────────────────────────────────────────

#[derive(Parser, Debug)]
#[command(
    name = "tor-bench",
    about = "Benchmark a Tor SOCKS5 or HTTP proxy"
)]
struct Cli {
    /// SOCKS5 proxy  (e.g. 127.0.0.1:9150)
    #[arg(long, value_name = "HOST:PORT")]
    socks5: Option<String>,

    /// HTTP proxy  (e.g. 127.0.0.1:8118)
    #[arg(long, value_name = "HOST:PORT")]
    http: Option<String>,

    /// Stop after N requests  (default 20; mutually exclusive with --duration)
    #[arg(long, value_name = "N")]
    count: Option<usize>,

    /// Stop after S seconds — sequential mode only
    #[arg(long, value_name = "S")]
    duration: Option<f64>,

    /// Number of parallel workers  (requires --count)
    #[arg(long, default_value = "1", value_name = "N")]
    concurrency: usize,

    /// Per-request timeout in seconds
    #[arg(long, default_value = "30", value_name = "S")]
    timeout: f64,

    /// Pause between requests in seconds — sequential mode only
    #[arg(long, default_value = "0", value_name = "S")]
    delay: f64,

    /// Write per-request data to a CSV file
    #[arg(long, value_name = "FILE")]
    csv: Option<String>,
}

// ── result type ───────────────────────────────────────────────────────────────

#[derive(Clone, Debug)]
struct Outcome {
    success: bool,
    latency: Duration,
    ip:      Option<String>,
    error:   Option<String>,
}

// ── per-request logic ─────────────────────────────────────────────────────────

/// Build a reqwest Client with a unique circuit_id embedded as proxy
/// credentials.  Both SOCKS5 and HTTP proxy paths pass those credentials
/// to Arti, which uses them as stream-isolation tokens.
fn build_client(cli: &Cli, circuit_id: usize, req_timeout: Duration) -> reqwest::Result<Client> {
    let proxy_url = if let Some(addr) = &cli.socks5 {
        format!("socks5h://{circuit_id:016x}:{circuit_id:016x}@{addr}")
    } else {
        let addr = cli.http.as_deref().unwrap();
        format!("http://{circuit_id:016x}:{circuit_id:016x}@{addr}")
    };
    Client::builder()
        .proxy(Proxy::all(&proxy_url)?)
        .timeout(req_timeout)
        .build()
}

async fn one_request(cli: Arc<Cli>, circuit_id: usize, req_timeout: Duration) -> Outcome {
    let t0 = Instant::now();

    let client = match build_client(&cli, circuit_id, req_timeout) {
        Ok(c) => c,
        Err(e) => {
            return Outcome {
                success: false,
                latency: t0.elapsed(),
                ip:      None,
                error:   Some(format!("client build error: {e}")),
            };
        }
    };

    match client.get(TARGET).send().await {
        Ok(resp) if resp.status().is_success() => {
            let latency = t0.elapsed();
            match resp.text().await {
                Ok(body) => {
                    if !body.contains("\"IsTor\":true") {
                        Outcome {
                            success: false, latency, ip: None,
                            error: Some("not routed through Tor".into()),
                        }
                    } else {
                        Outcome { success: true, latency, ip: extract_ip(&body), error: None }
                    }
                }
                Err(e) => Outcome {
                    success: false, latency, ip: None,
                    error: Some(format!("read error: {e}")),
                },
            }
        }
        Ok(resp) => Outcome {
            success: false,
            latency: t0.elapsed(),
            ip:      None,
            error:   Some(format!("HTTP {}", resp.status())),
        },
        Err(e) => {
            let latency = t0.elapsed();
            let desc = if e.is_timeout() {
                "timeout".to_string()
            } else if e.is_connect() {
                format!("connect error: {e}")
            } else {
                e.to_string()
            };
            Outcome { success: false, latency, ip: None, error: Some(desc) }
        }
    }
}

/// Extract the IP field from {"IsTor":true,"IP":"x.x.x.x"} without pulling
/// in a JSON parser.
fn extract_ip(body: &str) -> Option<String> {
    let key   = "\"IP\":\"";
    let start = body.find(key)? + key.len();
    let end   = body[start..].find('"')? + start;
    Some(body[start..end].to_string())
}

// ── progress display ──────────────────────────────────────────────────────────

fn print_row(seq: usize, total: Option<usize>, r: &Outcome, cpm: f64) {
    let width   = total.map_or_else(|| num_digits(seq), num_digits);
    let total_s = total.map(|t| format!("/{t}")).unwrap_or_default();
    let rate    = if cpm > 0.0 { format!("  ({cpm:.0}/min)") } else { String::new() };

    if r.success {
        let ip = r.ip.as_deref().unwrap_or("?");
        println!("  [{seq:>width$}{total_s}]  {:6.2}s  {ip}{rate}", r.latency.as_secs_f64());
    } else {
        let err = r.error.as_deref().unwrap_or("error");
        println!("  [{seq:>width$}{total_s}]  {:6.2}s  FAIL  {err}{rate}", r.latency.as_secs_f64());
    }
    let _ = std::io::stdout().flush();
}

fn num_digits(mut n: usize) -> usize {
    if n == 0 { return 1; }
    let mut d = 0;
    while n > 0 { d += 1; n /= 10; }
    d
}

// ── benchmark runners ─────────────────────────────────────────────────────────

async fn run_sequential(cli: Arc<Cli>) -> (Vec<Outcome>, Duration) {
    let req_timeout = Duration::from_secs_f64(cli.timeout);
    let delay       = Duration::from_secs_f64(cli.delay);
    let max_count   = cli.count.unwrap_or(usize::MAX);
    let deadline    = cli.duration.map(|d| Instant::now() + Duration::from_secs_f64(d));

    let t_start = Instant::now();
    let mut results = Vec::new();

    loop {
        if results.len() >= max_count { break; }
        if deadline.map_or(false, |d| Instant::now() >= d) { break; }

        let id = results.len() + 1;
        let r  = one_request(cli.clone(), id, req_timeout).await;

        let elapsed = t_start.elapsed();
        let n_ok    = results.iter().filter(|o: &&Outcome| o.success).count()
                      + r.success as usize;
        let cpm     = if elapsed.as_secs_f64() > 1.0 {
            n_ok as f64 / elapsed.as_secs_f64() * 60.0
        } else { 0.0 };

        print_row(id, cli.count, &r, cpm);
        results.push(r);

        if delay > Duration::ZERO {
            tokio::time::sleep(delay).await;
        }
    }

    (results, t_start.elapsed())
}

async fn run_concurrent(cli: Arc<Cli>) -> (Vec<Outcome>, Duration) {
    let count       = cli.count.unwrap_or(20);
    let req_timeout = Duration::from_secs_f64(cli.timeout);
    let sem         = Arc::new(Semaphore::new(cli.concurrency));
    let results     = Arc::new(Mutex::new(vec![None::<Outcome>; count]));
    let n_done      = Arc::new(AtomicUsize::new(0));
    let t_start     = Instant::now();

    let mut join: JoinSet<()> = JoinSet::new();

    for i in 0..count {
        let permit  = sem.clone().acquire_owned().await.expect("semaphore closed");
        let cli     = cli.clone();
        let results = results.clone();
        let n_done  = n_done.clone();

        join.spawn(async move {
            let r   = one_request(cli.clone(), i + 1, req_timeout).await;
            let seq = n_done.fetch_add(1, Ordering::AcqRel) + 1;

            let mut guard = results.lock().await;
            guard[i]      = Some(r.clone());
            let n_ok      = guard.iter().flatten().filter(|o| o.success).count();
            let elapsed   = t_start.elapsed();
            let cpm       = if elapsed.as_secs_f64() > 1.0 {
                n_ok as f64 / elapsed.as_secs_f64() * 60.0
            } else { 0.0 };
            print_row(seq, Some(count), &r, cpm);
            drop(permit);
        });
    }

    while join.join_next().await.is_some() {}

    let final_results = results
        .lock()
        .await
        .iter()
        .flatten()
        .cloned()
        .collect();

    (final_results, t_start.elapsed())
}

// ── statistics helpers ────────────────────────────────────────────────────────

/// Linear-interpolation percentile over a sorted slice.
fn percentile(sorted: &[f64], pct: f64) -> f64 {
    if sorted.is_empty() { return f64::NAN; }
    let k  = (sorted.len() - 1) as f64 * pct / 100.0;
    let lo = k as usize;
    let hi = (lo + 1).min(sorted.len() - 1);
    sorted[lo] + (k - lo as f64) * (sorted[hi] - sorted[lo])
}

fn mean(values: &[f64]) -> f64 {
    values.iter().sum::<f64>() / values.len() as f64
}

/// Sample standard deviation (Bessel-corrected).
fn stdev(values: &[f64]) -> f64 {
    if values.len() < 2 { return 0.0; }
    let m = mean(values);
    let v = values.iter().map(|x| (x - m).powi(2)).sum::<f64>()
            / (values.len() - 1) as f64;
    v.sqrt()
}

/// Returns (repeat_at, ip, first_seen_at) for the first repeated exit IP,
/// or None if all IPs were distinct.
fn find_first_repeat(ips: &[String]) -> Option<(usize, &str, usize)> {
    let mut seen: HashMap<&str, usize> = HashMap::new();
    for (i, ip) in ips.iter().enumerate() {
        if let Some(&first) = seen.get(ip.as_str()) {
            return Some((i + 1, ip.as_str(), first));
        }
        seen.insert(ip.as_str(), i + 1);
    }
    None
}

/// Returns (percentage, hit_count) of consecutive same-IP requests.
fn consec_reuse_stats(ips: &[String]) -> (f64, usize) {
    if ips.len() < 2 { return (0.0, 0); }
    let hits: usize = ips.windows(2).filter(|w| w[0] == w[1]).count();
    (100.0 * hits as f64 / ips.len() as f64, hits)
}

/// Returns the top-N exit IPs by hit count, sorted by count descending.
fn top_ips(ips: &[String], n: usize) -> Vec<(&str, usize)> {
    let mut counts: HashMap<&str, usize> = HashMap::new();
    for ip in ips { *counts.entry(ip.as_str()).or_insert(0) += 1; }
    let mut v: Vec<_> = counts.into_iter().collect();
    v.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(b.0)));
    v.truncate(n);
    v
}

// ── report ────────────────────────────────────────────────────────────────────

fn print_report(cli: &Cli, results: &[Outcome], total_s: Duration) {
    let ok:    Vec<&Outcome> = results.iter().filter(|r| r.success).collect();
    let fail:  Vec<&Outcome> = results.iter().filter(|r| !r.success).collect();
    let n      = results.len();
    let n_ok   = ok.len();
    let n_fail = fail.len();
    let mins   = total_s.as_secs_f64() / 60.0;

    let mut lats: Vec<f64> = ok.iter().map(|r| r.latency.as_secs_f64()).collect();
    lats.sort_by(f64::total_cmp);

    let ips: Vec<String> = ok.iter().filter_map(|r| r.ip.clone()).collect();
    let unique_ips = {
        let mut s: std::collections::HashSet<&str> = std::collections::HashSet::new();
        ips.iter().for_each(|ip| { s.insert(ip.as_str()); });
        s.len()
    };

    let cpm      = if mins > 0.0 { n_ok   as f64 / mins } else { 0.0 };
    let fpm      = if mins > 0.0 { n_fail as f64 / mins } else { 0.0 };
    let avg_hits = if unique_ips > 0 { n_ok as f64 / unique_ips as f64 } else { 0.0 };

    let proxy_type  = if cli.socks5.is_some() { "SOCKS5" } else { "HTTP" };
    let proxy_addr  = cli.socks5.as_deref().or(cli.http.as_deref()).unwrap_or("");
    let concur_note = if cli.concurrency > 1 {
        format!(", {} concurrent", cli.concurrency)
    } else {
        String::new()
    };
    let delay_note = if cli.delay > 0.0 {
        format!(", {}s delay", cli.delay)
    } else {
        String::new()
    };

    println!();
    println!("{SEP2}");
    println!("  BENCHMARK RESULTS");
    println!("{SEP2}");
    println!("  Proxy          {proxy_type}  {proxy_addr}");
    println!("  Target         {TARGET}");
    println!("  Requests       {n}  (timeout {}s{concur_note}{delay_note})", cli.timeout);
    println!("  Wall time      {:.1}s", total_s.as_secs_f64());
    println!();

    println!("  {SEP}");
    println!("  THROUGHPUT");
    println!("  {SEP}");
    println!("  Circuits/min       {cpm:6.1}");
    println!("  Failures/min       {fpm:6.1}");
    println!("  Success rate       {:5.1}%  ({n_ok}/{n})", 100.0 * n_ok as f64 / n as f64);
    println!();

    if !lats.is_empty() {
        println!("  {SEP}");
        println!("  LATENCY  (successful requests, seconds)");
        println!("  {SEP}");
        println!("  min       {:6.2}s", lats[0]);
        println!("  mean      {:6.2}s", mean(&lats));
        println!("  median    {:6.2}s", percentile(&lats, 50.0));
        println!("  stdev     {:6.2}s", stdev(&lats));
        println!("  p95       {:6.2}s", percentile(&lats, 95.0));
        println!("  p99       {:6.2}s", percentile(&lats, 99.0));
        println!("  max       {:6.2}s", lats[lats.len() - 1]);
        println!();
    }

    println!("  {SEP}");
    println!("  EXIT IP DIVERSITY  ({n_ok} successful requests)");
    println!("  {SEP}");
    println!("  Unique exits       {unique_ips}");
    println!("  Avg hits/exit      {avg_hits:.1}x");
    match find_first_repeat(&ips) {
        Some((at, ip, first)) => {
            println!("  First repeat       request #{at}  ({ip}, first seen at #{first})");
        }
        None => {
            println!("  First repeat       none in {n_ok} requests");
        }
    }
    let (reuse_pct, reuse_hits) = consec_reuse_stats(&ips);
    println!("  Consec. reuse      {reuse_pct:.1}%  ({reuse_hits} consecutive same-IP hits)");
    if !ips.is_empty() {
        println!("  Top exits:");
        for (ip, count) in top_ips(&ips, 10) {
            let bar = "█".repeat(count.min(30));
            let pct = 100.0 * count as f64 / n_ok as f64;
            println!("    {ip:<22}  {count:3}x  {pct:4.1}%  {bar}");
        }
    }
    println!();

    if !fail.is_empty() {
        println!("  {SEP}");
        println!("  FAILURES  ({n_fail} total)");
        println!("  {SEP}");
        let mut err_counts: HashMap<&str, usize> = HashMap::new();
        for r in &fail {
            *err_counts.entry(r.error.as_deref().unwrap_or("unknown")).or_insert(0) += 1;
        }
        let mut errs: Vec<_> = err_counts.into_iter().collect();
        errs.sort_by(|a, b| b.1.cmp(&a.1));
        for (e, c) in errs {
            println!("  {c:3}x  {e}");
        }
        println!();
    }

    println!("{SEP2}");
}

// ── CSV output ────────────────────────────────────────────────────────────────

fn write_csv(path: &str, results: &[Outcome]) -> std::io::Result<()> {
    use std::fs::File;
    use std::io::{BufWriter, Write};

    let mut f = BufWriter::new(File::create(path)?);
    writeln!(f, "seq,success,latency_s,exit_ip,error")?;
    for (i, r) in results.iter().enumerate() {
        writeln!(
            f,
            "{},{},{:.3},{},{}",
            i + 1,
            r.success,
            r.latency.as_secs_f64(),
            r.ip.as_deref().unwrap_or(""),
            r.error.as_deref().unwrap_or(""),
        )?;
    }
    println!("  Raw results written to: {path}");
    Ok(())
}

// ── main ──────────────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() {
    let mut cli = Cli::parse();

    // Validate mutually exclusive proxy arguments
    match (&cli.socks5, &cli.http) {
        (None, None) => {
            eprintln!("error: one of --socks5 or --http is required");
            std::process::exit(1);
        }
        (Some(_), Some(_)) => {
            eprintln!("error: --socks5 and --http are mutually exclusive");
            std::process::exit(1);
        }
        _ => {}
    }

    // Validate concurrency/duration combination
    if cli.concurrency > 1 {
        if cli.duration.is_some() {
            eprintln!("error: --duration is not supported with --concurrency > 1; use --count");
            std::process::exit(1);
        }
        if cli.delay > 0.0 {
            eprintln!("error: --delay is not supported with --concurrency > 1");
            std::process::exit(1);
        }
    }

    // Default to 20 requests if neither --count nor --duration was given
    if cli.count.is_none() && cli.duration.is_none() {
        cli.count = Some(20);
    }

    let proxy_type = if cli.socks5.is_some() { "SOCKS5" } else { "HTTP" };
    let proxy_addr = cli.socks5.as_deref().or(cli.http.as_deref()).unwrap_or("");
    let term_desc  = match (cli.count, cli.duration) {
        (Some(n), _) => format!("{n} requests"),
        (_, Some(s)) => format!("{s}s"),
        _            => unreachable!(),
    };
    let concur_note = if cli.concurrency > 1 {
        format!("  |  Concurrency: {}", cli.concurrency)
    } else {
        String::new()
    };

    use std::time::{SystemTime, UNIX_EPOCH};
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let (y, mo, d, h, mi, s) = unix_to_ymd_hms(now);
    println!(
        "tor-bench  {y:04}-{mo:02}-{d:02} {h:02}:{mi:02}:{s:02} UTC"
    );
    println!(
        "Proxy: {proxy_type} {proxy_addr}  |  {term_desc}  |  Timeout: {}s{concur_note}{}",
        cli.timeout,
        if cli.delay > 0.0 { format!("  |  Delay: {}s", cli.delay) } else { String::new() },
    );
    println!();

    let cli = Arc::new(cli);
    let (results, total_s) = if cli.concurrency > 1 {
        run_concurrent(cli.clone()).await
    } else {
        run_sequential(cli.clone()).await
    };

    print_report(&cli, &results, total_s);

    if let Some(path) = &cli.csv {
        if let Err(e) = write_csv(path, &results) {
            eprintln!("warning: failed to write CSV: {e}");
        }
    }
}

/// Minimal UTC timestamp decomposition without a time-crate dependency.
fn unix_to_ymd_hms(ts: u64) -> (u32, u32, u32, u32, u32, u32) {
    let s   = ts % 60;
    let ts  = ts / 60;
    let mi  = ts % 60;
    let ts  = ts / 60;
    let h   = ts % 24;
    let mut days = ts / 24;

    // Gregorian calendar epoch: 1970-01-01
    let mut y = 1970u32;
    loop {
        let dy = if is_leap(y) { 366 } else { 365 };
        if days < dy { break; }
        days -= dy;
        y += 1;
    }
    let months = [31u64, if is_leap(y) { 29 } else { 28 }, 31, 30, 31, 30,
                  31, 31, 30, 31, 30, 31];
    let mut mo = 1u32;
    for &dm in &months {
        if days < dm { break; }
        days -= dm;
        mo += 1;
    }
    (y, mo, days as u32 + 1, h as u32, mi as u32, s as u32)
}

fn is_leap(y: u32) -> bool {
    (y % 4 == 0 && y % 100 != 0) || y % 400 == 0
}
