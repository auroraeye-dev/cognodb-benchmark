# Fairness ledger (working notes)

Running log of every place parity between the five databases is imperfect. This is
the source material for the README's fairness ledger table. Written down as it's
discovered rather than reconstructed at the end — reconstructed honesty is the kind
that quietly loses entries.

## Region / network placement

| Database | Deployment | Provider + region | Notes |
|---|---|---|---|
| CognoDB Cloud | Managed | GCP `us-east4` (N. Virginia) | The subject of the benchmark. |
| Neo4j Aura Free | Managed | GCP, US region — **auto-assigned, not user-selectable** | Free tier gives no region choice at all. |
| Memgraph | Self-hosted Docker | Local dev machine | No network tax at all — loopback only. |
| FalkorDB | Self-hosted Docker | Local dev machine | Same. |
| NebulaGraph | Self-hosted Docker | Local dev machine | Same. |

**Disclosed gap — region parity is imperfect and only partly within our control.**
CognoDB runs on GCP `us-east4` (N. Virginia). Neo4j Aura's **Free tier does not let you
pick a region at all** — it auto-assigns one, and this instance landed on GCP in the US.
Both managed instances are therefore on GCP in the US, but not in the same region and
not in the same datacenter, and we could not have forced them to be: the constraint is
Aura's, not a choice we made.

Worth stating plainly for the write-up: this means the CognoDB↔Aura comparison cannot
be made region-identical by anyone, on any budget, using Aura's free tier. Anyone
claiming a clean region-matched managed-vs-managed comparison at this tier is either
paying for a higher Aura tier or not being careful.

Residual round-trip difference between them is real and is NOT hand-waved away — it is
controlled for by the network-subtracted **engine-only latency** metric (measure a
trivial `RETURN 1` RTT per DB, subtract it from measured query latency), which is
reported alongside raw latency throughout.

The three self-hosted engines have effectively zero network tax (loopback), which is a
much larger gap than the CognoDB↔Aura one. This is exactly why raw latency is never
compared across the managed/self-hosted boundary without the engine-only figure beside it.

## Dataset sizing

The dataset is sized to the **smallest ceiling in the fleet**, not to the assignment's
suggested 100k-500k band. Neo4j Aura Free hard-caps at **200k nodes / 400k relationships**,
which is tighter than the assignment band's upper end. Since the benchmark's core claim
is "the identical dataset on every platform", the most-constrained platform sets the size
for everyone — otherwise Aura would silently truncate and every cross-DB number would be
comparing different graphs.

Target: 200k relationships (mid-range of a 150k-250k window), yielding ~90k nodes at this
graph's snowball density. That leaves >2x headroom under Aura's node cap, deliberately, so
that per-engine differences in what counts against a quota can't push any platform over.

## Resource caps

| Database | Cap applied | Notes |
|---|---|---|
| CognoDB Cloud | Free tier: burstable 0.5 vCPU, 256 MB RAM, 1 GB disk | The baseline every self-hosted cap is matched to. |
| Neo4j Aura Free | Aura Free tier defaults | Not user-configurable; whatever Neo4j provisions. |
| Memgraph | `cpus: 0.5`, `mem_limit: 256m`, `--memory-limit=200` | 200 MB engine limit leaves headroom under the 256 MB container cap. |
| FalkorDB | `cpus: 0.5`, `mem_limit: 256m` | |
| NebulaGraph | `cpus: 0.5`, `mem_limit: 512m` **per service, 3 services** | **Exception — see below.** |

**Disclosed gap — NebulaGraph cannot run at the 256 MB cap. Measured, not assumed.**

Nebula is a multi-process architecture (metad + storaged + graphd as separate daemons).
We tested the 256 MB cap directly rather than asserting it was impossible —
`docker/docker-compose.nebula-256.yml` reproduces the experiment.

| Config | Result |
|---|---|
| 256 MB × 3 services | Containers **start cleanly, zero OOM kills**, status `running`. But metad idles at **98.8%** and graphd at **99.5%** of the cap, far past Nebula's `system_memory_high_watermark_ratio` (0.8). Every query is rejected and storage never reaches ONLINE. **Cannot load.** |
| ~256 MB total across all 3 | Not attemptable. storaged alone needs ~192 MB at rest; metad and graphd ~253 MB each. |
| 512 MB × 3 (what we used) | Works. Idle 54–64% per service. |

