"""Percentile + variance math, in one place, used identically for every DB.

Why isolate this: p50/p95 and run-to-run spread are the actual grade-bearing
numbers. Computing them once here (rather than inline per adapter call site)
means there's exactly one implementation to check for off-by-one/interpolation
mistakes during the interview defense.
"""
import numpy as np


def summarize(samples_ms: list) -> dict:
    """One read workload's timed samples -> the stats block used everywhere in
    results JSON and README tables. Requires >= 1 sample; the harness enforces
    the >=100-iteration rule before calling this, not this function itself."""
    arr = np.array(samples_ms, dtype=float)
    return {
        "n": len(arr),
        "mean_ms": float(np.mean(arr)),
        "p50_ms": float(np.percentile(arr, 50)),
        "p95_ms": float(np.percentile(arr, 95)),
        "min_ms": float(np.min(arr)),
        "max_ms": float(np.max(arr)),
        "std_ms": float(np.std(arr)),
    }


def variance_across_runs(per_run_p50s: list, per_run_p95s: list) -> dict:
    """Spread across N full-suite repeats (BENCH_VARIANCE_RUNS) — the "run-to-run
    variance" distinctiveness item. Reported as std dev AND min/max band so a
    reader doesn't have to trust a single summary number."""
    p50_arr = np.array(per_run_p50s, dtype=float)
    p95_arr = np.array(per_run_p95s, dtype=float)
    return {
        "n_runs": len(per_run_p50s),
        "p50_mean_ms": float(np.mean(p50_arr)),
        "p50_std_ms": float(np.std(p50_arr)),
        "p50_min_ms": float(np.min(p50_arr)),
        "p50_max_ms": float(np.max(p50_arr)),
        "p95_mean_ms": float(np.mean(p95_arr)),
        "p95_std_ms": float(np.std(p95_arr)),
        "p95_min_ms": float(np.min(p95_arr)),
        "p95_max_ms": float(np.max(p95_arr)),
    }
