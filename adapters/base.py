"""Abstract interface every graph DB adapter must implement.

Why this exists: the harness (harness/runner.py) is written once against this
interface and never special-cased per database. That's what makes "same dataset,
same workload, same client, same measurement code" true by construction rather
than by discipline we might forget under deadline pressure.

Every method below returns raw driver results (or None), not pre-formatted
strings — the harness owns timing, percentiles, and formatting so that
measurement logic lives in exactly one place.
"""
from abc import ABC, abstractmethod


class GraphDBAdapter(ABC):
    """One instance = one connection to one graph database for the duration of a run."""

    # Human-readable name used in results JSON and README tables, e.g. "CognoDB Cloud".
    name: str

    @abstractmethod
    def connect(self) -> None:
        """Open the driver/session. Kept separate from __init__ so connection
        failures surface at a predictable point in the harness, not at import time."""
        raise NotImplementedError

    @abstractmethod
    def load(self, dataset) -> dict:
        """Bulk-load the sampled Pokec dataset (dataset.Dataset — see dataset/sample.py).

        Returns a dict with at least: nodes_loaded, rels_loaded, wall_clock_sec,
        nodes_per_sec, rels_per_sec, load_method (str, e.g. "driver batching" vs
        "bulk import tool") — the load method is a disclosed fairness caveat when
        it differs from other DBs, so it must always be recorded.
        """
        raise NotImplementedError

    @abstractmethod
    def graph_counts(self) -> dict:
        """Return {"nodes": int, "rels": int} as the ENGINE sees them, post-load.

        Why this is part of the interface rather than a load-time side effect: the
        central claim of this benchmark is "the identical dataset on every platform".
        That claim is only credible if it's verified against each engine independently
        after loading, rather than inferred from how many rows we think we sent. A
        silently truncated load (quota cap, dropped batch, MATCH that found no endpoint)
        would otherwise show up as a suspiciously fast engine, not as an error.
        """
        raise NotImplementedError

    @abstractmethod
    def reset(self) -> None:
        """Delete all benchmark data so load() is idempotent across re-runs.

        Without this, re-running a load silently doubles the graph and every
        subsequent number describes a dataset that matches no other platform.
        """
        raise NotImplementedError

    @abstractmethod
    def warmup(self, start_node_ids: list) -> None:
        """Run each query family once (or a few times) before timing begins, so the
        first-hit cost (query plan caching, connection pool spin-up, page cache fill
        for on-disk engines) doesn't contaminate the measured latencies. Cold-start
        numbers are reported separately, never mixed into the warm distribution."""
        raise NotImplementedError

    # --- Traversals: same start-node set (fixed seed) across every DB ---

    @abstractmethod
    def one_hop(self, start_node_id) -> None:
        raise NotImplementedError

    @abstractmethod
    def two_hop(self, start_node_id) -> None:
        raise NotImplementedError

    @abstractmethod
    def three_hop(self, start_node_id) -> None:
        raise NotImplementedError

    # --- Lookups ---

    @abstractmethod
    def point_lookup(self, node_id) -> None:
        """Lookup by the primary indexed id property. Should be O(1)-ish on every
        engine — this is the baseline every other latency number gets compared to."""
        raise NotImplementedError

    @abstractmethod
    def indexed_lookup(self, filter_value) -> None:
        """Filtered lookup on a secondary indexed property (e.g. age). Which property
        is indexed, and how, must be recorded per-DB in the README (indexing strategy
        is itself a fairness variable — an unindexed scan on one DB vs an indexed
        seek on another is not an apples-to-apples number)."""
        raise NotImplementedError

    # --- Aggregation ---

    @abstractmethod
    def aggregation(self) -> None:
        """At least one count/group-by over the full loaded dataset."""
        raise NotImplementedError

    # --- Mixed workload ---

    @abstractmethod
    def concurrent_workload(self, n_clients: int, rw_mix: float, duration_sec: float) -> dict:
        """Run a sustained mixed read/write workload at a given client concurrency.

        rw_mix: fraction of operations that are reads (e.g. 0.8 = 80% read / 20% write).
        Returns a dict with at least: throughput_qps, n_ops, n_errors, duration_sec.
        Each concrete adapter is responsible for its own connection pooling under
        concurrency (a single shared session is not safe to hit from N threads).
        """
        raise NotImplementedError

    # --- Footprint ---

    @abstractmethod
    def footprint(self) -> dict:
        """Stored data size / memory / instance specs, wherever the platform exposes
        them. Return {"observable": False, "reason": "..."} for anything the platform
        hides rather than guessing — an honest gap beats a fabricated number."""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError
