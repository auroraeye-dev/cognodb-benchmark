"""results/*.json -> markdown tables, written to results/RESULTS.md.

Why a separate generated file rather than hand-editing README.md tables: the
numbers must come from the JSON, never be retyped by hand (that's how a
benchmark's reported numbers silently drift from its raw data). README.md
includes results/RESULTS.md's tables verbatim — regenerate this, then copy/
paste-or-include into README when writing up the analysis section.

Usage: python report.py
"""
import json
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"
OUT_PATH = RESULTS_DIR / "RESULTS.md"

DB_ORDER = ["cognodb", "neo4j_aura", "memgraph", "falkordb", "nebula"]
READ_WORKLOADS = ["one_hop", "two_hop", "three_hop", "point_lookup", "indexed_lookup", "aggregation"]


def load_results() -> dict:
    results = {}
    for db_key in DB_ORDER:
        path = RESULTS_DIR / f"{db_key}.json"
        if path.exists():
            results[db_key] = json.loads(path.read_text())
    return results


def fmt(x, digits=2):
    if x is None:
        return "n/a"
    return f"{x:.{digits}f}"


def build_ingest_table(results: dict) -> str:
    lines = [
        "| Database | Nodes loaded | Rels loaded | Wall clock (s) | Nodes/sec | Rels/sec | Load method |",
        "|---|---|---|---|---|---|---|",
    ]
    for k in DB_ORDER:
        r = results.get(k)
        if not r or "error" in r or not r.get("load", {}).get("rels_loaded"):
            # Keep the raw driver error out of the cell — it contains pipe characters
            # that break the table, and the full story belongs in the appendix anyway.
            note = "no complete run — see docs/nebula-appendix.md" if k == "nebula" else "no complete run"
            lines.append(f"| {k} | — | — | — | — | — | **{note}** |")
            continue
        load = r.get("load", {})
        lines.append(
            f"| {r['db']} | {load.get('nodes_loaded', '—')} | {load.get('rels_loaded', '—')} | "
            f"{fmt(load.get('wall_clock_sec'))} | {fmt(load.get('nodes_per_sec'), 0)} | "
            f"{fmt(load.get('rels_per_sec'), 0)} | {load.get('load_method', '—')} |"
        )
    return "\n".join(lines)


# Above this share of batch time spent on the round trip, the per-op figure is dominated
# by network noise rather than engine work and must not be presented as a measurement.
# 60% is a judgement call, chosen because it is comfortably below the point where our
# observed RTT jitter (std 27-32 ms on a ~300 ms RTT) can swallow the whole signal.
RTT_SHARE_UNRESOLVABLE_PCT = 60.0


def amplified_cell(r: dict, workload: str) -> str:
    """Format one amplified per-op figure, or say plainly that it is unresolvable.

    Amplification has a resolution floor of roughly RTT/N. When the engine operation is
    faster than that floor, subtracting the round trip yields ~0 (or negative, clamped
    to 0) and the result is an artifact, not a speed. Neo4j Aura hit exactly this: its
    point lookups came back with an RTT share above 100%, i.e. the batch finished faster
    than the separately-measured round trip. Printing "0.0000 ms" there would be the
    single most misleading number in this report - it reads as "immeasurably fast" when
    it actually means "we could not measure this from here"."""
    a = (r.get("amplified") or {}).get("workloads", {}).get(workload)
    if not a or "error" in (a or {}):
        return "n/a"
    share = a.get("rtt_share_pct")
    per_op = a.get("per_op_engine_ms")
    if share is not None and share >= RTT_SHARE_UNRESOLVABLE_PCT:
        # Report the floor as an upper bound instead of a false point estimate.
        return f"<{max(per_op, 0.0):.3f}*"
    return f"{per_op:.4f}"


def _rtt(r: dict) -> float:
    """Per-DB network round-trip floor, or 0.0 where we couldn't measure it.

    0.0 is the honest default rather than a guess: for engines where no RTT probe
    exists, engine-only and raw are the same number and the table says so."""
    return (r.get("network_rtt") or {}).get("p50_ms", 0.0)


def engine_only(r: dict, workload: str, pct: str = "p50") -> float:
    """Network-subtracted latency — the headline metric.

    Subtracting a trivial RETURN 1 round trip isolates engine time from client
    placement. Floored at 0: a negative would mean the query beat an empty round
    trip, which is measurement noise, not a faster-than-light engine."""
    v = r["read_suite"]["variance"][workload]
    return max(v[f"{pct}_mean_ms"] - _rtt(r), 0.0)


