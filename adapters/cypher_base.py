"""Shared base for every DB that speaks Cypher (CognoDB, Neo4j Aura, Memgraph, FalkorDB).

Why this exists: four of the five databases in this benchmark speak Cypher, so we can
send them the *literal same query string* — that's what makes "same logical query"
airtight for those four instead of merely "queries that are supposed to be equivalent".
Nebula's nGQL adapter (adapters/nebula.py) does NOT inherit from this; it hand-translates
each of these same queries into nGQL, and that translation is a disclosed caveat in the
README, not something we can paper over here.

Concrete subclasses only need to implement `_run_query`, `connect`, `close`, `load`,
and `footprint` — everything query-shape-related lives here exactly once.

Data model assumed (see dataset/sample.py): nodes are labeled `:Person` with an
integer `pid` property (the original SNAP Pokec node id) and an `age` property
(from the Pokec profile data); edges are `:FRIEND` relationships, directed exactly
as they appear in soc-pokec-relationships.txt (Pokec "friend" edges are directional
declarations, not symmetric, so we preserve that rather than silently treating the
graph as undirected).
"""
import random
import threading
import time
from abc import abstractmethod

from adapters.base import GraphDBAdapter


class CypherAdapter(GraphDBAdapter):
    # Traversal queries count distinct reachable nodes N hops out. COUNT rather than
    # returning full node payloads so we're timing traversal cost, not payload
    # serialization size, and so the query shape is identical across DBs regardless
    # of how "wide" any individual start node happens to be.
    Q_ONE_HOP = (
        "MATCH (n:Person {pid: $pid})-[:FRIEND]->(m) "
        "RETURN count(m) AS c"
    )
    Q_TWO_HOP = (
        "MATCH (n:Person {pid: $pid})-[:FRIEND]->()-[:FRIEND]->(m) "
        "RETURN count(DISTINCT m) AS c"
    )
    Q_THREE_HOP = (
        "MATCH (n:Person {pid: $pid})-[:FRIEND]->()-[:FRIEND]->()-[:FRIEND]->(m) "
        "RETURN count(DISTINCT m) AS c"
    )
    # Point lookup: single node by the primary indexed key.
    Q_POINT_LOOKUP = "MATCH (n:Person {pid: $pid}) RETURN n.pid AS pid"
    # Indexed/filtered lookup: range scan on a secondary indexed property (age).
    Q_INDEXED_LOOKUP = (
        "MATCH (n:Person) WHERE n.age = $age RETURN n.pid AS pid LIMIT 100"
    )
    # Aggregation: group-by count over the full loaded dataset.
    Q_AGGREGATION = (
        "MATCH (n:Person) RETURN n.age AS age, count(*) AS c ORDER BY c DESC"
    )
    # Mixed-workload write op: bump a counter property on one node. Cheap and
    # idempotent-ish (monotonic per node) so it doesn't grow the dataset during
    # a sustained-throughput run.
    Q_WRITE = (
        "MATCH (n:Person {pid: $pid}) SET n.touch_count = coalesce(n.touch_count, 0) + 1"
    )

    # Post-load verification. Counts the same labels/types the load actually writes,
    # so a partial load shows up as a count mismatch rather than a fast engine.
    Q_COUNT_NODES = "MATCH (n:Person) RETURN count(n) AS c"
    Q_COUNT_RELS = "MATCH ()-[r:FRIEND]->() RETURN count(r) AS c"

    @abstractmethod
    def _run_query(self, cypher: str, params: dict) -> list:
        """Execute one Cypher statement, return a list of result rows (dict-like).
        Each subclass wraps its own driver's session/query API here."""
        raise NotImplementedError

    # --- Amplified (batched) variants: N operations inside ONE round trip -------------
    #
    # Why these exist. Subtracting a separately-measured RETURN 1 from a single query's
    # latency fails when RTT jitter exceeds engine time — which is exactly our situation
    # (RTT std 27-32 ms vs engine times of 1-40 ms). Worse, RTT drifts across a long run,
    # so a probe taken at minute 0 doesn't describe minute 18; subtracting it produced
    # physically impossible NEGATIVE engine times.
    #
    # Amplification sidesteps the problem instead of correcting for it: put N operations
    # inside one statement, and the single round trip amortises to RTT/N. At N=200 a
    # 300 ms RTT contributes 1.5 ms per operation, and engine time dominates. The
    # per-op cost is then (total - RTT) / N, and the RTT term barely matters.
    #
    # Each variant preserves the per-start-node semantics of its single-shot twin
    # (note the WITH ... per-p grouping) so we're amplifying the same logical work,
    # not substituting a cheaper query that happens to batch well.
    Q_AMP_ONE_HOP = (
        "UNWIND $pids AS p "
        "MATCH (n:Person {pid: p})-[:FRIEND]->(m) "
        "WITH p, count(m) AS c RETURN sum(c) AS total"
    )
    Q_AMP_TWO_HOP = (
        "UNWIND $pids AS p "
        "MATCH (n:Person {pid: p})-[:FRIEND]->()-[:FRIEND]->(m) "
        "WITH p, count(DISTINCT m) AS c RETURN sum(c) AS total"
    )
    Q_AMP_THREE_HOP = (
        "UNWIND $pids AS p "
        "MATCH (n:Person {pid: p})-[:FRIEND]->()-[:FRIEND]->()-[:FRIEND]->(m) "
        "WITH p, count(DISTINCT m) AS c RETURN sum(c) AS total"
    )
    Q_AMP_POINT_LOOKUP = (
        "UNWIND $pids AS p "
        "MATCH (n:Person {pid: p}) RETURN count(n) AS total"
    )
    Q_AMP_INDEXED_LOOKUP = (
        "UNWIND $ages AS a "
        "MATCH (n:Person) WHERE n.age = a "
        "WITH a, collect(n.pid)[..100] AS rows RETURN count(rows) AS total"
    )
    Q_AMP_AGGREGATION = (
        "UNWIND range(1, $n) AS i "
        "MATCH (n:Person) WITH i, n.age AS age, count(*) AS c RETURN count(*) AS total"
    )

    def amplified_ops(self, workload: str, pids: list, ages: list, n_ops: int) -> None:
        """Execute n_ops of `workload` inside a single round trip."""
        if workload == "one_hop":
            self._run_query(self.Q_AMP_ONE_HOP, {"pids": pids[:n_ops]})
        elif workload == "two_hop":
            self._run_query(self.Q_AMP_TWO_HOP, {"pids": pids[:n_ops]})
        elif workload == "three_hop":
            self._run_query(self.Q_AMP_THREE_HOP, {"pids": pids[:n_ops]})
        elif workload == "point_lookup":
            self._run_query(self.Q_AMP_POINT_LOOKUP, {"pids": pids[:n_ops]})
        elif workload == "indexed_lookup":
            self._run_query(self.Q_AMP_INDEXED_LOOKUP, {"ages": ages[:n_ops]})
        elif workload == "aggregation":
            self._run_query(self.Q_AMP_AGGREGATION, {"n": n_ops})
        else:
            raise ValueError(f"unknown workload: {workload}")

    def graph_counts(self) -> dict:
        nodes = self._run_query(self.Q_COUNT_NODES, {})[0]["c"]
        rels = self._run_query(self.Q_COUNT_RELS, {})[0]["c"]
        return {"nodes": int(nodes), "rels": int(rels)}

    def reset(self) -> None:
        """Drop all :Person nodes and their relationships.

        Deleted in batches rather than one `MATCH (n) DETACH DELETE n`: a single
        transaction holding 200k+ relationship deletions will exhaust the 256 MB
        free-tier heap we're deliberately benchmarking against. Loops until the
        graph is empty because each call only removes up to `batch`."""
        while True:
            rows = self._run_query(
                "MATCH (n:Person) WITH n LIMIT 5000 DETACH DELETE n RETURN count(n) AS c",
                {},
            )
            if not rows or int(rows[0]["c"]) == 0:
                break

    def warmup(self, start_node_ids: list) -> None:
        # One pass through every query family so query plans, connection pools, and
        # (for on-disk engines) the OS page cache are primed before we start timing.
        for pid in start_node_ids[:5]:
            self._run_query(self.Q_ONE_HOP, {"pid": pid})
            self._run_query(self.Q_TWO_HOP, {"pid": pid})
            self._run_query(self.Q_THREE_HOP, {"pid": pid})
            self._run_query(self.Q_POINT_LOOKUP, {"pid": pid})
        self._run_query(self.Q_INDEXED_LOOKUP, {"age": 25})
        self._run_query(self.Q_AGGREGATION, {})

    def one_hop(self, start_node_id) -> None:
        self._run_query(self.Q_ONE_HOP, {"pid": start_node_id})

    def two_hop(self, start_node_id) -> None:
        self._run_query(self.Q_TWO_HOP, {"pid": start_node_id})

    def three_hop(self, start_node_id) -> None:
        self._run_query(self.Q_THREE_HOP, {"pid": start_node_id})

    def point_lookup(self, node_id) -> None:
        self._run_query(self.Q_POINT_LOOKUP, {"pid": node_id})

    def indexed_lookup(self, filter_value) -> None:
        self._run_query(self.Q_INDEXED_LOOKUP, {"age": filter_value})

    def aggregation(self) -> None:
        self._run_query(self.Q_AGGREGATION, {})

    def concurrent_workload(self, n_clients: int, rw_mix: float, duration_sec: float) -> dict:
        """Sustained mixed workload: n_clients threads hammer read/write queries for
        duration_sec, using a shared random-pid pool so every DB sees the same access
        pattern shape. Wall-clock-bounded (not op-count-bounded) so the throughput
        number is directly comparable at any concurrency level."""
        # Local RNG seeded from the caller's global seed via random.getstate() isn't
        # threaded through here deliberately: workload pid selection just needs to be
        # *representative*, not reproduced bit-for-bit run to run (unlike the fixed
        # traversal start-node set, which does need exact reproducibility).
        stop_at = time.monotonic() + duration_sec
        op_count = [0] * n_clients
        error_count = [0] * n_clients
        pid_pool = self._sample_pids_for_workload()

        def worker(idx: int):
            rng = random.Random(idx)
            while time.monotonic() < stop_at:
                pid = rng.choice(pid_pool)
                try:
                    if rng.random() < rw_mix:
                        self._run_query(self.Q_ONE_HOP, {"pid": pid})
                    else:
                        self._run_query(self.Q_WRITE, {"pid": pid})
                    op_count[idx] += 1
                except Exception:
                    error_count[idx] += 1

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_clients)]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - t0

        total_ops = sum(op_count)
        return {
            "n_clients": n_clients,
            "rw_mix": rw_mix,
            "duration_sec": elapsed,
            "n_ops": total_ops,
            "n_errors": sum(error_count),
            "throughput_qps": total_ops / elapsed if elapsed > 0 else 0.0,
        }

    def _sample_pids_for_workload(self, n: int = 200) -> list:
        """A representative pool of existing pids for the mixed workload to hit.
        Not the same fixed-seed set used for traversal latency measurement —
        the mixed workload cares about sustained throughput under realistic
        access patterns, not reproducing one exact trace."""
        rows = self._run_query(
            "MATCH (n:Person) RETURN n.pid AS pid LIMIT $n", {"n": n}
        )
        return [r["pid"] for r in rows] or [0]
