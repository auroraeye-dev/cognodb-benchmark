"""CognoDB Cloud adapter — the subject of this benchmark.

CognoDB speaks Bolt + Cypher, so it's a thin subclass of BoltCypherAdapter: only
the load path (batched via the driver, since CognoDB's free tier gives us no
bulk-import tool) and footprint (whatever the managed control plane exposes)
are specific to this engine.

Why driver batching for load: CognoDB Cloud is a managed free-tier instance with
no filesystem access for a bulk-import CLI (unlike self-hosted Memgraph/FalkorDB/
Nebula, which can use their native bulk loaders). This is a disclosed fairness
caveat in the README — load-path differences are a documented, not hidden, gap.
"""
import time

from adapters.bolt_common import BoltCypherAdapter

# Why 1000: large enough that per-batch round-trip overhead is negligible, small
# enough to stay well under any reasonable request-size / memory limit on a
# 256 MB free-tier instance.
LOAD_BATCH_SIZE = 1000


class CognoDBAdapter(BoltCypherAdapter):
    def __init__(self, uri: str, user: str, password: str, database: str = None):
        # database is optional/env-driven for symmetry with the Aura adapter; CognoDB
        # uses the server default when COGNODB_DATABASE is unset.
        super().__init__(uri, user, password, name="CognoDB Cloud", database=database)

    def load(self, dataset) -> dict:
        t0 = time.perf_counter()

        # Constraint doubles as the primary index on Person.pid — this is the index
        # every point_lookup/traversal query relies on. Created before data load so
        # writes stay index-maintained throughout (matches how the other Cypher DBs
        # are loaded, for a fair ingest-throughput comparison).
        self._run_query(
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Person) REQUIRE n.pid IS UNIQUE",
            {},
        )
        # Secondary index backing indexed_lookup(age). Documented in the README's
        # "which properties are indexed" table.
        self._run_query(
            "CREATE INDEX IF NOT EXISTS FOR (n:Person) ON (n.age)", {}
        )

        nodes_loaded = 0
        for batch in _chunk(dataset.nodes, LOAD_BATCH_SIZE):
            self._run_query(
                "UNWIND $rows AS row "
                "CREATE (n:Person {pid: row.pid, age: row.age})",
                {"rows": batch},
            )
            nodes_loaded += len(batch)

        rels_loaded = 0
        for batch in _chunk(dataset.edges, LOAD_BATCH_SIZE):
            self._run_query(
                "UNWIND $rows AS row "
                "MATCH (a:Person {pid: row.src}), (b:Person {pid: row.dst}) "
                "CREATE (a)-[:FRIEND]->(b)",
                {"rows": [{"src": s, "dst": d} for s, d in batch]},
            )
            rels_loaded += len(batch)

        elapsed = time.perf_counter() - t0
        return {
            "nodes_loaded": nodes_loaded,
            "rels_loaded": rels_loaded,
            "wall_clock_sec": elapsed,
            "nodes_per_sec": nodes_loaded / elapsed if elapsed > 0 else 0.0,
            "rels_per_sec": rels_loaded / elapsed if elapsed > 0 else 0.0,
            "load_method": "driver batching (UNWIND, batch size %d)" % LOAD_BATCH_SIZE,
        }

    def footprint(self) -> dict:
        # Free-tier managed instances typically don't expose `dbms.*` JMX/storage
        # procedures to non-admin users. Try the standard one; fall back honestly.
        try:
            rows = self._run_query(
                "CALL apoc.monitor.store() YIELD stringStoreSize RETURN stringStoreSize",
                {},
            )
            return {"observable": True, "raw": rows}
        except Exception as exc:
            return {
                "observable": False,
                "reason": f"CognoDB free tier does not expose storage introspection ({exc})",
            }


def _chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]
