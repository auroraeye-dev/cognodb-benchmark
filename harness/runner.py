"""The single harness every adapter runs through. Written once against
GraphDBAdapter's abstract interface (adapters/base.py) — it never branches on
which concrete DB it's talking to, which is what makes "same workload, same
measurement code, same client" true by construction.

Usage:
    python -m harness.runner --db cognodb
    python -m harness.runner --db all

Emits results/<db_key>.json — raw numbers, git-committed for reproducibility
(report.py turns these into README tables and charts).
"""
import argparse
import json
import os
import random
import time
from pathlib import Path

from dotenv import load_dotenv

from dataset.sample import Dataset
from harness.stats import summarize, variance_across_runs

load_dotenv()

# Results are namespaced by WHERE THE CLIENT RAN, because client placement changes the
# numbers more than any engine difference in this benchmark (a 302 ms RTT dwarfs every
# engine time we measure). Keeping laptop and in-region runs in separate directories means
# a later run can never overwrite an earlier one, and the two remain directly comparable.
# Default preserves the original flat layout so existing results stay where they are.
RESULTS_DIR = Path(__file__).parent.parent / "results"
_subdir = os.environ.get("BENCH_RESULTS_SUBDIR", "").strip()
if _subdir:
    RESULTS_DIR = RESULTS_DIR / _subdir

READ_WORKLOADS = ["one_hop", "two_hop", "three_hop", "point_lookup", "indexed_lookup", "aggregation"]


def _env_int(key, default):
    return int(os.environ.get(key, default))


def _env_list_int(key, default):
    raw = os.environ.get(key)
    if not raw:
        return default
    return [int(x) for x in raw.split(",")]


def build_adapter(db_key: str):
    """Factory: env vars -> a connected-but-not-yet-loaded adapter instance.
    Kept out of __main__ so harness code (and tests) can build an adapter
    without going through argparse."""
    if db_key == "cognodb":
        from adapters.cognodb import CognoDBAdapter
        return CognoDBAdapter(
            uri=os.environ["COGNODB_URI"],
            user=os.environ["COGNODB_USER"],
            password=os.environ["COGNODB_PASSWORD"],
            database=os.environ.get("COGNODB_DATABASE") or None,
        )
    if db_key == "neo4j_aura":
        from adapters.neo4j_aura import Neo4jAuraAdapter
        return Neo4jAuraAdapter(
            uri=os.environ["NEO4J_AURA_URI"],
            user=os.environ["NEO4J_AURA_USER"],
            password=os.environ["NEO4J_AURA_PASSWORD"],
            database=os.environ.get("NEO4J_AURA_DATABASE") or None,
        )
    if db_key == "memgraph":
        from adapters.memgraph import MemgraphAdapter
        return MemgraphAdapter(
            uri=os.environ.get("MEMGRAPH_URI", "bolt://localhost:7688"),
            user=os.environ.get("MEMGRAPH_USER", ""),
            password=os.environ.get("MEMGRAPH_PASSWORD", ""),
        )
    if db_key == "falkordb":
        from adapters.falkordb import FalkorDBAdapter
        return FalkorDBAdapter(
            host=os.environ.get("FALKORDB_HOST", "localhost"),
            port=_env_int("FALKORDB_PORT", 6379),
            password=os.environ.get("FALKORDB_PASSWORD", ""),
            graph_name=os.environ.get("FALKORDB_GRAPH_NAME", "pokec_bench"),
        )
    if db_key == "nebula":
        from adapters.nebula import NebulaAdapter
        return NebulaAdapter(
            host=os.environ.get("NEBULA_HOST", "localhost"),
            port=_env_int("NEBULA_PORT", 9669),
            user=os.environ.get("NEBULA_USER", "root"),
            password=os.environ.get("NEBULA_PASSWORD", "nebula"),
            space=os.environ.get("NEBULA_SPACE", "pokec_bench"),
            storage_host=os.environ.get("NEBULA_STORAGE_HOST", "nebula-storaged"),
            storage_port=_env_int("NEBULA_STORAGE_PORT", 9779),
        )
    raise ValueError(f"unknown db_key: {db_key}")


