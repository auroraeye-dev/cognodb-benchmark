# Wrong benchmarks don't look wrong. They look clean.

I set out to benchmark five graph databases against CognoDB Cloud. I finished with results
for four, a proof that the fifth couldn't be included fairly, and a much less comfortable
conclusion than I expected:

**Four separate times, my benchmark was measuring nothing, and it never once looked broken.**

No crashes. No error messages. No suspicious outliers. Just tidy tables of plausible
numbers, arranged in a sensible order, ready to publish. Every one of them was wrong for a
different reason, and I only caught them because I went looking for trouble that the output
gave me no reason to suspect.

This is the story of those four failures, because they turned out to be more interesting
than the benchmark.

---

## The setup

Same dataset, same logical queries, same resource limits. SNAP's soc-Pokec social graph
sampled down to **16,000 nodes and 204,109 relationships** with a fixed seed, loaded
identically into CognoDB Cloud, Neo4j Aura Free, Memgraph, FalkorDB, and NebulaGraph.
Traversals at one, two, and three hops. Point and indexed lookups. Aggregation. A mixed
read/write workload at 1, 10, and 40 clients. Five full repeats for variance.

Four of the five speak Cypher, so "the same logical query" could be literally the same
string. That felt airtight.

It wasn't the queries that broke.

---

## Failure 1: I benchmarked an empty graph for a week's worth of design

My sampler walked breadth-first from a random seed node, collecting edges until it hit a
budget of 200,000. Reasonable. It produced exactly the node and relationship counts I
wanted. The CSVs looked fine.

Then I ran a sanity check I almost skipped, because the counts already matched:

```
start nodes with ZERO out-edges: 48 / 50
average out-degree: 1.85    (real Pokec: ~18.75)
```

Breadth-first expansion means most nodes you *discover* sit on the outer frontier. They got
reached, so they're in the node set — but the search stopped before expanding them, so none
of their own out-edges made it into the sample. Sampling start nodes uniformly from that set
draws a dead end 96% of the time.

All three traversal workloads would have been timing **empty result sets**. Across five
databases. And the results would have looked *wonderful* — fast, consistent, low variance,
tight percentiles. A one-hop traversal that returns nothing is extremely quick and extremely
reproducible.

The fix was to collect a node set first, then keep every edge with both endpoints inside it —
an induced subgraph, where interior nodes keep their real neighbourhoods:

| | before | after |
|---|---|---|
| average out-degree | 1.85 | **12.76** |
| start nodes with no out-edges | 48/50 | **2/50** |
| 3-hop frontier (median) | ~0 | **1,687 nodes** |

The two remaining dead ends are real isolated users. I left them in. Filtering them would
have made the numbers prettier and the graph less true.

---

## Failure 2: the metric I was proudest of was mathematically impossible

The client ran on a laptop. CognoDB runs in Virginia. A trivial `RETURN 1` round trip cost
**302 milliseconds**.

At that floor, the whole read suite collapses into noise. Point lookups and three-hop
traversals — operations differing by orders of magnitude in real work — landed between
305 ms and 341 ms. A 1.12× spread across six workloads. Raw latency wasn't measuring
databases. It was measuring my Wi-Fi.

The planned fix was obvious: measure the round trip, subtract it, report "engine-only"
latency. I built it, ran it, and got this on CognoDB:

| workload | "engine-only" |
|---|---|
| one_hop | 6.88 ms |
| **point_lookup** | **14.77 ms** |

A point lookup, apparently slower than a one-hop traversal. But `one_hop` *is* a point
lookup followed by a traversal — it does strictly more work by construction. The number
wasn't merely wrong, it was impossible. On Aura the same method produced **negative engine
times**: queries finishing faster than an empty round trip.

The cause is that subtraction assumes the thing you're subtracting is stable. Mine wasn't.
RTT jitter had a standard deviation of 27–32 ms while the engine times I was chasing were
1–50 ms — the error bar was larger than the signal. And RTT drifts, so a probe taken at
minute zero doesn't describe minute eighteen.

What works is to stop subtracting and start **amortising**: run 200 operations inside a
single statement, so one round trip spreads across all of them and becomes 1.5 ms per
operation instead of 302.

| workload | subtraction | amplified |
|---|---|---|
| point_lookup | 14.77 ms | **0.057 ms** |
| one_hop | 6.88 ms | **10.97 ms** |
| three_hop | 72.52 ms | **49.93 ms** |

The ordering is coherent again. A point lookup is ~200× cheaper than a one-hop traversal,
which is what you'd expect.

**And this method has its own floor, which I report rather than hide.** Its resolution is
roughly RTT/N. Aura's engine is fast enough that 200 point lookups finish in *less* time
than a single measured round trip. Those cells would print `0.0000 ms`, which any reader
would take as "immeasurably fast" when it actually means "not measurable from here." They're
marked as unresolvable upper bounds instead. There are seven such cells in my results matrix,
and every one of them is an argument for the same fix: run the client in the same region.

---

## Failure 3: the same query, doing completely different work

Four databases, one Cypher string, identical semantics. That was supposed to be the part I
didn't have to worry about.

Then Memgraph's load started crawling. After ten minutes it had managed 144,000 of 204,109
relationships. CognoDB had done the whole thing in eighty seconds.

Memgraph accepted my uniqueness constraint on `pid` without complaint. What it did **not**
do — unlike Neo4j and CognoDB — was build an index to back it. Constraint and index are
separate concepts there. So `MATCH (n:Person {pid: $pid})` silently degraded from an index
seek to a full scan of all 16,000 nodes. Two per edge created. Roughly 6.5 billion
comparisons, on a container capped at half a CPU.

One line of DDL:

