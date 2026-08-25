# CLAUDE.md — Graph Database Cloud Benchmark

## What this project is
A take-home assignment for Wexa AI. Build a **reproducible, honest benchmark** comparing
CognoDB Cloud against four other managed/self-hosted graph databases on the **same dataset**
and the **same workloads**, under the **same resource limits**. The grade is about
engineering rigor and clear communication — NOT about which database "wins".

Deadline: 48 hours. Deliverable: a public GitHub repo. The author must be able to explain
and defend every line in a follow-up interview, so keep code readable and comment the
non-obvious reasoning.

## Grading weights (optimize against these)
- Methodology & fairness — 25%
- Completeness of metrics — 20%
- Reproducibility & code quality — 20%
- README & analysis — 15%
- Communication (the written article) — 20%

## The 5 databases
1. **CognoDB Cloud** — Bolt protocol + Cypher, managed. The subject. Free tier: burstable
   0.5 vCPU, 256 MB RAM, 1 GB disk.
2. **Neo4j Aura Free** — Bolt + Cypher, managed. The most direct apples-to-apples comparison.
3. **Memgraph** — Bolt + Cypher, self-hosted in Docker, in-memory engine.
4. **FalkorDB** — OpenCypher over Redis, self-hosted in Docker, sparse-matrix (GraphBLAS) engine.
5. **NebulaGraph** — nGQL (a DIFFERENT query language), self-hosted in Docker. Deliberately
   chosen for paradigm breadth. Its different query language is a disclosed caveat.

Four of five speak Cypher, so "same logical query" is airtight for those. Nebula's nGQL is
translated to the equivalent logical query and that difference is documented honestly.

## Fairness rules (this is 25% of the grade — do not cut corners)
- Cap every self-hosted DB (Memgraph, FalkorDB, Nebula) in docker-compose to match CognoDB's
  free tier as closely as the engine allows: `cpus: 0.5`, `mem_limit: 256m`. Document where an
  engine refuses to start that small and what you actually used.
- Managed DBs (CognoDB, Aura) carry real network latency that local Docker does not. This is
  the single biggest fairness gap. Handle it two ways: (a) run the client from a cloud VM in
  the same region as the managed instances to shrink RTT, and (b) measure a trivial `RETURN 1`
  round-trip per DB and report a **network-subtracted "engine-only" latency** alongside raw
  latency. That engine-only metric is a key differentiator — few candidates will have it.
- Same dataset, same logical queries, same client machine, same region, same warm-up for all.
- Maintain a **fairness ledger** table in the README: each DB's advertised specs, what you
  capped it to, in-memory vs on-disk, managed vs local, and every place parity is imperfect.

## Dataset
- **SNAP soc-Pokec**, sampled down to land in the 100k–500k relationship range (assignment's
  suggested sweet spot; keeps it inside the 256 MB / 1 GB free tier). Fix a random seed for the
  sample so it's reproducible. Record exact node count and relationship count in the README.
- Load the *identical* sampled dataset into every platform. Document the load method per DB
  (driver batching vs bulk import) and note that differing load paths are a disclosed caveat.

## Metrics to measure on EVERY platform (all required)
- **Ingest throughput**: nodes/sec, relationships/sec, total wall-clock load time.
- **Traversals**: 1-hop, 2-hop, 3-hop latency — p50 AND p95 (ms), from a fixed random set of
  start nodes (same seed across DBs).
- **Lookups**: point lookup + indexed/filtered lookup — p50/p95. State which properties are
  indexed on each platform.
- **Aggregations**: at least one count / group-by — p50/p95.
- **Mixed workload**: sustained queries/sec at a stated client concurrency and read/write mix.
- **Footprint**: stored data size, memory, instance specs where observable; say "not observable"
  explicitly where the platform hides it.

## Measurement rules
- Warm up before measuring. Report cold-start numbers SEPARATELY if included.
- >= 100 iterations per read workload after warm-up. Report percentiles, not just averages.
- Fixed random seeds everywhere (dataset sample, start-node selection) for reproducibility.
- Automate everything: one command loads data, runs workloads, emits results.

## What makes this submission DISTINCTIVE (build these in from the start)
1. **Run-to-run variance**: repeat the full read suite N times (e.g. 5 runs) and report the
   spread (std dev or min/max band), not just a single p50/p95. Most candidates skip this.
2. **Concurrency sweep**: run the mixed workload at 1 / 10 / 40 concurrent clients and plot
   throughput curves. Shows where each engine plateaus or collapses.
3. **Network-subtracted engine-only latency** (see fairness rules) — isolates engine speed from
   the managed-DB network tax.
4. **Fairness ledger** table — honesty as a visible feature.
5. **Root-cause analysis**, not just numbers: e.g. why FalkorDB's GraphBLAS wins deep traversals,
   why in-memory Memgraph beats on-disk on point lookups, why managed latency floors are higher.
6. **Charts** for every metric family (matplotlib), warm vs cold clearly separated.
7. **A genuinely engaging written article** (README + optionally a standalone post) with a real
   narrative arc: hypothesis → method → what surprised us → why. This is 20% of the grade.

## Architecture
Adapter pattern. One abstract base class `GraphDBAdapter` with methods:
`connect`, `load(dataset)`, `warmup`, `one_hop/two_hop/three_hop(start_nodes)`,
`point_lookup`, `indexed_lookup`, `aggregation`, `concurrent_workload(n_clients, rw_mix)`,
`footprint`, `close`. One concrete adapter per DB. Cypher adapters share a base; Nebula gets
its own nGQL implementation.

Suggested layout:
```
/adapters        # one file per DB, all implement GraphDBAdapter
/harness         # runner.py (warmup + timed iterations + percentiles + variance)
/dataset         # download + sample scripts (seeded)
/results         # emitted JSON (raw numbers, git-committed for reproducibility)
/charts          # chart generation from results JSON
/docker          # docker-compose.yml with cpu/mem caps for self-hosted DBs
report.py        # turns results JSON -> README tables + charts
.env.example     # documents required env vars (NO real secrets)
requirements.txt # pinned versions
run.sh           # the single-command entrypoint
README.md        # results matrix, methodology, fairness ledger, analysis, article
```

## Secrets — non-negotiable
NEVER commit CognoDB (or any) passwords or connection URIs. Read them from environment
variables only. Ship a `.env.example` with placeholder keys. Add `.env` to `.gitignore`.

## Stack
Python. Official `neo4j` driver for Bolt/Cypher DBs, FalkorDB's client (or redis + Cypher),
Nebula's `nebula3-python`. `numpy` for percentiles/variance, `matplotlib` for charts,
`python-dotenv` for env, Docker + docker-compose for self-hosted DBs. Pin every version.

## Build order (respect the 48h clock)
1. Sign up for CognoDB + Neo4j Aura, get both connecting with a trivial Cypher query.
2. Build the dataset download + seeded sampling script.
3. Build `GraphDBAdapter` base + the CognoDB adapter + the harness end-to-end for CognoDB ONLY.
   Get one full result JSON out before touching other DBs.
4. Add adapters one at a time: Neo4j Aura, Memgraph, FalkorDB, Nebula. Test each in isolation.
5. Run the full suite (with variance repeats + concurrency sweep). Commit results JSON.
6. Generate charts, write the README results matrix + fairness ledger.
7. Write the analysis + the article. Budget real time here — it's 20% of the grade.

## Interview-defense reminder
For each adapter and each metric, keep a one-line "why" comment. The author will be quizzed
on methodology choices, so favor clarity over cleverness.
