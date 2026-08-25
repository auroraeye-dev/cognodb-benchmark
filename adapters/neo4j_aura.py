"""Neo4j Aura Free adapter — the most direct apples-to-apples comparison to CognoDB:
same wire protocol (Bolt), same query language (Cypher), same "managed free tier"
deployment shape. Any latency gap here is much more likely to be genuine engine
difference than protocol/language difference.

Load path deliberately mirrors adapters/cognodb.py exactly (driver batching, same
batch size, same constraint/index) so ingest numbers are comparable rather than an
artifact of two different loading strategies.
"""
import time

from adapters.bolt_common import BoltCypherAdapter

LOAD_BATCH_SIZE = 1000


class Neo4jAuraAdapter(BoltCypherAdapter):
    def __init__(self, uri: str, user: str, password: str, database: str = None):
        # user/database both come from env: some Aura instances authenticate as the
        # instance id rather than "neo4j", and expose the database under that same id.
        super().__init__(uri, user, password, name="Neo4j Aura Free", database=database)

    def load(self, dataset) -> dict:
        t0 = time.perf_counter()

        self._run_query(
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Person) REQUIRE n.pid IS UNIQUE",
            {},
        )
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
        try:
            rows = self._run_query(
                "CALL apoc.monitor.store() YIELD stringStoreSize RETURN stringStoreSize",
                {},
            )
            return {"observable": True, "raw": rows}
        except Exception as exc:
            return {
                "observable": False,
                "reason": f"Aura Free does not expose storage introspection to this role ({exc})",
            }


def _chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]