| | before | after |
|---|---|---|
| ingest | 144k edges in >600 s, unfinished | 204,109 edges in **2.1 s** |

About a 285× difference, and not one byte of the query text changed.

Had I not noticed, Memgraph would have posted catastrophic numbers across every traversal
and lookup workload, and the write-up would have concluded something about its storage
engine. It would have been completely wrong. The engine was fine. My setup was scanning.

**"Same logical query" is not sufficient for a fair benchmark. Equivalent *access paths* are
also required** — and verifying that means inspecting each engine's actual index state,
because identical DDL does not imply identical behaviour.

The same trap nearly caught me in reverse on CognoDB: its `pid` constraint *is* index-backed,
but `SHOW INDEXES` doesn't list it. Introspection said unindexed; measurement said 500 seeks
cost 1.1 ms against 1,960 ms of equivalent scanning. Believing the introspection would have
sent me chasing a phantom.

---

## Failure 4: the fairness rule was itself unfair

Every self-hosted engine got `cpus: 0.5` and `mem_limit: 256m`, matching CognoDB's free
tier. Identical constraints for everyone. That is what fairness looks like.

NebulaGraph would not run in it. My assumption was that it would refuse to start, and I
nearly wrote that sentence without checking. So I checked.

It starts perfectly. All three daemons come up, report `running`, and are never OOM-killed.
And then it declines to answer this:

```
E QueryInstance.cpp:151] Used memory hits the high watermark(0.800000)
  of total system memory., query: SHOW HOSTS
```

That's `SHOW HOSTS` — a read-only metadata query against an *empty* graph. Not my 204k-edge
load. Nebula's metad and graphd idle at 98.8% and 99.5% of a 256 MB cap, past the 0.8
memory watermark before a single query arrives.

**A distributed database can be healthy by every signal a container orchestrator exposes —
running, not restarting, not OOM-killed — and still be unable to answer anything at all.**

Nebula needs three coordinating daemons whose combined idle footprint is about **1,164 MB**.
Memgraph does the same graph in **143 MB**, single process. That's roughly 8×, and it's the
floor cost of a cluster, not a function of my dataset being large — my dataset is tiny.

Which means the parity rule I'd chosen to guarantee fairness was quietly a filter. A
free-tier-sized cap doesn't just constrain distributed architectures, it excludes them. That
isn't a fact about NebulaGraph. It's a fact about my methodology, and I could only see it by
running the engine my methodology couldn't accommodate.

I could have run Nebula at 512 MB × 3 and put its numbers in the table next to engines
capped at 256 MB × 1. It would have looked complete. It would have been a 1.5 GB deployment
quietly compared against 256 MB ones. The missing row is the more accurate result.

---

## What actually got measured

With the methodology finally trustworthy, four databases, per-operation engine time:

| Database | point lookup | one hop | three hop | ingest |
|---|---|---|---|---|
| Memgraph | 0.0026 ms | 0.0018 ms | 1.49 ms | **97,105 rels/s** |
| FalkorDB | 0.0108 ms | 0.0118 ms | **0.36 ms** | 28,235 rels/s |
| Neo4j Aura Free | below floor | below floor | 0.72 ms | 5,103 rels/s |
| CognoDB Cloud | below floor | 10.97 ms | 49.93 ms | 2,547 rels/s |

FalkorDB wins deep traversal — its GraphBLAS sparse-matrix engine turns a 3-hop into matrix
multiplication, and it beats in-memory Memgraph by 4× there while losing to it everywhere
shallower. Memgraph's in-memory storage dominates ingest and cheap lookups.

But the most interesting table is concurrency, because it shows three completely different
failure shapes under the *same* half-CPU cap:

| Database | 1 client | 10 | 40 |
|---|---|---|---|
| CognoDB Cloud | 3 qps | 27 | 109 |
| Neo4j Aura Free | 7 qps | 70 | 279 |
| Memgraph | 1,938 qps | 3,899 | 3,724 |
| FalkorDB | **2,269 qps** | 990 | **354** |

The managed databases scale beautifully and linearly — and it means nothing. At one client,
CognoDB's 3 qps is just 1 ÷ 0.34 s: the client sits idle waiting for Virginia. Concurrency
isn't exercising the engine, it's hiding latency. The CPU is never pressed.

The loopback engines, with nowhere to hide, show what saturation actually looks like.
Memgraph plateaus and holds. FalkorDB is the fastest engine in the fleet at one client and
then **collapses to 15% of its peak** by forty, because it serialises writes against the
whole graph. During one run it stopped serving entirely: 41 connections blocked for eleven
minutes at 0.76% CPU.

I kept FalkorDB's workload identical to everyone else's and reported the timeouts as errors.
Lowering its concurrency to get a clean number would have been tuning the benchmark until
the engine looked good.

Oh — and CognoDB has a genuine Cypher bug. `MATCH (n)-[:FRIEND]->(n)` returns 204,109: every
relationship in the graph, where the correct answer is zero. Add a label and it's correct.
Aura gets it right on identical data. I found it because a post-load check counted self-loops
and disagreed with the source CSV. A check written to validate *data* caught an *engine*
defect it was never designed to look for.

---

## What I'd tell someone starting one of these

**The output of a broken benchmark is indistinguishable from the output of a working one.**
Four times, my numbers were clean, ordered, low-variance, and meaningless. Not one of them
announced itself. Every single one was caught by a check whose only purpose was to verify
something I already believed was true.

So: assert the boring things. Count the rows after loading. Check that your traversals
actually traverse. Confirm the indexes exist rather than that the DDL succeeded. Ask whether
your derived metric can produce a value that's physically impossible — and if it can, find
out whether it just did.

And when a result contradicts a rule you wrote yourself, take the result seriously. The most
useful thing in this entire project came from an engine that produced no numbers at all.
