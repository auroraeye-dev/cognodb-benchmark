"""NebulaGraph adapter — self-hosted in Docker, nGQL (NOT Cypher). Deliberately does
NOT inherit from CypherAdapter: it implements GraphDBAdapter directly and hand-
translates each query family into nGQL's GO/FETCH/LOOKUP/pipe syntax.

This is the one disclosed language-paradigm gap in the whole benchmark (see README
fairness ledger): every query below is written to be the closest faithful nGQL
equivalent of the matching Q_* Cypher string in adapters/cypher_base.py, not a
"whatever's fast on Nebula" query. Where nGQL's execution model forces a materially
different approach (e.g. no native unique-constraint-on-property — the vertex ID
*is* the identifier), that's called out inline and in the README.

Parameter handling: nebula3-python's native parameter binding requires building
nebula.Value protobuf objects per type, which adds a lot of ceremony for values
that are always internally-generated integers (pid, age) from our own seeded
dataset sample — never user input. We format them directly into the nGQL string
rather than parameter-bind; this is safe here specifically because there is no
untrusted input path, and it keeps the query text readable for the interview
defense. This would NOT be safe in an app talking to Nebula on behalf of end users.
"""
import time

from nebula3.Config import Config
from nebula3.gclient.net import ConnectionPool

from adapters.base import GraphDBAdapter

LOAD_BATCH_SIZE = 500  # smaller than Cypher DBs' 1000: nGQL INSERT statements are more verbose per-row


