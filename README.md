# Graph Database Cloud Benchmark — CognoDB vs. the field

A reproducible benchmark comparing **CognoDB Cloud** against four other graph databases on
an identical dataset, identical logical queries, and matched resource limits.

One command reproduces everything: `./run.sh`

> **The honest headline first.** Four of five databases produced complete results. The fifth,
> NebulaGraph, produced none — and that is a finding, not a gap. At the 256 MB parity cap
> every other engine runs under, Nebula starts cleanly with zero OOM kills and then refuses
> to answer a read-only `SHOW HOSTS`, because its three daemons idle at 98.8% and 99.5% of
> the cap. Full evidence in [docs/nebula-appendix.md](docs/nebula-appendix.md). We chose to
> report that rather than quietly run Nebula at 512 MB × 3 and present the numbers next to
> engines capped at 256 MB × 1.

---

## Results

Full generated tables: [results/RESULTS.md](results/RESULTS.md) · raw JSON in [results/](results/)

### Per-operation engine time (ms) — the headline metric

| Database | point lookup | indexed lookup | one hop | two hop | three hop | aggregation | RTT p50 |
|---|---|---|---|---|---|---|---|
| CognoDB Cloud | <0.057* | <0.138* | 10.97 | 12.66 | 49.93 | <21.19* | 301.62 ms |
| Neo4j Aura Free | <0.000* | <0.000* | <0.024* | <0.080* | 0.72 | <5.75* | 132.57 ms |
| Memgraph | 0.0026 | 0.0155 | 0.0018 | 0.0262 | 1.49 | 2.80 | 0.43 ms |
| FalkorDB | 0.0108 | 0.0346 | 0.0118 | 0.0314 | 0.36 | 2.14 | loopback |

