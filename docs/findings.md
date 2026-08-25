# Engine findings

Correctness and behaviour observations surfaced while building the benchmark. These are
not performance numbers — they're things that would change how you *read* the numbers, or
how you'd use the engine.

---

## 1. CognoDB: repeated node variable is not enforced when the pattern is unlabeled

**Severity:** correctness — returns wrong results silently, no error or warning.

In Cypher, reusing the same variable on both sides of a pattern constrains them to the
same node. `MATCH (n)-[:FRIEND]->(n)` therefore means "self-loops only". CognoDB honours
this when the node pattern carries a label, and ignores it when it does not.

Measured against the benchmark graph (16,000 nodes / 204,109 relationships), where the
source CSV independently verifies **zero** self-loops:

| Query | CognoDB | Neo4j Aura | Ground truth |
|---|---|---|---|
| `MATCH (n)-[:FRIEND]->(n) RETURN count(*)` | **204109** | 0 | 0 |
| `MATCH (n:Person)-[:FRIEND]->(n) RETURN count(*)` | 0 | 0 | 0 |
| `MATCH (x)-[:FRIEND]->(y) WHERE elementId(x)=elementId(y)` | 0 | 0 | 0 |
| `MATCH (x)-[:FRIEND]->(y) WHERE x.pid=y.pid` | 0 | 0 | 0 |

CognoDB returns **every relationship in the graph** — it appears to treat the second `(n)`
as an independent binding, so the pattern degenerates to an unconstrained `(a)-[:FRIEND]->(b)`.
Adding a label, or expressing identity through `WHERE`, produces the correct answer.

**Confirmed CognoDB-specific.** Neo4j Aura, running the identical query against an
identically-loaded graph, answers correctly in all four forms. This is not a shared Cypher
quirk or an artifact of our data.

**Why it matters:** the failure is silent and inverts the result — a query asking for a
rare subset returns the entire set. Any unlabeled self-referencing pattern (cycle
detection, reciprocity checks, "users who follow themselves") is affected. The workaround
is trivial once known — always label the node, or compare `elementId()` — but nothing in
the engine tells you that you need it.

**How this was found:** a post-load sanity check counted self-loops and reported 204,109
where the source data had none. The first hypothesis was a corrupt load; the graph turned
out to be correct and the *query* was wrong. Worth remembering as a methodology point —
the verification step caught an engine bug it wasn't designed to look for.

---

## 2. CognoDB: unique constraints are index-backed, but `SHOW INDEXES` doesn't list them

**Severity:** cosmetic / discoverability. No wrong results.

`SHOW INDEXES` reports only the explicitly-created `idx_Person_age`. The uniqueness
constraint on `Person.pid` appears solely in `SHOW CONSTRAINTS`, with no corresponding
index row, and the returned index rows have `entityType`, `labelsOrTypes` and `state`
all `NULL` — a narrower result shape than Neo4j's.

The constraint *is* index-backed. Amplifying 500 lookups into a single round trip to lift
engine time above the 302 ms network floor:

| 500 operations, one round trip | total | minus RTT |
|---|---|---|
| `pid` point lookup (constraint-backed) | 310.5 ms | **1.1 ms** |
| `age` filtered lookup (explicit RANGE index) | 504.5 ms | 195.1 ms |
| unindexed property scan | 2268.8 ms | 1959.5 ms |

500 `pid` seeks cost ~1 ms of engine time versus ~1960 ms for equivalent scans, so the
lookup is unambiguously an index seek. Only the introspection surface is incomplete.

**Why it matters:** you cannot audit CognoDB's index coverage from `SHOW INDEXES` alone.
For a benchmark this is a genuine trap — concluding "pid isn't indexed" from introspection
would have been wrong, and would have made every traversal number look inexplicable.

---

## 3. Memgraph: a unique constraint is NOT index-backed

**Severity:** benchmark-invalidating if unnoticed. Silent — no error, just slowness.

Neo4j, CognoDB and Memgraph all accept a uniqueness constraint on `Person.pid`. On
Neo4j/CognoDB that constraint is automatically backed by an index, so
`MATCH (n:Person {pid: $pid})` is an index seek (measured earlier: 500 seeks ≈ 1.1 ms of
engine time on CognoDB). **Memgraph enforces uniqueness without creating a
label-property index**, so the identical query degrades to a full label scan of every
`:Person` node.

Observed while loading, before the cause was understood:

| | CognoDB (constraint = indexed) | Memgraph (constraint only) |
|---|---|---|
| Relationship ingest | 204,109 edges in **80 s** | 144,000 edges in **>600 s**, still running |

Each `CREATE (a)-[:FRIEND]->(b)` does two `pid` lookups. Without the index that's two
16,000-node scans per edge — roughly 6.5 billion comparisons across the full load, on a
container capped at 0.5 vCPU.

