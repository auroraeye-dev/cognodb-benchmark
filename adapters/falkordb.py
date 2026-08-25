"""FalkorDB adapter — self-hosted in Docker, OpenCypher over Redis (RESP protocol,
not Bolt), sparse-matrix (GraphBLAS) query engine. Reuses the exact same Cypher
query text as every other CypherAdapter subclass (see adapters/cypher_base.py) —
only the wire protocol and driver call shape differ, which is the whole point:
if FalkorDB wins or loses a query family, it's the GraphBLAS engine's doing, not
a different query.

Why its own driver wiring instead of BoltCypherAdapter: FalkorDB isn't Bolt, it's
Redis RESP with a Cypher-over-RESP command (GRAPH.QUERY), wrapped by the `falkordb`
client. Hence a separate CypherAdapter subclass rather than reusing bolt_common.py.
"""
import time

from falkordb import FalkorDB

from adapters.cypher_base import CypherAdapter

LOAD_BATCH_SIZE = 1000


class FalkorDBAdapter(CypherAdapter):
    def __init__(self, host: str, port: int, password: str, graph_name: str,
                 socket_timeout: float = 30.0):
        self.host = host
        self.port = port
        self.password = password or None
        self.graph_name = graph_name
        # 30s: far longer than any healthy query here (the slowest single read observed
        # is ~12 ms) so it never trips on legitimate work, but short enough that a
        # wedged connection fails instead of stalling the whole run.
        self.socket_timeout = socket_timeout
        self.name = "FalkorDB"
        self._client = None
        self._graph = None

    def connect(self) -> None:
        # socket_timeout is not optional here. FalkorDB serialises writes against the
        # whole graph, and under the 40-client mixed workload (20% writes) we observed
        # queries block indefinitely: 41 connections sat with Redis flags=b for 11
        # minutes with the server at <1% CPU. The workload threads check a wall-clock
        # deadline, but a thread blocked *inside* a query never reaches that check, so
        # the sweep hung forever rather than ending.
        #
        # With a timeout, a wedged query raises instead of hanging. That turns an
        # un-runnable benchmark into a measurable one: the timeouts surface as errors
        # in the concurrency results, which is the honest way to report an engine that
        # stops serving under write contention.
        self._client = FalkorDB(
            host=self.host, port=self.port, password=self.password,
            socket_timeout=self.socket_timeout,
            socket_connect_timeout=10,
        )
        self._graph = self._client.select_graph(self.graph_name)

    def _run_query(self, cypher: str, params: dict) -> list:
        result = self._graph.query(cypher, params)
        return _rows_to_dicts(result)

    def load(self, dataset) -> dict:
        t0 = time.perf_counter()

        # FalkorDB's index DDL (RedisGraph lineage): label+property index, not a
        # uniqueness constraint. Documented as a fairness-relevant difference in the
        # README's indexing table — pid uniqueness is enforced by dataset generation
        # (each SNAP node id is unique), not by the engine, on this one platform.
        for ddl in (
            "CREATE INDEX FOR (n:Person) ON (n.pid)",
            "CREATE INDEX FOR (n:Person) ON (n.age)",
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
            "load_method": "driver batching (UNWIND via GRAPH.QUERY, batch size %d)" % LOAD_BATCH_SIZE,
        }

    def footprint(self) -> dict:
        try:
            info = self._client.connection.execute_command("INFO", "memory")
            return {"observable": True, "raw": str(info)}
        except Exception as exc:
            return {"observable": False, "reason": str(exc)}

    def close(self) -> None:
        # falkordb-py wraps redis-py; the underlying connection pool closes with
        # the process, but drop our reference so a re-connect in the same process
        # (e.g. variance-run repeats) builds a fresh client rather than reusing state.
        self._client = None
        self._graph = None


def _rows_to_dicts(result) -> list:
    """Normalize a falkordb QueryResult into the same list-of-dict shape every
    other adapter's _run_query returns, so harness code never branches on DB type."""
    header = getattr(result, "header", None) or []
    # falkordb-py has represented headers as either plain strings or
    # (type, name) tuples across versions; handle both defensively.
    col_names = [h[1] if isinstance(h, (list, tuple)) else h for h in header]
    rows = []
    for record in getattr(result, "result_set", []) or []:
        if col_names and len(col_names) == len(record):
            rows.append(dict(zip(col_names, record)))
        else:
            rows.append({str(i): v for i, v in enumerate(record)})
    return rows


def _chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]