The failure mode matters: Nebula does **not** refuse to start, which is what we would have
written had we reasoned instead of measured. It starts and then refuses to *work*. Only
graphd's error log shows why — it rejects even a read-only `SHOW HOSTS` on an empty graph:

```
E QueryInstance.cpp:151] Used memory hits the high watermark(0.800000)
  of total system memory., query: SHOW HOSTS
```

**Why the gap is unfixable rather than merely unfixed.** Memgraph (143 MB) and FalkorDB
(130 MB) each run a single process comfortably inside 256 MB. Nebula needs three
coordinating daemons whose combined *idle* footprint is ~1,164 MB — about **8× Memgraph's
entire footprint** for the same 204,109-edge graph. That floor is a property of the
distributed architecture, not of this dataset's size, so no workload tuning closes it.

Consequence for reading the results: Nebula has materially more memory than any other
engine here, and any result where it wins on a memory-sensitive workload must be read with
that in mind. The comparison is caveated in both directions — Nebula has the most total
memory in the fleet *and* the least headroom per service (graphd hit 82% and the cluster
wedged under load during one run at 512 MB).

## Query language

Four of five (CognoDB, Aura, Memgraph, FalkorDB) speak Cypher and receive the
**literal same query strings** — see `adapters/cypher_base.py`. NebulaGraph speaks nGQL
and its queries are hand-translated equivalents (`adapters/nebula.py`), written to be
faithful to the Cypher original rather than optimised for Nebula. This is the single
language-paradigm gap in the benchmark and is disclosed wherever Nebula appears.

## Indexing

Equivalent *access paths* matter as much as equivalent queries. Sending every engine the
same Cypher is not enough if one of them answers it with a scan while the others seek —
that difference alone was worth ~285x on Memgraph's ingest (see findings #3).

| Database | `pid` (point lookup / traversal start) | `age` (indexed lookup) | Notes |
|---|---|---|---|
| CognoDB Cloud | UNIQUE constraint, **implicitly index-backed** | explicit RANGE index | Constraint's backing index is invisible to `SHOW INDEXES` — verified index-backed by measurement instead (findings #2). |
| Neo4j Aura Free | UNIQUE constraint, implicitly index-backed | explicit RANGE index | Standard Neo4j behaviour. |
| Memgraph | UNIQUE constraint **+ explicit `CREATE INDEX ON :Person(pid)`** | explicit label-property index | The explicit pid index is REQUIRED: Memgraph constraints are not index-backed (findings #3). |
| FalkorDB | explicit `CREATE INDEX ... ON (n.pid)` | explicit index | No uniqueness constraint available; uniqueness is guaranteed by the dataset (each SNAP id appears once), not enforced by the engine. |
| NebulaGraph | **native vertex ID** — no index concept | `person_age_idx` tag index | The VID *is* the key, so FETCH is O(1) by construction rather than by planner choice. Additionally needs `person_all_idx` / `friend_idx` purely to make `LOOKUP` scannable at all. |

**Disclosed asymmetries:**
- Only FalkorDB lacks engine-enforced `pid` uniqueness. Harmless here (the sample
  guarantees it) but it is not the same guarantee the others provide.
- Nebula's point lookup is a native ID fetch, structurally different from the other four's
  index seek. It is the closest faithful equivalent, not the same mechanism.
- Nebula required two extra indexes that exist only so the verification step can count
  rows — they have no analogue on the other engines and do add index-maintenance cost to
  its ingest number.

## Load path

All five currently load via driver batching rather than native bulk-import tools, which
keeps ingest numbers comparable. CognoDB and Aura free tiers give no filesystem access
for a bulk loader; the self-hosted three do have native loaders but are deliberately not
using them, so the comparison stays like-for-like. Batch size 1000 for the Cypher DBs,
500 for Nebula (nGQL INSERT statements are more verbose per row).