`SHOW INDEX INFO` confirmed it directly: only `Person(age)` was indexed, while
`SHOW CONSTRAINT INFO` showed the `pid` uniqueness constraint present. Constraint and
index are genuinely separate concepts in Memgraph.

**Fix:** create the `pid` index explicitly (`CREATE INDEX ON :Person(pid)`). Done in
`adapters/memgraph.py`, with the load re-run from scratch so ingest throughput is
measured on equal footing rather than being penalised for a missing index the other
engines got for free.

**Why it matters for fairness:** had this gone unnoticed, every Memgraph traversal and
point-lookup number would have been scan-based while its competitors were seek-based —
and Memgraph would have looked catastrophically slow for reasons that have nothing to do
with its engine. "Same logical query" is not sufficient for a fair benchmark; **equivalent
access paths** are also required, and confirming that means inspecting each engine's
actual index state rather than assuming DDL means the same thing everywhere. Per-engine
indexing is recorded in [fairness-ledger.md](fairness-ledger.md).

---

## 4. FalkorDB: the mixed workload wedges under concurrent writers

**Severity:** availability. The engine stops serving and does not recover on its own.

Running the standard mixed workload (40 clients, 80% read / 20% write) against FalkorDB,
the sweep stopped making progress entirely and sat there for **11 minutes** until killed.

Server-side state at that point:

- 41 connections open, many carrying Redis `flags=b` (blocked), `idle=659` seconds
- every blocked client stuck on `cmd=graph.QUERY`
- container CPU **0.76%** — idle, not computing
- slowlog dominated by the write op `SET n.touch_count = coalesce(n.touch_count,0)+1` at
  ~93 ms per call

FalkorDB serialises writes against the whole graph. Under 40 concurrent clients with a
20% write mix, the write path stopped draining. Low CPU with long-blocked clients rules
out "slow but progressing" — this was a stall.

**This also exposed a harness bug of our own.** `concurrent_workload` bounds itself by
wall clock (`while time.monotonic() < stop_at`), but a thread blocked *inside* a driver
call never reaches that check. A deadline test only fires between operations, so the
sweep could hang indefinitely. Fixed by giving the FalkorDB client a `socket_timeout`
(30 s — far above the ~12 ms slowest healthy read, low enough to fail fast), so a wedged
query raises instead of hanging and the run completes.

**Reported, not hidden.** The right treatment is to keep the workload identical to every
other engine and let the timeouts surface as errors in the concurrency results. Lowering
FalkorDB's concurrency or dropping its write mix to get a clean number would be tuning
the benchmark until the engine looks good. An engine that stops serving under write
contention is a real property of that engine at these settings, and the error count is
the honest way to say so.

**Caveat on attribution:** this was measured at a `cpus: 0.5` / `mem_limit: 256m` cap, and
contention effects are exactly the kind of thing a tight CPU cap amplifies. The finding is
"FalkorDB wedged under 40 concurrent writers *at this resource cap*", not a claim about
FalkorDB at production sizing. Re-testing uncapped would be needed to separate the two.

---

## 5. Single-query latency to a managed instance is ~99% network from a laptop

**Severity:** methodology — invalidates raw latency as an engine metric.

`RETURN 1` round-trip to CognoDB from the local dev machine: **p50 302.3 ms**. Against
that floor, the entire read suite compresses into noise:

| | raw p50 spread | engine-only p50 spread |
|---|---|---|
| CognoDB, 6 workloads | 305.2 → 340.6 ms (1.12×) | 2.9 → 38.3 ms (**13.2×**) |

Raw p50 makes a point lookup and a 3-hop traversal look like the same operation. Subtract
the round trip and the expected structure appears immediately: point lookup cheapest,
hop cost rising with depth, 3-hop dominated by frontier expansion.

**Consequence:** for managed databases measured from outside their region, raw latency is
a measurement of the network path, not the engine. Engine-only latency is reported as the
headline throughout; raw is retained and disclosed as an artifact of client placement.

**Corollary — the concurrency curve is also distorted.** CognoDB scaled 2.92 → 28.75 →
112.61 qps across 1/10/40 clients with zero errors and no plateau. That is not evidence of
engine parallelism: at 1 client, 2.92 qps is simply 1/0.34 s, the client idling on a round
trip. Concurrency is hiding latency rather than exercising the 0.5 vCPU. Expect this curve
to change shape entirely when re-run from an in-region VM, and expect self-hosted engines
on loopback to plateau where CognoDB did not.

---

## 4. Observability differs sharply across platforms

`apoc.monitor.store` is not registered on CognoDB's free tier, so stored-size footprint is
**not observable** there. Recorded as an explicit gap rather than estimated — see the
footprint row in the results matrix. Per-platform footprint observability is tracked in
[fairness-ledger.md](fairness-ledger.md).