class NebulaAdapter(GraphDBAdapter):
    def __init__(self, host: str, port: int, user: str, password: str, space: str,
                 storage_host: str = "nebula-storaged", storage_port: int = 9779):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.space = space
        # Storage host as the META service resolves it — i.e. the compose service name
        # on the container network, NOT localhost. graphd is reached from the host via
        # a published port, but metad talks to storaged container-to-container.
        self.storage_host = storage_host
        self.storage_port = storage_port
        self.name = "NebulaGraph"
        self._pool = None
        self._session = None

    def connect(self) -> None:
        config = Config()
        config.max_connection_pool_size = 20
        self._pool = ConnectionPool()
        self._pool.init([(self.host, self.port)], config)
        self._session = self._pool.get_session(self.user, self.password)
        # Point the session at the benchmark space immediately. Nebula sessions carry
        # no default space, so every read method would otherwise fail with
        # "Space was not chosen" on a --no-load run where load() never ran to set it.
        # Tolerates a missing space so a first-time load can still create it.
        self._use_space()

    def _exec(self, ngql: str):
        result = self._session.execute(ngql)
        if not result.is_succeeded():
            raise RuntimeError(f"nGQL failed: {ngql!r} -> {result.error_msg()}")
        return result

    def load(self, dataset) -> dict:
        t0 = time.perf_counter()

        # Nebula 3.x does NOT auto-register storage nodes with the meta service: until
        # ADD HOSTS runs, the cluster has zero storage hosts and CREATE SPACE either
        # fails or produces a space with no online partitions. This is a one-time
        # cluster bootstrap, not part of the measured load, so it happens before t0's
        # work begins and is cheap enough not to distort ingest throughput.
        # Idempotent in practice: re-adding an existing host is an error we swallow.
        try:
            self._exec(f'ADD HOSTS "{self.storage_host}":{self.storage_port}')
        except Exception:
            pass
        # Wait for storage to actually report ONLINE rather than assuming a fixed delay.
        # Note this is checked whether or not ADD HOSTS just succeeded — on a re-run the
        # host already exists, ADD HOSTS raises, and skipping the wait on that path is
        # what left the cluster unready in an earlier attempt.
        self._wait_until(
            lambda: "ONLINE" in str(self._exec("SHOW HOSTS")),
            timeout=90, what="storage host ONLINE",
        )

        self._exec(
            f"CREATE SPACE IF NOT EXISTS {self.space} "
            "(vid_type=INT64, partition_num=10, replica_factor=1)"
        )
        # CREATE SPACE returns before the space is usable, and "usable" has two distinct
        # stages that bit us separately:
        #   1. graphd accepts USE — the metadata exists.
        #   2. storaged has allocated partition LEADERS for the space — it can serve reads
        #      and index operations.
        # Waiting only for (1) let REBUILD INDEX run too early and fail with
        # "SpaceNotFound: SpaceId `2`" — graphd knew the space, storage did not yet.
        # SHOW HOSTS reports leader distribution per space, so we poll for the space
        # name appearing there, which is the real readiness signal.
        self._wait_until(self._use_space, timeout=90, what=f"space {self.space} accepted by graphd")
        self._exec("CREATE TAG IF NOT EXISTS Person(age int)")
        self._exec("CREATE EDGE IF NOT EXISTS FRIEND()")
        # Tag/edge schema is likewise heartbeat-propagated — poll until an INSERT-shaped
        # read of the schema succeeds rather than guessing at a sleep duration.
        self._wait_until(
            lambda: "Person" in str(self._exec("SHOW TAGS"))
            and "FRIEND" in str(self._exec("SHOW EDGES")),
            timeout=90, what="Person tag and FRIEND edge visible",
        )
        # person_age_idx backs indexed_lookup (the analogue of the other engines' index
        # on Person.age). Nebula additionally requires an index on a tag/edge before
        # LOOKUP can scan it AT ALL, so person_all_idx and friend_idx exist purely so
        # graph_counts() can verify the load — without them LOOKUP errors rather than
        # returning zero, which would be a baffling failure at the verification step.
        #
        # Indexes are created before any data is inserted, matching the other four
        # engines, so ingest throughput is measured with index maintenance included on
        # every platform rather than only on some.
        self._exec("CREATE TAG INDEX IF NOT EXISTS person_age_idx ON Person(age)")
        self._exec("CREATE TAG INDEX IF NOT EXISTS person_all_idx ON Person()")
        self._exec("CREATE EDGE INDEX IF NOT EXISTS friend_idx ON FRIEND()")
        # Index DDL is heartbeat-propagated like everything else; REBUILD on an index
        # the storage layer hasn't seen yet fails with "index not found".
        self._wait_until(
            lambda: all(k in str(self._exec("SHOW TAG INDEXES"))
                        for k in ("person_age_idx", "person_all_idx"))
            and "friend_idx" in str(self._exec("SHOW EDGE INDEXES")),
            timeout=90, what="tag/edge indexes visible",
        )
        # REBUILD is retried rather than gated on a readiness proxy. Every proxy signal
        # we tried (USE succeeding, the space appearing in SHOW HOSTS' partition
        # distribution) reported ready while REBUILD still failed with
        # "SpaceNotFound: SpaceId `N`" — graphd and metad know the space before storaged
        # can serve index operations on it, and none of the observable signals mark that
        # last transition. Retrying the actual operation is the only honest readiness
        # test available.
        #
        # Best-effort by design: we create the indexes BEFORE inserting any data, and on
        # an empty tag Nebula's indexes are populated by subsequent INSERTs. REBUILD is
        # only strictly required to backfill an index added to pre-existing data, which
        # is never our case here. So if it still fails after retrying we record that and
        # continue rather than abort — the indexes will be correct either way, and
        # graph_counts() would catch it if they weren't.
        rebuild_failures = []
        for stmt in ("REBUILD TAG INDEX person_age_idx",
                     "REBUILD TAG INDEX person_all_idx",
                     "REBUILD EDGE INDEX friend_idx"):
            try:
                self._wait_until(lambda s=stmt: self._try_exec(s), timeout=120, what=stmt)
            except RuntimeError as exc:
                rebuild_failures.append(f"{stmt}: {exc}")

        nodes_loaded = 0
        for batch in _chunk(dataset.nodes, LOAD_BATCH_SIZE):
            values = ",".join(f'{row["pid"]}:({row["age"] or 0})' for row in batch)
            self._exec(f"INSERT VERTEX Person(age) VALUES {values}")
            nodes_loaded += len(batch)

        rels_loaded = 0
        for batch in _chunk(dataset.edges, LOAD_BATCH_SIZE):
            values = ",".join(f"{s}->{d}:()" for s, d in batch)
            self._exec(f"INSERT EDGE FRIEND() VALUES {values}")
            rels_loaded += len(batch)

        elapsed = time.perf_counter() - t0
        return {
            "nodes_loaded": nodes_loaded,
            "rels_loaded": rels_loaded,
            "wall_clock_sec": elapsed,
            "nodes_per_sec": nodes_loaded / elapsed if elapsed > 0 else 0.0,
            "rels_per_sec": rels_loaded / elapsed if elapsed > 0 else 0.0,
            "load_method": "driver batching (INSERT VERTEX/EDGE VALUES, batch size %d)" % LOAD_BATCH_SIZE,
            # Surfaced rather than swallowed: if REBUILD never succeeded, the reader
            # should know, even though indexes are expected to be correct regardless
            # (created before insert, so INSERTs populate them).
            "index_rebuild_failures": rebuild_failures,
        }

    def _try_exec(self, ngql: str) -> bool:
        """Run a statement, returning success as a bool instead of raising. For use as
        a _wait_until predicate where the operation itself is the readiness test."""
        try:
            self._exec(ngql)
            return True
        except Exception:
            return False

    def _wait_until(self, check, timeout: float, what: str, interval: float = 2.0):
        """Poll `check` until it returns truthy, or raise after `timeout` seconds.

        Nebula propagates DDL through the meta service on a heartbeat, so CREATE SPACE,
        ADD HOSTS and CREATE/REBUILD INDEX all return before their effect is visible.
        Fixed sleeps are the wrong tool: too short and the next statement fails
        ("SpaceNotFound" right after a successful CREATE SPACE — which is exactly what
        bit us), too long and every run pays for the worst case. Polling makes the wait
        self-terminating and turns a timeout into an explicit error instead of a
        confusing downstream failure."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if check():
                    return
            except Exception:
                pass
            time.sleep(interval)
        raise RuntimeError(f"Nebula: timed out after {timeout}s waiting for {what}")

    def _use_space(self) -> bool:
        """Select the benchmark space. Returns False if it doesn't exist yet.

        Unlike the Cypher engines, Nebula has no implicit default database: every
        session must USE a space before any read, and a space that hasn't been created
        makes even a count query a semantic error rather than an empty result. This is
        why graph_counts() has to distinguish "no space" from "space with zero rows" —
        the pre-load count check hits the former on a fresh cluster."""
        try:
            self._exec(f"USE {self.space}")
            return True
        except Exception:
            return False

    def graph_counts(self) -> dict:
        """nGQL equivalent of the Cypher COUNT queries.

        Nebula's `SHOW STATS` is populated by an asynchronous SUBMIT JOB STATS and is
        stale until that job finishes, so it's unusable as a post-load assertion. We
        instead LOOKUP over the index and count, which is slower but reflects committed
        state immediately — correctness matters more than speed for a verification step
        that runs once per load."""
        # An uninitialised graph is an empty graph, not an error. On a fresh cluster the
        # space is missing; on a cluster where a previous load died partway the space
        # exists but the Person tag / FRIEND edge don't. Both mean "zero rows".
        #
        # Deliberately narrow: only the two "doesn't exist yet" errors map to zero.
        # Anything else re-raises, so a genuine post-load failure still surfaces as a
        # count mismatch instead of being silently reported as an empty graph.
        if not self._use_space():
            return {"nodes": 0, "rels": 0}
        try:
            return self._counts_from_lookup()
        except Exception as exc:
            msg = str(exc)
            if "Schema not exist" in msg or "SpaceNotFound" in msg:
                return {"nodes": 0, "rels": 0}
            raise

    def _counts_from_lookup(self) -> dict:
        # Use column_values(), NOT rows(). rows() hands back raw thrift `Value` objects
        # with no typed accessors; column_values() wraps them in `ValueWrapper`, which
        # is where as_int()/cast() live. Same data, and only one of the two paths has a
        # usable API.
        return {
            "nodes": self._scalar_int(
                "LOOKUP ON Person YIELD id(vertex) AS vid | YIELD COUNT($-.vid) AS c", "c"),
            "rels": self._scalar_int(
                "LOOKUP ON FRIEND YIELD src(edge) AS s | YIELD COUNT($-.s) AS c", "c"),
        }

    def _scalar_int(self, ngql: str, column: str) -> int:
        vals = self._exec(ngql).column_values(column)
        return int(vals[0].as_int()) if vals else 0

    def reset(self) -> None:
        """Drop and recreate the space — far cheaper in Nebula than deleting vertices
        one batch at a time, and guarantees indexes/schema start clean too. The sleep
        covers Nebula's asynchronous heartbeat-based schema propagation (same reason
        load() sleeps after CREATE SPACE)."""
        self._exec(f"DROP SPACE IF EXISTS {self.space}")
        # DROP SPACE is asynchronous: storage keeps reclaiming partitions after the
        # statement returns. Creating and inserting into a same-named space too soon
        # collides with that cleanup ("More than one request trying to add/update/delete
        # one edge/vertex at the same time"). Wait until the space is genuinely gone.
        self._wait_until(
            lambda: self.space not in str(self._exec("SHOW SPACES")),
            timeout=120, what=f"space {self.space} fully dropped",
        )
        # Even once metad forgets the space, storage reclamation lags; this margin is
        # empirical, not principled, and is the pragmatic price of Nebula's async DDL.
        time.sleep(10)

    def warmup(self, start_node_ids: list) -> None:
        for pid in start_node_ids[:5]:
            self.one_hop(pid)
            self.two_hop(pid)
            self.three_hop(pid)
            self.point_lookup(pid)
        self.indexed_lookup(25)
        self.aggregation()

    def one_hop(self, start_node_id) -> None:
        self._exec(
            f"GO 1 STEPS FROM {start_node_id} OVER FRIEND "
            "YIELD DISTINCT dst(edge) AS m | YIELD COUNT($-.m) AS c"
        )

    def two_hop(self, start_node_id) -> None:
        self._exec(
            f"GO 2 STEPS FROM {start_node_id} OVER FRIEND "
            "YIELD DISTINCT dst(edge) AS m | YIELD COUNT($-.m) AS c"
        )

    def three_hop(self, start_node_id) -> None:
        self._exec(
            f"GO 3 STEPS FROM {start_node_id} OVER FRIEND "
            "YIELD DISTINCT dst(edge) AS m | YIELD COUNT($-.m) AS c"
        )

    def point_lookup(self, node_id) -> None:
        # Nebula has no separate "primary index" concept to disclose here: the
        # vertex ID itself IS the lookup key, so FETCH is an O(1) ID-based fetch
        # by construction, not a query-planner decision like the Cypher DBs' pid index.
        self._exec(f"FETCH PROP ON Person {node_id} YIELD Person.age AS age")

    def indexed_lookup(self, filter_value) -> None:
        self._exec(
            f"LOOKUP ON Person WHERE Person.age == {filter_value} "
            "YIELD Person.age AS age | LIMIT 100"
        )

    def aggregation(self) -> None:
        self._exec(
            "LOOKUP ON Person YIELD Person.age AS age "
            "| GROUP BY $-.age YIELD $-.age AS age, COUNT(*) AS c"
        )

    def concurrent_workload(self, n_clients: int, rw_mix: float, duration_sec: float) -> dict:
        import random
        import threading

        stop_at = time.monotonic() + duration_sec
        op_count = [0] * n_clients
        error_count = [0] * n_clients
        pool = self._pool
        space = self.space
        pid_pool = self._sample_pids_for_workload()

        def worker(idx: int):
            # Nebula sessions are not thread-safe; each worker thread gets its own
            # session from the shared connection pool (same pattern the Bolt/Redis
            # drivers give us for free via their own internal pooling).
            session = pool.get_session(self.user, self.password)
            session.execute(f"USE {space}")
            rng = random.Random(idx)
            while time.monotonic() < stop_at:
                pid = rng.choice(pid_pool)
                try:
                    if rng.random() < rw_mix:
                        session.execute(
                            f"GO 1 STEPS FROM {pid} OVER FRIEND "
                            "YIELD DISTINCT dst(edge) AS m | YIELD COUNT($-.m) AS c"
                        )
                    else:
                        session.execute(
                            f"UPDATE VERTEX ON Person {pid} SET age = age"
                        )
                    op_count[idx] += 1
                except Exception:
                    error_count[idx] += 1
            session.release()

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
        # column_values(), not rows() — see _scalar_int for why.
        result = self._exec(f"LOOKUP ON Person YIELD id(vertex) AS pid | LIMIT {n}")
        pids = [v.as_int() for v in result.column_values("pid")]
        return pids or [0]

    def footprint(self) -> dict:
        try:
            result = self._exec(f"SHOW STATS")
            return {"observable": True, "raw": str(result)}
        except Exception as exc:
            return {"observable": False, "reason": str(exc)}

    def close(self) -> None:
        if self._session is not None:
            self._session.release()
        if self._pool is not None:
            self._pool.close()


def _chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]
