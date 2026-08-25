"""Shared plumbing for every DB reachable over Bolt via the official `neo4j` driver:
CognoDB, Neo4j Aura, and Memgraph. FalkorDB is Cypher too but speaks RESP (Redis
protocol), not Bolt, so it gets its own driver wiring in falkordb_adapter.py while
still reusing the same query text from CypherAdapter.

Why share this: connect/_run_query/close/footprint-via-dbms-calls are pure driver
boilerplate. Keeping them in one place means the only thing that differs between
CognoDB, Aura, and Memgraph adapter files is URI/auth and each engine's load path
and footprint quirks — exactly the things that SHOULD differ and be visible.
"""
import time

from neo4j import GraphDatabase

from adapters.cypher_base import CypherAdapter


class BoltCypherAdapter(CypherAdapter):
    def __init__(self, uri: str, user: str, password: str, name: str, database: str = None):
        self.uri = uri
        self.user = user
        self.password = password
        self.name = name
        # Database name is per-deployment, NOT a safe constant. Neo4j's own default is
        # "neo4j", but Aura instances can be provisioned where both the username and the
        # database are the instance id instead — hardcoding "neo4j" silently connects to
        # the wrong place (or fails auth outright). None => let the driver use the
        # server's default, which is correct for CognoDB and stock Neo4j.
        self.database = database or None
        self._driver = None

    def connect(self) -> None:
        auth = (self.user, self.password) if self.user else None
        # Bounded timeouts, learned the hard way: an Aura Free instance dropped the
        # connection mid-suite and the driver's defaults left the run hanging for
        # 83 minutes before surfacing "defunct connection". Failing in ~2 minutes
        # instead means a flaky managed instance costs one run, not an evening.
        self._driver = GraphDatabase.driver(
            self.uri, auth=auth,
            connection_timeout=30.0,           # TCP/TLS handshake
            max_transaction_retry_time=60.0,   # total retry budget per transaction
            connection_acquisition_timeout=60.0,
            max_connection_lifetime=600,       # recycle before a managed proxy times us out
            keep_alive=True,
        )
        self._driver.verify_connectivity()

    def _run_query(self, cypher: str, params: dict) -> list:
        with self._driver.session(database=self.database) as session:
            result = session.run(cypher, params)
            return [dict(r) for r in result]

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()

    def measure_network_rtt(self, iterations: int = 30) -> dict:
        """Trivial `RETURN 1` round-trip, timed, to compute the network-subtracted
        "engine-only" latency the fairness rules call for. Same query, same driver
        call path as every real query, so the only variable is "does the network
        round-trip to a managed cloud instance vs a local Docker container"."""
        samples = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            self._run_query("RETURN 1 AS x", {})
            samples.append((time.perf_counter() - t0) * 1000.0)
        return {"rtt_ms_samples": samples}

    def footprint(self) -> dict:
        # `CALL dbms.*` procedures are not standardized across Bolt-speaking engines
        # (Neo4j/Aura/CognoDB expose them via APOC/dbms procs; Memgraph has its own
        # `SHOW STORAGE INFO`). Each concrete subclass overrides this with what its
        # engine actually exposes; this default is the honest fallback.
        return {"observable": False, "reason": "not implemented for this engine yet"}
