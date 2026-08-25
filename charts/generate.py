"""Generates every chart from results/*.json. One chart per metric family, per
the "charts for every metric family" distinctiveness item. Run after harness/runner.py
has produced results for all five DBs.

Usage: python -m charts.generate
Writes PNGs to charts/output/ (git-committed alongside the results JSON they were
built from, so the README's embedded images don't go stale/missing).
"""
import json
import os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless: this runs in CI/terminal, never needs a display
import matplotlib.pyplot as plt
import numpy as np

RESULTS_DIR = Path(__file__).parent.parent / "results"
# Mirror the runner's client-placement namespacing (BENCH_RESULTS_SUBDIR) so report and
# chart generation read the same run the harness just wrote.
_sub = os.environ.get("BENCH_RESULTS_SUBDIR", "").strip()
if _sub:
    RESULTS_DIR = RESULTS_DIR / _sub
OUTPUT_DIR = Path(__file__).parent / "output"

DB_ORDER = ["cognodb", "neo4j_aura", "memgraph", "falkordb", "nebula"]
READ_WORKLOADS = ["one_hop", "two_hop", "three_hop", "point_lookup", "indexed_lookup", "aggregation"]


def load_results() -> dict:
    results = {}
    for db_key in DB_ORDER:
        path = RESULTS_DIR / f"{db_key}.json"
        if path.exists():
            results[db_key] = json.loads(path.read_text())
    return results


def _labels_and_present(results: dict):
    present = [k for k in DB_ORDER if k in results and "error" not in results[k]]
    labels = [results[k].get("db", k) for k in present]
    return present, labels


def chart_read_latency(results: dict):
    """p50 + p95 bar chart per workload, one figure per workload family."""
    present, labels = _labels_and_present(results)
    if not present:
        return
    for workload in READ_WORKLOADS:
        p50s, p95s = [], []
        for k in present:
            # Use the mean across variance runs (see harness) as the headline bar;
            # the separate variance chart shows the spread around it.
            variance = results[k]["read_suite"]["variance"][workload]
            p50s.append(variance["p50_mean_ms"])
            p95s.append(variance["p95_mean_ms"])

        x = np.arange(len(present))
        width = 0.35
        fig, ax = plt.subplots(figsize=(8, 5))
        ax.bar(x - width / 2, p50s, width, label="p50")
        ax.bar(x + width / 2, p95s, width, label="p95")
        ax.set_ylabel("Latency (ms)")
        ax.set_title(f"{workload.replace('_', ' ').title()} latency (mean across variance runs)")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=20, ha="right")
        ax.legend()
        fig.tight_layout()
        fig.savefig(OUTPUT_DIR / f"latency_{workload}.png", dpi=150)
        plt.close(fig)


def chart_variance_band(results: dict):
    """Run-to-run variance: min/max band + mean, per DB, for one_hop p50 as the
    representative workload (same shape works for any workload)."""
    present, labels = _labels_and_present(results)
    if not present:
        return
    means, mins, maxs = [], [], []
    for k in present:
        v = results[k]["read_suite"]["variance"]["one_hop"]
        means.append(v["p50_mean_ms"])
        mins.append(v["p50_min_ms"])
        maxs.append(v["p50_max_ms"])

    x = np.arange(len(present))
    err_low = np.array(means) - np.array(mins)
    err_high = np.array(maxs) - np.array(means)

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.errorbar(x, means, yerr=[err_low, err_high], fmt="o", capsize=6, markersize=8)
    ax.set_ylabel("one_hop p50 latency (ms)")
    ax.set_title("Run-to-run variance (min/mean/max across repeated full suites)")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=20, ha="right")
    fig.tight_layout()
    fig.savefig(OUTPUT_DIR / "variance_band.png", dpi=150)
    plt.close(fig)


def chart_concurrency_sweep(results: dict):
    """Throughput (qps) vs concurrency, one line per DB — shows where each engine
    plateaus or collapses, per the concurrency-sweep distinctiveness item."""
    present, labels = _labels_and_present(results)
    if not present:
        return
    fig, ax = plt.subplots(figsize=(8, 5))
    for k, label in zip(present, labels):
        sweep = results[k].get("concurrency_sweep", [])
        if not sweep:
            continue
        xs = [pt["n_clients"] for pt in sweep]
        ys = [pt["throughput_qps"] for pt in sweep]
        ax.plot(xs, ys, marker="o", label=label)
    ax.set_xlabel("Concurrent clients")
    ax.set_ylabel("Throughput (ops/sec)")
    ax.set_title("Mixed-workload throughput vs concurrency")
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUTPUT_DIR / "concurrency_sweep.png", dpi=150)
    plt.close(fig)


def chart_ingest_throughput(results: dict):
    present, labels = _labels_and_present(results)
    if not present:
        return
    rels_per_sec = []
    for k in present:
        load = results[k].get("load", {})
        rels_per_sec.append(load.get("rels_per_sec", 0) or 0)

    x = np.arange(len(present))
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar(x, rels_per_sec)
    ax.set_ylabel("Relationships/sec")
    ax.set_title("Ingest throughput")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=20, ha="right")
    fig.tight_layout()
    fig.savefig(OUTPUT_DIR / "ingest_throughput.png", dpi=150)
    plt.close(fig)


def chart_engine_only_latency(results: dict):
    """Raw vs network-subtracted "engine-only" latency, for DBs where RTT was
    measured (Bolt-based). This is the key differentiator metric — isolates the
    managed-DB network tax from actual engine speed."""
    present = [k for k in DB_ORDER if k in results and "error" not in results[k] and "network_rtt" in results[k]]
    if not present:
        return
    labels = [results[k].get("db", k) for k in present]
    raw = [results[k]["read_suite"]["variance"]["one_hop"]["p50_mean_ms"] for k in present]
    rtt = [results[k]["network_rtt"]["p50_ms"] for k in present]
    engine_only = [max(r - t, 0) for r, t in zip(raw, rtt)]

    x = np.arange(len(present))
    width = 0.35
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar(x - width / 2, raw, width, label="raw p50 (incl. network)")
    ax.bar(x + width / 2, engine_only, width, label="engine-only (network-subtracted)")
    ax.set_ylabel("one_hop latency (ms)")
    ax.set_title("Raw vs network-subtracted engine-only latency")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUTPUT_DIR / "engine_only_latency.png", dpi=150)
    plt.close(fig)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results = load_results()
    if not results:
        print("No results found in results/. Run harness/runner.py first.")
        return
    chart_read_latency(results)
    chart_variance_band(results)
    chart_concurrency_sweep(results)
    chart_ingest_throughput(results)
    chart_engine_only_latency(results)
    print(f"Wrote charts to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