def select_start_nodes(dataset: Dataset, n: int, seed: int) -> list:
    """Fixed random set of start nodes, same seed across every DB — required so
    traversal latency differences are about the engine, not about which nodes
    happened to get queried."""
    rng = random.Random(seed)
    pids = [node["pid"] for node in dataset.nodes]
    return rng.sample(pids, min(n, len(pids)))


def timed_calls(fn, args_list) -> list:
    """Call fn(arg) once per element of args_list, return per-call latency in ms.
    Sequential/single-client by design — this is the read-latency suite, not the
    concurrency sweep (see run_concurrency_sweep)."""
    samples = []
    for arg in args_list:
        t0 = time.perf_counter()
        fn(arg)
        samples.append((time.perf_counter() - t0) * 1000.0)
    return samples


def run_read_suite_once(adapter, dataset, start_nodes, iterations, filter_values) -> dict:
    """One pass of every read workload family, >= `iterations` calls each. Returns
    {workload_name: stats_dict}. Start nodes / filter values are cycled if
    `iterations` exceeds the fixed sample size, so every DB still sees exactly
    the same access sequence regardless of iteration count."""
    out = {}

    def cycle(seq, n):
        return [seq[i % len(seq)] for i in range(n)]

    out["one_hop"] = summarize(timed_calls(adapter.one_hop, cycle(start_nodes, iterations)))
    out["two_hop"] = summarize(timed_calls(adapter.two_hop, cycle(start_nodes, iterations)))
    out["three_hop"] = summarize(timed_calls(adapter.three_hop, cycle(start_nodes, iterations)))
    out["point_lookup"] = summarize(timed_calls(adapter.point_lookup, cycle(start_nodes, iterations)))
    out["indexed_lookup"] = summarize(timed_calls(adapter.indexed_lookup, cycle(filter_values, iterations)))
    out["aggregation"] = summarize(timed_calls(lambda _: adapter.aggregation(), list(range(iterations))))
    return out


def run_variance_repeats(adapter, dataset, start_nodes, iterations, filter_values, n_runs) -> dict:
    """Repeat the full read suite N times (BENCH_VARIANCE_RUNS) and report the
    spread — the run-to-run variance distinctiveness item most candidates skip."""
    runs = []
    errors = []
    for i in range(n_runs):
        print(f"  [variance] run {i + 1}/{n_runs}")
        try:
            runs.append(run_read_suite_once(adapter, dataset, start_nodes, iterations, filter_values))
        except Exception as exc:
            # Keep the runs that completed. A managed instance dropping the connection
            # on run 3 of 5 shouldn't discard runs 1 and 2 — partial variance data with
            # a recorded n_runs is far more useful than an error string, and the reader
            # can see exactly how much of the intended sampling actually happened.
            print(f"  [variance] run {i + 1} FAILED, keeping {len(runs)} completed: {str(exc)[:120]}")
            errors.append({"run": i + 1, "error": str(exc)[:300]})
            break

    if not runs:
        raise RuntimeError(f"no variance runs completed: {errors}")

    variance = {}
    for workload in READ_WORKLOADS:
        p50s = [r[workload]["p50_ms"] for r in runs]
        p95s = [r[workload]["p95_ms"] for r in runs]
        variance[workload] = variance_across_runs(p50s, p95s)

    return {
        "runs": runs,
        "variance": variance,
        "n_runs_completed": len(runs),
        "n_runs_requested": n_runs,
        # Non-empty means the spread below is computed from fewer repeats than intended.
        "run_errors": errors,
    }