def build_latency_table(results: dict, workload: str) -> str:
    """Engine-only first (headline), raw retained and labelled as an artifact of
    where the client happens to be running."""
    lines = [
        "| Database | **engine-only p50** | **engine-only p95** | raw p50 | raw p95 | RTT p50 | p50 std | p50 min-max |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for k in DB_ORDER:
        r = results.get(k)
        if not r or "error" in r or "read_suite" not in r:
            continue
        v = r["read_suite"]["variance"][workload]
        rtt = _rtt(r)
        rtt_cell = fmt(rtt) if rtt else "n/a"
        lines.append(
            f"| {r['db']} | **{fmt(engine_only(r, workload, 'p50'))}** | "
            f"**{fmt(engine_only(r, workload, 'p95'))}** | "
            f"{fmt(v['p50_mean_ms'])} | {fmt(v['p95_mean_ms'])} | {rtt_cell} | "
            f"{fmt(v['p50_std_ms'])} | {fmt(v['p50_min_ms'])}-{fmt(v['p50_max_ms'])} |"
        )
    return "\n".join(lines)


def build_headline_matrix(results: dict) -> str:
    """The at-a-glance matrix: per-operation engine time from the amplified suite.

    Uses amplified rather than RTT-subtracted latency because subtraction is unsound at
    our jitter levels — it produced a point lookup "slower" than a one-hop traversal on
    CognoDB, which is impossible since one_hop contains a point lookup."""
    header = "| Database | " + " | ".join(w.replace("_", " ") for w in READ_WORKLOADS) + " | RTT p50 |"
    sep = "|---" * (len(READ_WORKLOADS) + 2) + "|"
    lines = [header, sep]
    for k in DB_ORDER:
        r = results.get(k)
        if not r or "error" in r or "read_suite" not in r:
            continue
        cells = [amplified_cell(r, w) for w in READ_WORKLOADS]
        rtt = _rtt(r)
        lines.append(f"| {r['db']} | " + " | ".join(cells) + f" | {fmt(rtt) if rtt else 'n/a'} |")
    lines.append("")
    lines.append(
        f"`<x*` = **unresolvable from this client**: the round trip was >= "
        f"{RTT_SHARE_UNRESOLVABLE_PCT:.0f}% of the batch, so the value is an upper bound, "
        "not a measurement. Amplification's resolution floor is ~RTT/N; operations faster "
        "than that floor cannot be timed from outside the database's region. This is the "
        "single strongest argument for re-running the client from an in-region VM."
    )
    return "\n".join(lines)


def build_engine_only_table(results: dict) -> str:
    lines = [
        "| Database | Network RTT p50 (ms) | Raw one_hop p50 (ms) | Engine-only one_hop p50 (ms) |",
        "|---|---|---|---|",
    ]
    for k in DB_ORDER:
        r = results.get(k)
        if not r or "network_rtt" not in r:
            continue
        rtt = r["network_rtt"]["p50_ms"]
        raw = r["read_suite"]["variance"]["one_hop"]["p50_mean_ms"]
        engine_only = max(raw - rtt, 0)
        lines.append(f"| {r['db']} | {fmt(rtt)} | {fmt(raw)} | {fmt(engine_only)} |")
    return "\n".join(lines)


def build_concurrency_table(results: dict) -> str:
    lines = ["| Database | Clients | Throughput (ops/sec) | Errors |", "|---|---|---|---|"]
    for k in DB_ORDER:
        r = results.get(k)
        if not r or "concurrency_sweep" not in r:
            continue
        for pt in r["concurrency_sweep"]:
            lines.append(f"| {r['db']} | {pt['n_clients']} | {fmt(pt['throughput_qps'])} | {pt['n_errors']} |")
    return "\n".join(lines)


def build_footprint_table(results: dict) -> str:
    lines = ["| Database | Observable | Detail |", "|---|---|---|"]
    for k in DB_ORDER:
        r = results.get(k)
        if not r or "footprint" not in r:
            continue
        fp = r["footprint"]
        detail = fp.get("raw") if fp.get("observable") else fp.get("reason", "not observable")
        lines.append(f"| {r['db']} | {fp.get('observable')} | {str(detail)[:200]} |")
    return "\n".join(lines)


def main():
    results = load_results()
    sections = [
        "# Results\n\n_Generated by `report.py` from `results/*.json`. Do not hand-edit._\n",
        "## Results matrix — per-operation engine time (ms), amplified\n\n"
        "**How to read this.** Raw latency to a managed instance measured from outside its\n"
        "region is ~99% network and says almost nothing about the engine. We therefore report\n"
        "per-operation engine time measured by *amplification*: N operations are executed\n"
        "inside a single statement, so one round trip amortises to RTT/N and engine work\n"
        "dominates.\n\n"
        "We do **not** use RTT-subtracted single-query latency as the headline, despite\n"
        "planning to. At our jitter (RTT std 27-32 ms against engine times of 1-50 ms) that\n"
        "subtraction is unsound: on CognoDB it reported a point lookup as *slower* than a\n"
        "one-hop traversal (14.77 ms vs 6.88 ms), which is impossible because one_hop\n"
        "performs a point lookup and then traverses. Amplification restores the correct\n"
        "ordering (0.057 ms vs 10.97 ms). The subtracted figures are retained in the\n"
        "per-workload tables below for transparency, but should not be trusted where the\n"
        "engine time is small relative to RTT jitter.\n\n"
        + build_headline_matrix(results),
    ]

    sections.append("## Ingest throughput\n\n" + build_ingest_table(results))

    sections.append(
        "## Per-workload detail\n\n"
        "> **Caveat on the `engine-only` columns below.** These are raw p50/p95 minus a\n"
        "> separately-measured `RETURN 1` round trip. For the two loopback engines\n"
        "> (Memgraph, FalkorDB) they are reliable. For the two managed engines they are\n"
        "> **not** — RTT jitter exceeds the engine time being measured, and RTT drifts over\n"
        "> a multi-minute run, so a probe taken at the start does not describe the middle.\n"
        "> Values clamp at 0.00 where the subtraction went negative. Use the amplified\n"
        "> matrix above for managed engines; these tables are included so the raw\n"
        "> measurements and their spread remain auditable."
    )
    for workload in READ_WORKLOADS:
        sections.append(f"### {workload.replace('_', ' ').title()} latency\n\n" + build_latency_table(results, workload))

    sections.append("## Network-subtracted engine-only latency\n\n" + build_engine_only_table(results))
    sections.append("## Concurrency sweep\n\n" + build_concurrency_table(results))
    sections.append("## Footprint\n\n" + build_footprint_table(results))

    OUT_PATH.write_text("\n\n".join(sections) + "\n")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
