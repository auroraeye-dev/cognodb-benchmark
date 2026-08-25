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
        if not r or "error" in r:
            lines.append(f"| {k} | — | — | — | — | — | FAILED: {r.get('error') if r else 'no results'} |")
            continue
        load = r.get("load", {})
        lines.append(
            f"| {r['db']} | {load.get('nodes_loaded', '—')} | {load.get('rels_loaded', '—')} | "
            f"{fmt(load.get('wall_clock_sec'))} | {fmt(load.get('nodes_per_sec'), 0)} | "
            f"{fmt(load.get('rels_per_sec'), 0)} | {load.get('load_method', '—')} |"
        )
    return "\n".join(lines)


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
    """One table, every DB x every read workload, engine-only p50 — the at-a-glance
    results matrix. Everything else in this file is the supporting detail."""
    header = "| Database | " + " | ".join(w.replace("_", " ") for w in READ_WORKLOADS) + " | RTT p50 |"
    sep = "|---" * (len(READ_WORKLOADS) + 2) + "|"
    lines = [header, sep]
    for k in DB_ORDER:
        r = results.get(k)
        if not r or "error" in r or "read_suite" not in r:
            continue
        cells = [fmt(engine_only(r, w, "p50")) for w in READ_WORKLOADS]
        rtt = _rtt(r)
        lines.append(f"| {r['db']} | " + " | ".join(cells) + f" | {fmt(rtt) if rtt else 'n/a'} |")
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
        "## Results matrix — engine-only p50 latency (ms)\n\n"
        "Network round-trip subtracted per database. This is the headline: raw latency to a\n"
        "managed instance measured from outside its region is ~99% network and says almost\n"
        "nothing about the engine. Raw figures are retained in the per-workload tables below\n"
        "and are a disclosed artifact of client placement, not an engine property.\n\n"
        + build_headline_matrix(results),
    ]

    sections.append("## Ingest throughput\n\n" + build_ingest_table(results))

    for workload in READ_WORKLOADS:
        sections.append(f"## {workload.replace('_', ' ').title()} latency\n\n" + build_latency_table(results, workload))

    sections.append("## Network-subtracted engine-only latency\n\n" + build_engine_only_table(results))
    sections.append("## Concurrency sweep\n\n" + build_concurrency_table(results))
    sections.append("## Footprint\n\n" + build_footprint_table(results))

    OUT_PATH.write_text("\n\n".join(sections) + "\n")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