def run_amplified_suite(adapter, start_nodes, filter_values, n_ops=200, repeats=15) -> dict:
    """Per-operation engine cost, measured by amortising the round trip.

    This is the metric that actually survives a network-dominated environment. We time
    N operations executed inside ONE statement, subtract a single round trip, and divide
    by N — so RTT contributes RTT/N per operation (at N=200 and RTT=300 ms, 1.5 ms) and
    engine time dominates instead of being buried.

    Reported alongside a same-session RTT probe rather than a probe from 18 minutes ago,
    because the network floor drifts over a long run.
    """
    if not hasattr(adapter, "amplified_ops"):
        return {"supported": False, "reason": f"{adapter.name} adapter has no amplified_ops"}

    # Same-session RTT, sampled immediately before the amplified timings so drift
    # can't contaminate the subtraction the way it did for the single-shot suite.
    # Adapters with no RTT probe (loopback engines) get 0.0 — for those the round
    # trip is negligible and the batch time IS engine time.
    if hasattr(adapter, "measure_network_rtt"):
        rtt = summarize(adapter.measure_network_rtt(iterations=repeats)["rtt_ms_samples"])
    else:
        rtt = {"p50_ms": 0.0}

    out = {"supported": True, "n_ops_per_round_trip": n_ops, "repeats": repeats,
           "rtt_p50_ms": rtt["p50_ms"], "workloads": {}}

    for workload in READ_WORKLOADS:
        # Aggregation is a whole-graph scan; amplifying it 200x would take minutes for
        # no extra signal, so it starts at a lower multiplier.
        n = 5 if workload == "aggregation" else n_ops

        # Adaptive batch size. Amplification raises PEAK MEMORY because the engine
        # materialises intermediates for N start nodes simultaneously — on Memgraph
        # (capped at 200 MB to match CognoDB's free tier) a 200-wide 3-hop batch
        # exceeded the cap outright. Rather than hardcoding per-engine constants that
        # would silently rot, halve N and retry until it fits, then record the N
        # actually used so the reader can judge how well RTT was amortised.
        stats = None
        while n >= 1:
            try:
                adapter.amplified_ops(workload, start_nodes, filter_values, n)  # warm
                samples = []
                for _ in range(repeats):
                    t0 = time.perf_counter()
                    adapter.amplified_ops(workload, start_nodes, filter_values, n)
                    samples.append((time.perf_counter() - t0) * 1000.0)
                stats = summarize(samples)
                break
            except Exception as exc:
                last_err = str(exc)[:200]
                if n == 1:
                    out["workloads"][workload] = {"error": last_err, "n_ops": 1}
                    break
                n = max(1, n // 2)
                print(f"    [amplified] {workload}: retrying at n={n} after: {last_err[:80]}")

        if stats is None:
            continue

        per_op = max(stats["p50_ms"] - rtt["p50_ms"], 0.0) / n
        out["workloads"][workload] = {
            "n_ops": n,
            "n_ops_requested": 5 if workload == "aggregation" else n_ops,
            "batch_p50_ms": stats["p50_ms"],
            "batch_std_ms": stats["std_ms"],
            "per_op_engine_ms": per_op,
            # Share of batch time that was round trip. Small = engine-dominated =
            # trustworthy. Large means N had to be reduced and RTT still intrudes.
            "rtt_share_pct": (rtt["p50_ms"] / stats["p50_ms"] * 100.0) if stats["p50_ms"] else None,
        }
    return out


def run_concurrency_sweep(adapter, levels: list, rw_mix: float, duration_sec: float) -> list:
    """Mixed read/write throughput at each concurrency level — shows where each
    engine plateaus or collapses, per the concurrency-sweep distinctiveness item."""
    sweep = []
    for n_clients in levels:
        print(f"  [concurrency] {n_clients} clients, {duration_sec}s, rw_mix={rw_mix}")
        sweep.append(adapter.concurrent_workload(n_clients, rw_mix, duration_sec))
    return sweep


def run_load_only(db_key: str, dataset: Dataset, do_reset: bool = True) -> dict:
    """Load the dataset and verify the resulting graph counts, without running any
    workload. Exists so the load can be checkpointed and eyeballed before committing
    to a multi-hour measurement run — a wrong dataset discovered after the fact
    invalidates every number produced on top of it."""
    print(f"[{db_key}] connecting...")
    adapter = build_adapter(db_key)
    adapter.connect()
    result = {"db": adapter.name, "db_key": db_key}
    try:
        before = adapter.graph_counts()
        print(f"[{db_key}] counts before: {before}")
        result["counts_before"] = before

        if do_reset and (before["nodes"] or before["rels"]):
            print(f"[{db_key}] non-empty — resetting for an idempotent load...")
            adapter.reset()
            print(f"[{db_key}] counts after reset: {adapter.graph_counts()}")

        print(f"[{db_key}] loading {dataset.node_count} nodes / {dataset.edge_count} rels...")
        result["load"] = adapter.load(dataset)

        after = adapter.graph_counts()
        result["counts_after"] = after
        expected = {"nodes": dataset.node_count, "rels": dataset.edge_count}
        result["expected"] = expected
        result["counts_match"] = (after == expected)
        print(f"[{db_key}] counts after:  {after}")
        print(f"[{db_key}] expected:      {expected}")
        print(f"[{db_key}] MATCH: {result['counts_match']}")
    finally:
        adapter.close()
    return result


def run_full_benchmark(db_key: str, dataset: Dataset, do_load: bool = True) -> dict:
    seed = _env_int("BENCH_RANDOM_SEED", 42)
    iterations = _env_int("BENCH_READ_ITERATIONS", 100)
    variance_runs = _env_int("BENCH_VARIANCE_RUNS", 5)
    concurrency_levels = _env_list_int("BENCH_CONCURRENCY_LEVELS", [1, 10, 40])

    print(f"[{db_key}] connecting...")
    adapter = build_adapter(db_key)
    adapter.connect()

    result = {"db": adapter.name, "db_key": db_key, "seed": seed}

    try:
        if do_load:
            print(f"[{db_key}] loading {dataset.node_count} nodes / {dataset.edge_count} edges...")
            result["load"] = adapter.load(dataset)
        else:
            # Reuse ingest metrics captured by an earlier --load-only run rather than
            # dropping them: ingest throughput is a required metric, and re-loading
            # purely to re-measure it would mean wiping an already-verified graph.
            prior = RESULTS_DIR / f"{db_key}_load.json"
            if prior.exists():
                blob = json.loads(prior.read_text())
                result["load"] = dict(blob.get("load", {}))
                result["load"]["measured_by"] = f"--load-only run, reused from {prior.name}"
                result["load_verification"] = {
                    "counts_after": blob.get("counts_after"),
                    "expected": blob.get("expected"),
                    "counts_match": blob.get("counts_match"),
                }
                print(f"[{db_key}] reused ingest metrics from {prior.name}")
            else:
                result["load"] = {"skipped": True, "reason": "--no-load and no prior _load.json"}

        # Re-assert the graph matches the dataset before measuring it. Cheap, and it
        # catches a DB mutated or partially wiped between runs — which would otherwise
        # silently yield fast, wrong numbers that look publishable.
        counts = adapter.graph_counts()
        expected_counts = {"nodes": dataset.node_count, "rels": dataset.edge_count}
        result["counts_at_measure_time"] = counts
        if counts != expected_counts:
            raise RuntimeError(
                f"graph does not match dataset before measuring: {counts} != {expected_counts}. "
                "Refusing to benchmark a graph that differs from the other platforms."
            )
        print(f"[{db_key}] verified graph matches dataset: {counts}")

        start_nodes = select_start_nodes(dataset, n=50, seed=seed)
        # Filter values for indexed_lookup: ages actually present in the sample,
        # so every query hits real (not empty) results.
        present_ages = sorted({n["age"] for n in dataset.nodes if n["age"] is not None})
        filter_values = present_ages[:50] if present_ages else [25]

        print(f"[{db_key}] warming up...")
        adapter.warmup(start_nodes)

        # Network-subtracted "engine-only" latency: only meaningful for Bolt-based
        # managed/self-hosted DBs where we control the RTT-measuring call path.
        if hasattr(adapter, "measure_network_rtt"):
            print(f"[{db_key}] measuring network RTT (RETURN 1)...")
            result["network_rtt"] = summarize(adapter.measure_network_rtt(iterations=30)["rtt_ms_samples"])

        print(f"[{db_key}] running read suite ({variance_runs} repeats x {iterations} iterations)...")
        result["read_suite"] = run_variance_repeats(
            adapter, dataset, start_nodes, iterations, filter_values, variance_runs
        )

        # Each remaining phase is isolated: a failure in one must not discard the
        # results of those already completed. We learned this the expensive way —
        # a memory-limit error in the amplified suite threw away a fully-completed
        # 5x100-iteration read suite for Memgraph, and the JSON was written with
        # nothing but the error string.
        for phase_name, phase_fn in [
            ("amplified", lambda: run_amplified_suite(adapter, start_nodes, filter_values)),
            ("concurrency_sweep", lambda: run_concurrency_sweep(
                adapter, concurrency_levels, rw_mix=0.8, duration_sec=15.0)),
            ("footprint", adapter.footprint),
        ]:
            print(f"[{db_key}] running {phase_name}...")
            try:
                result[phase_name] = phase_fn()
            except Exception as exc:
                print(f"[{db_key}] {phase_name} FAILED (continuing): {str(exc)[:160]}")
                result[phase_name] = {"error": str(exc)[:500]}

    finally:
        adapter.close()

    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", required=True,
        choices=["cognodb", "neo4j_aura", "memgraph", "falkordb", "nebula", "all"],
    )
    parser.add_argument("--no-load", action="store_true", help="skip load step (dataset already present in the DB)")
    parser.add_argument("--load-only", action="store_true", help="load + verify counts, then stop (no workloads)")
    parser.add_argument("--no-reset", action="store_true", help="with --load-only: don't wipe an existing graph first")
    args = parser.parse_args()

    dataset = Dataset.from_csv()
    print(f"[dataset] {dataset.node_count} nodes, {dataset.edge_count} edges (seed={dataset.seed}, method={dataset.method})")

    db_keys = ["cognodb", "neo4j_aura", "memgraph", "falkordb", "nebula"] if args.db == "all" else [args.db]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    for db_key in db_keys:
        try:
            if args.load_only:
                result = run_load_only(db_key, dataset, do_reset=not args.no_reset)
            else:
                result = run_full_benchmark(db_key, dataset, do_load=not args.no_load)
        except Exception as exc:
            print(f"[{db_key}] FAILED: {exc}")
            result = {"db_key": db_key, "error": str(exc)}
        # Load-only output goes to a separate file so it can never overwrite a real
        # benchmark result with a workload-free stub.
        suffix = "_load" if args.load_only else ""
        out_path = RESULTS_DIR / f"{db_key}{suffix}.json"

        # NEVER clobber a good result with a pure-error stub. A re-run that dies
        # partway (defunct connection, throttling) would otherwise destroy a complete
        # earlier run — which is exactly what happened to Neo4j Aura: an 83-minute
        # hang ended in a connection error and replaced a full result set with a
        # 208-byte error blob. Failures go to <db>_error.json and the good file stands.
        if "error" in result and "read_suite" not in result and out_path.exists():
            prior = json.loads(out_path.read_text())
            if "read_suite" in prior:
                err_path = RESULTS_DIR / f"{db_key}{suffix}_error.json"
                err_path.write_text(json.dumps(result, indent=2, default=str))
                print(f"[{db_key}] run failed; PRESERVED existing good result at {out_path.name}, "
                      f"error written to {err_path.name}")
                continue
        out_path.write_text(json.dumps(result, indent=2, default=str))
        print(f"[{db_key}] wrote {out_path}")


if __name__ == "__main__":
    main()
