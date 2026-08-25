# Appendix — NebulaGraph: why a distributed engine can't be held to this parity bar

**Status: no benchmark numbers.** NebulaGraph is in this benchmark for paradigm breadth —
a different query language (nGQL) and a different architecture (distributed, multi-process).
It did not produce a complete measured run, and rather than publish a partial or
specially-tuned result we report why, with evidence.

This is the most useful thing Nebula contributed: a **proven impossibility result** about
the benchmark's own fairness premise, which the four single-process engines could not have
revealed.

---

## The premise Nebula breaks

The fairness rule for this benchmark is that every self-hosted engine runs under the same
cap as CognoDB's free tier: `cpus: 0.5`, `mem_limit: 256m`. Memgraph and FalkorDB each run
a single process and fit comfortably:

| Engine | Processes | Idle RSS | Fits in 256 MB? |
|---|---|---|---|
| Memgraph | 1 | 143 MiB | yes (56%) |
| FalkorDB | 1 | 130 MiB | yes (51%) |
| NebulaGraph | **3** (metad + storaged + graphd) | **~1,164 MiB combined** | **no** |

Nebula's idle footprint for the same 16,000-node / 204,109-relationship graph is roughly
**8x Memgraph's entire footprint**. That is not a consequence of dataset size — the graph is
small — it is the floor cost of running a coordinating cluster.

---

## Evidence 1 — at 256 MB per service, Nebula starts and then refuses to work

Reproduce with `docker/docker-compose.nebula-256.yml`.

The intuitive prediction is "it won't start." That prediction is **wrong**, and this is the
part worth reading. All three containers start cleanly, report `status=running`, and are
never OOM-killed:

```
nebula-metad    restarts=0 oomkilled=false status=running
nebula-storaged restarts=0 oomkilled=false status=running
nebula-graphd   restarts=0 oomkilled=false status=running
```

But at rest they sit against the ceiling:

| Service | Idle usage | % of 256 MB cap |
|---|---|---|
| nebula-metad | 252.9 MiB | **98.8%** |
| nebula-storaged | 192.0 MiB | 75.0% |
| nebula-graphd | 254.8 MiB | **99.5%** |

Nebula's `system_memory_high_watermark_ratio` defaults to `0.8`. metad and graphd are past
it before a single query arrives, so the engine rejects everything. From graphd's own error
log:

```
E20260825 11:50:08.421094 QueryInstance.cpp:151]
  Used memory hits the high watermark(0.800000) of total system memory., query: SHOW HOSTS
```

It is refusing **`SHOW HOSTS`** — a read-only metadata query against an empty graph. Not the
204k-edge load, not a write: the most trivial statement available. Storage consequently
never reaches `ONLINE` and no load is possible.

**A distributed database can be perfectly healthy by every liveness signal a container
orchestrator exposes — running, not restarting, not OOM-killed — and still be unable to
answer a single query.** Any benchmark that treats "container is up" as "engine is ready"
would have recorded this as a working configuration.

## Evidence 2 — at 512 MB per service it runs, but did not survive a full clean run

We raised each service to 512 MB (already breaking parity, and disclosed as such) and did
get the dataset loaded and verified at 16,000 / 204,109 more than once. It was not stable:

- Under the amplified/ingest load, graphd climbed to **82%** of its 512 MB cap and the
  cluster wedged — `DROP SPACE` returned false and `SHOW HOSTS` timed out.
- `DROP SPACE` is asynchronous; recreating a same-named space too soon collides with
  storage still reclaiming partitions:
  `Storage Error: More than one request trying to add/update/delete one edge/vertex at the same time`.
- Recreating the cluster with wiped volumes left storaged unable to re-register with metad:
  ```
  E MetaClient.cpp:112] Heartbeat failed, status:Machine not existed!
  W FileBasedClusterIdMan.cpp:43] Open file failed, error No such file or directory
  ```
  Storage briefly reported `ONLINE`, then the first `INSERT VERTEX` batch failed with
  `Storage Error: RPC failure, probably timeout`.

**Honest attribution.** Evidence 2 is *not* a clean resource-parity result. It mixes three
different causes: genuine memory pressure at 512 MB, Nebula's asynchronous cluster
lifecycle, and our own orchestration of it (wiping the meta volume orphaned storaged's host
registration). We are not claiming "Nebula is unstable." We are claiming we could not
bootstrap and hold a stable single-node Nebula cluster within the time budgeted for one of
five databases — which is itself the relevant signal, but a different one from Evidence 1.

Evidence 1 is the load-bearing result: clean, reproducible, and unambiguous.

---

## Integration cost, stated plainly

Adapter fixes required, by engine:

| Engine | Fixes needed |
|---|---|
| CognoDB, Aura, FalkorDB | 0–1 |
| Memgraph | 1 (explicit `pid` index) |
| **NebulaGraph** | **7** |

Nebula's seven: `ADD HOSTS` cluster bootstrap; `USE space` on connect; missing-space
handling; missing-schema handling; polling instead of fixed sleeps for async DDL;
`REBUILD INDEX` retry because no observable readiness signal exists for it; and
`column_values()` instead of `rows()` because only the former returns typed accessors.

None were guesses — each came from reading a specific error. But the pattern is the
finding: **the one non-Cypher, multi-process engine cost several times the integration
effort of the four Cypher engines combined**, and still did not finish.

---

## What this means for the benchmark

1. **The parity bar is not neutral.** `cpus: 0.5 / mem_limit: 256m` looks like an even
   playing field and is in fact a filter that excludes distributed architectures
   outright. Free-tier-sized comparisons structurally favour single-process engines.
   That is a property of the *methodology*, not of the databases.
2. **The gap is unfixable, not merely unfixed.** No workload tuning closes a ~1.1 GB idle
   floor against a 256 MB cap.
3. **A fair Nebula comparison needs a different benchmark** — multi-node, larger data,
   resource levels where a cluster's fixed overhead amortises. Nebula is built for a scale
   this benchmark deliberately does not test.

Reporting Nebula numbers from a 512 MB × 3 configuration alongside engines capped at
256 MB × 1 would have been the dishonest option: superficially complete, quietly
comparing a 1.5 GB deployment against 256 MB ones. The absence of numbers here is the
accurate result.
