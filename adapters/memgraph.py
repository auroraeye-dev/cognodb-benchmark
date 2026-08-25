"""Memgraph adapter — self-hosted in Docker (docker/docker-compose.yml), in-memory
storage engine, still speaks Bolt + Cypher so it reuses BoltCypherAdapter's driver
plumbing. Only load's DDL syntax differs from Neo4j-family (Memgraph's constraint/
index grammar predates `IF NOT EXISTS` support in some versions), and footprint
uses Memgraph's own `SHOW STORAGE INFO` instead of APOC.

Why this is interesting to compare: Memgraph is in-memory (no disk I/O on the read
path) capped to the same 256 MB as CognoDB's free tier — see the fairness ledger
in README for exactly what "capped to match" meant in practice for an in-memory
engine that may refuse to run that small.
"""
import time

from adapters.bolt_common import BoltCypherAdapter

LOAD_BATCH_SIZE = 1000


class MemgraphAdapter(BoltCypherAdapter):
    def __init__(self, uri: str, user: str, password: str):
        super().__init__(uri, user, password, name="Memgraph")

    def load(self, dataset) -> dict:
        t0 = time.perf_counter()

        # CRITICAL, and a genuine engine difference: in Memgraph a uniqueness
        # constraint is NOT index-backed. Neo4j/CognoDB create a backing index for a
        # unique constraint automatically, so `MATCH (n:Person {pid: $pid})` is an
        # index seek there. Memgraph enforces uniqueness without building a
        # label-property index, so the same query degrades to a full label scan of
        # every :Person node.
        #
        # We measured the consequence before catching it: relationship ingest was
        # doing two 16,000-node scans per edge and had managed 144k of 204k edges in
        # over 10 minutes, versus 80 s on CognoDB. Every traversal and point-lookup
        # number would likewise have been scan-based and not comparable with the other
        # engines. The explicit pid index below is what makes the comparison fair.
        #
        # Memgraph's DDL doesn't take `IF NOT EXISTS` on all versions; swallow
        # "already exists" so load() stays idempotent across re-runs.
        for ddl in (
            "CREATE CONSTRAINT ON (n:Person) ASSERT n.pid IS UNIQUE",
            "CREATE INDEX ON :Person(pid)",
            "CREATE INDEX ON :Person(age)",
        ):
            try:
                self._run_query(ddl, {})
            except Exception:
                pass

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
        try:
            rows = self._run_query("SHOW STORAGE INFO", {})
            return {"observable": True, "raw": rows}
        except Exception as exc:
            return {"observable": False, "reason": str(exc)}


def _chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]