`<x*` = **unresolvable from this client**, not "immeasurably fast". See
[the measurement problem](#the-measurement-problem-why-raw-latency-is-useless-here).

### Ingest throughput — identical dataset, identical load path

| Database | Wall clock | Rels/sec | Deployment |
|---|---|---|---|
| Memgraph | **2.10 s** | **97,105** | local Docker, in-memory |
| FalkorDB | 7.23 s | 28,235 | local Docker, GraphBLAS |
| Neo4j Aura Free | 40.00 s | 5,103 | managed, ~133 ms away |
| CognoDB Cloud | 80.14 s | 2,547 | managed, ~302 ms away |

Every load was verified against the engine afterwards: all four report exactly
**16,000 nodes / 204,109 relationships**.

### Mixed workload — three completely different shapes

Sustained 80% read / 20% write, 15 s per level, at `cpus: 0.5`:

| Database | 1 client | 10 clients | 40 clients | Shape |
|---|---|---|---|---|
| CognoDB Cloud | 3 qps | 27 qps | 109 qps (0 err) | linear — network-bound, CPU never pressed |
| Neo4j Aura Free | 7 qps | 70 qps | 279 qps (0 err) | linear — same reason, 2.3× lower RTT |
| Memgraph | 1,938 qps | 3,899 qps | 3,724 qps (2 err) | **plateaus** — CPU-bound, degrades gracefully |
| FalkorDB | **2,269 qps** | 990 qps | 354 qps (13 err) | **collapses** — write contention |

This is the most interesting table in the benchmark, and the most misread-able.
CognoDB and Aura scale linearly to 40 clients **not because they parallelise well**, but
because each client spends its life waiting on a round trip — at 1 client, CognoDB's 3 qps
is simply 1 / 0.34 s. Concurrency is hiding latency, not exercising the engine. The two
loopback engines, pressed against the same 0.5 vCPU, show what saturation actually looks
like: Memgraph plateaus, FalkorDB collapses under write serialisation
([findings #4](docs/findings.md)).

### Run-to-run variance (one_hop p50 across 5 full suite repeats)

| Database | std | min–max band |
|---|---|---|
| CognoDB Cloud | 2.43 ms | 304.08 – 310.81 |
| Neo4j Aura Free | 3.52 ms | 116.64 – 127.44 |
| Memgraph | 0.05 ms | 0.43 – 0.57 |
| FalkorDB | 0.02 ms | 0.39 – 0.46 |

### Footprint

| Database | Observable? | |
|---|---|---|
| Memgraph | yes | `SHOW STORAGE INFO` — vertex/edge counts, memory |
| FalkorDB | yes | Redis `INFO memory` — 21.8 MB used, 102 MB RSS |
| CognoDB Cloud | **no** | free tier does not register `apoc.monitor.store` |
| Neo4j Aura Free | **no** | storage introspection not exposed to this role |

Stated as "not observable" rather than estimated.

---

## The measurement problem (why raw latency is useless here)

This is the part of the project worth reading.

The client runs on a laptop. CognoDB is in GCP `us-east4`. A trivial `RETURN 1` round trip
costs **302 ms**. Against that floor, the entire read suite compresses into noise:

| | raw p50 spread across 6 workloads |
|---|---|
| CognoDB | 305.2 → 340.6 ms — a **1.12×** spread |

Raw latency makes a point lookup and a 3-hop traversal look like the same operation.

**Attempt 1 — subtract the round trip.** Measure `RETURN 1`, subtract it from each query.
This is what the plan called for, and it does not work. RTT jitter (std 27–32 ms) exceeds
the engine times being measured (1–50 ms), and RTT drifts across an 18-minute run so a
probe taken at minute 0 does not describe minute 18. It produced **negative engine times** —
queries apparently finishing faster than an empty round trip — and, on CognoDB, claimed a
**point lookup was slower than a one-hop traversal** (14.77 ms vs 6.88 ms). That is
impossible: `one_hop` *is* a point lookup followed by a traversal.

**Attempt 2 — amplification.** Execute N operations inside a single statement, so one round
trip amortises to RTT/N. At N=200 against a 300 ms RTT that is 1.5 ms per operation, and
engine work dominates. Each amplified query preserves the per-start-node semantics of its
single-shot twin, so it is the same logical work batched — not a cheaper query that happens
to batch well.

The ordering it restores on CognoDB:

| workload | subtraction | amplified |
|---|---|---|
| point_lookup | 14.77 ms | **0.057 ms** |
| one_hop | 6.88 ms | **10.97 ms** |
| three_hop | 72.52 ms | **49.93 ms** |

**And amplification has its own floor, which we report rather than hide.** Its resolution is
~RTT/N. Neo4j Aura's engine is fast enough that 200 point lookups complete in *less* time
than the measured round trip — an RTT share above 100%. Those cells would print `0.0000 ms`,
which reads as "immeasurably fast" when it means "not measurable from here". They are marked
`<x*` instead. Every such cell is an argument for the same fix: **run the client in-region.**

---

## Fairness ledger

Full version: [docs/fairness-ledger.md](docs/fairness-ledger.md). The gaps that matter:

| Dimension | Status |
|---|---|
| Dataset | ✅ Identical. 16,000 / 204,109, seed 42, verified post-load on every engine. |
| Query language | ✅ 4/4 measured engines are Cypher and receive the **literal same query strings**. |
| Resource caps | ⚠️ Memgraph & FalkorDB at `cpus: 0.5 / mem_limit: 256m` to match CognoDB's free tier. Aura Free is not user-configurable. Nebula could not run at 256 MB at all. |
| Network | ❌ **The biggest gap.** Managed engines carry 133–302 ms RTT; local Docker carries ~0.4 ms. Raw latency is not comparable across that boundary — hence the amplified metric. |
| Region | ⚠️ CognoDB in GCP `us-east4`. Aura Free **auto-assigns** its region — not selectable at this tier, so a region-matched managed comparison is impossible for anyone on the free tier. |
| Index parity | ⚠️ Required per-engine work — see below. |
| Client placement | ❌ Laptop, not in-region. Known, quantified, and the top item of future work. |

### Index parity was not free

"Same logical query" is **not** sufficient for a fair benchmark. Equivalent *access paths*
are also required, and that took engine-specific work:

- **Memgraph:** a unique constraint is **not** index-backed (unlike Neo4j/CognoDB). Without
  an explicit `pid` index, every `MATCH (n:Person {pid: …})` was a full 16,000-node scan.
  Ingest ran **~285× slower** (144k edges in >600 s, unfinished → 204,109 edges in 2.1 s).
  Had this gone unnoticed, Memgraph would have looked catastrophically slow for reasons
  having nothing to do with its engine.
- **CognoDB:** the `pid` constraint *is* index-backed, but `SHOW INDEXES` does not list it.
  Verified by measurement instead — 500 seeks cost ~1.1 ms of engine time vs ~1,960 ms for
  equivalent scans.
- **FalkorDB:** no uniqueness constraint available; uniqueness is a property of the dataset,
  not enforced by the engine.

---

## Engine findings

Detail in [docs/findings.md](docs/findings.md).

1. **CognoDB returns wrong results for unlabeled repeated variables.**
   `MATCH (n)-[:FRIEND]->(n)` returns **204,109** — every relationship in the graph — where
   the correct answer is 0. Add a label and it is correct. Confirmed **CognoDB-specific**:
   Aura answers correctly on an identically-loaded graph. It fails silently and *inverts*
   the result. Found because a post-load sanity check counted self-loops and disagreed with
   the source CSV — a check built to validate data caught an engine bug instead.
2. **Memgraph constraints are not index-backed** (above).
3. **FalkorDB wedges under concurrent writers.** At 40 clients / 20% writes it stopped
   serving entirely for 11 minutes: 41 connections blocked, container at 0.76% CPU. Reported
   as errors rather than tuned away — lowering its concurrency to get a clean number would
   be tuning the benchmark until the engine looks good.
4. **NebulaGraph cannot meet the parity bar** ([appendix](docs/nebula-appendix.md)).

---

## Methodology

**Dataset.** SNAP soc-Pokec (1.6M nodes / 30.6M edges) sampled to **16,000 nodes /
204,109 relationships**, seed `42`, committed in [dataset/sample/](dataset/sample/).

Sampling is a **BFS-induced subgraph**, and the first implementation was wrong in a way
worth documenting: accumulating edges until an edge budget was hit left 96% of sampled nodes
on the outer BFS frontier with no out-edges. All three traversal workloads would have timed
*empty result sets* across five databases and produced clean, confident, meaningless
numbers. Collecting a node set first and then inducing all edges between those nodes fixed
it — average out-degree went from 1.85 to 12.76, and zero-result start nodes from 48/50 to
2/50. The remaining 2 are genuinely isolated users, left in rather than filtered out to make
results look tidier.

Sizing is set by the **most constrained platform**, not the assignment's suggested range:
Aura Free caps at 200k nodes / 400k relationships, and an identical dataset everywhere is
worth more than a larger one.

**Measurement.** Warm-up before timing; ≥100 iterations per read workload; 5 full-suite
repeats for variance; fixed seeds for sampling and start-node selection; percentiles not
averages. Same client, same dataset, same logical queries throughout.

**Architecture.** `GraphDBAdapter` abstract base; four Cypher engines share query text via
`CypherAdapter` so "same logical query" is true by construction; Nebula implements the
interface separately in nGQL.

```
adapters/   one per DB      harness/    runner + stats
dataset/    download+sample docker/     compose with caps
charts/     matplotlib      docs/       ledger, findings, appendix
results/    committed JSON  report.py   JSON → tables
```

---

## Reproducing

```bash
cp .env.example .env     # fill in credentials — never committed
./run.sh                 # dataset → docker → benchmark → charts → tables
./run.sh --db cognodb    # one database
```

Requires Python 3.12 and Docker. Pinned versions in `requirements.txt`.

**Note on Aura credentials:** some Aura instances authenticate with the **instance ID** as
both username and database name, not `neo4j`/`neo4j`. Copy both verbatim from the
credentials file — a wrong username and a wrong password return identical
`Unauthorized` errors, which cost us three debugging rounds.

---

## Known limitations

1. **Client is not in-region.** The single largest defect. Everything marked `<x*` becomes
   measurable from a `us-east4` VM. This is the top of the future-work list.
2. **NebulaGraph has no numbers.** Documented, reproducible, deliberate.
3. **FalkorDB's collapse was measured at `cpus: 0.5`.** A tight CPU cap is exactly what
   amplifies write contention. The claim is "FalkorDB wedged under 40 concurrent writers *at
   this resource cap*" — not a claim about FalkorDB at production sizing.
4. **Aura dropped a connection under sustained load** where CognoDB did not, costing a
   complete result set. One occurrence is an observation, not a pattern.
5. **Load path is driver batching everywhere.** Comparable across engines, but none of the
   self-hosted engines' native bulk loaders were used — those would be faster in production.
6. **Single region, single dataset size, single shape.** A 204k-relationship social graph on
   free tiers. Results should not be extrapolated to production sizing.
