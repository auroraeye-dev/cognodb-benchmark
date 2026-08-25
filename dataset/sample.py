"""Seeded sampling of soc-Pokec down to a size that fits every platform's free tier.

Why BFS rather than uniform-random edge sampling: uniform random edge selection from
a 30M-edge graph produces a nearly edgeless scatter of disconnected fragments — every
2-hop/3-hop traversal query would return ~0 results, which defeats the entire point of
measuring traversal latency. A BFS ball around a single seed node keeps the sample one
connected neighborhood, which is what makes multi-hop traversal numbers meaningful.

Why an INDUCED subgraph specifically (this is the important part). The obvious
implementation — BFS outward, accumulating edges until you hit an edge budget — is
subtly broken for benchmarking, and we shipped and then caught exactly that bug:

    nodes: 108195 | edges: 200000 | avg out-degree: 1.85
    start nodes with ZERO out-edges: 48 / 50

Breadth-first expansion means the overwhelming majority of *discovered* nodes are on
the outer frontier: they were reached, so they're in the node set, but the BFS stopped
before expanding them, so none of their own out-edges made it into the sample. Sampling
start nodes uniformly from that set draws sinks ~96% of the time, and every traversal
query would be timing an empty result set. The numbers would look perfectly clean and
mean nothing. It also crushed density to 1.85 avg out-degree against real Pokec's
~18.75, which would make deep traversals look artificially cheap on every engine.

So instead: BFS to collect a NODE set, then keep every edge whose *both* endpoints are
in that set (the induced subgraph). Interior nodes keep their real neighborhoods and
degree distribution survives. Measured result at a 16k node budget: ~196k edges,
avg out-degree ~12.3 — still below the true ~18.75, because an induced subgraph
necessarily truncates edges pointing out of the ball, and that residual bias is
disclosed rather than hidden.

Consequence for sizing: with an induced subgraph you choose a node budget and *observe*
the edge count; you can't dial edges directly. The node budget is tuned so the resulting
edge count lands mid-band of our 150k-250k target.

This is a real methodology choice with a real bias (it favors one dense region of the
graph over a representative global sample) and is disclosed as such in the README.

Reproducibility: BENCH_RANDOM_SEED (default 42, from .env) fixes both the seed
node choice and every tie-breaking random draw during the BFS, so re-running this
script produces byte-identical output.

Usage: python -m dataset.sample
Reads dataset/raw/*.txt (see download.py), writes dataset/sample/nodes.csv and
dataset/sample/edges.csv — the frozen, git-committed benchmark input every
adapter loads identically.
"""
import csv
import os
import random
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

RAW_DIR = Path(__file__).parent / "raw"
SAMPLE_DIR = Path(__file__).parent / "sample"

EDGES_RAW = RAW_DIR / "soc-pokec-relationships.txt"
PROFILES_RAW = RAW_DIR / "soc-pokec-profiles.txt"

# We choose a NODE budget and observe the resulting induced edge count (see module
# docstring — induced subgraphs don't let you dial edges directly).
#
# The assignment suggests a 100k-500k relationship sweet spot, but the BINDING
# constraint is tighter: Neo4j Aura Free hard-caps at 200k nodes / 400k relationships,
# and the benchmark is only valid if the *identical* dataset fits every platform. So we
# size to the smallest ceiling in the fleet, not to the assignment band.
#
# 16k nodes was picked by measuring induced edge counts at several budgets against this
# specific graph and seed (15k->183,847; 25k->339,605; 40k->593,966). 16k lands ~196k
# edges: mid-band of our 150k-250k target, ~12x under Aura's node cap and ~2x under its
# relationship cap. That headroom is deliberate — it absorbs engine-to-engine variance
# in what counts against a quota without any platform silently truncating the load.
TARGET_NODE_COUNT = 16_000

# Hard safety rail: if a re-tune ever pushes the induced edge count past this, fail loudly
# rather than silently shipping a dataset Aura will truncate mid-load.
MAX_EDGES_ANY_PLATFORM = 400_000

# AGE is column index 7 (0-based) in soc-pokec-profiles.txt; see SNAP's documented
# schema (user_id, public, completion_percentage, gender, region, last_login,
# registration, AGE, ...). Pulled in as the property used for indexed_lookup and
# aggregation, since it's a low-cardinality, near-always-populated numeric field.
AGE_COLUMN_INDEX = 7


@dataclass
class Dataset:
    nodes: list  # list of {"pid": int, "age": int|None}
    edges: list  # list of (src_pid, dst_pid)
    seed: int
    method: str = "bfs_induced_subgraph"

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    def to_csv(self, out_dir: Path = SAMPLE_DIR) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / "nodes.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["pid", "age"])
            for n in self.nodes:
                w.writerow([n["pid"], n["age"] if n["age"] is not None else ""])
        with open(out_dir / "edges.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["src", "dst"])
            for s, d in self.edges:
                w.writerow([s, d])
        with open(out_dir / "metadata.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["key", "value"])
            w.writerow(["seed", self.seed])
            w.writerow(["method", self.method])
            w.writerow(["node_count", self.node_count])
            w.writerow(["edge_count", self.edge_count])

    @classmethod
    def from_csv(cls, in_dir: Path = SAMPLE_DIR) -> "Dataset":
        nodes = []
        with open(in_dir / "nodes.csv") as f:
            for row in csv.DictReader(f):
                nodes.append({
                    "pid": int(row["pid"]),
                    "age": int(row["age"]) if row["age"] else None,
                })
        edges = []
        with open(in_dir / "edges.csv") as f:
            for row in csv.DictReader(f):
                edges.append((int(row["src"]), int(row["dst"])))
        seed, method = 42, "bfs_induced_subgraph"
        meta_path = in_dir / "metadata.csv"
        if meta_path.exists():
            with open(meta_path) as f:
                meta = {row["key"]: row["value"] for row in csv.DictReader(f)}
            seed = int(meta.get("seed", seed))
            method = meta.get("method", method)
        return cls(nodes=nodes, edges=edges, seed=seed, method=method)


def _build_out_adjacency(edges_path: Path) -> dict:
    """One streaming pass over the raw edge list into an out-adjacency dict.
    Out-adjacency only (not both directions materialized) to keep memory in
    check on the full ~30.6M-edge graph — Pokec's directed "friend" edges are
    heavily reciprocated in practice, so BFS over out-edges alone still reaches
    a well-connected neighborhood."""
    adj = {}
    with open(edges_path) as f:
        for line in f:
            parts = line.split()
            if len(parts) != 2:
                continue
            src, dst = int(parts[0]), int(parts[1])
            adj.setdefault(src, []).append(dst)
    return adj


def _bfs_node_set(adj: dict, seed_node: int, target_nodes: int, rng: random.Random) -> set:
    """BFS out from seed_node until we've collected target_nodes nodes.

    Collects nodes ONLY — edges are induced afterwards by _induced_edges. Neighbor
    order is shuffled per-node (with the seeded RNG) so the sample isn't an artifact
    of the raw file's row order."""
    visited = {seed_node}
    queue = deque([seed_node])

    while queue and len(visited) < target_nodes:
        node = queue.popleft()
        neighbors = list(adj.get(node, []))
        rng.shuffle(neighbors)
        for nbr in neighbors:
            if nbr not in visited:
                visited.add(nbr)
                queue.append(nbr)
                if len(visited) >= target_nodes:
                    break

    return visited


def _induced_edges(adj: dict, node_set: set) -> list:
    """Every edge whose BOTH endpoints are in node_set — the induced subgraph.

    This is what stops the outer BFS frontier from being all sinks: interior nodes
    keep their genuine neighborhoods instead of having their out-edges truncated by
    wherever the BFS happened to stop. Output is sorted for deterministic CSV bytes
    across runs (dict/set iteration order is stable within a run but sorting makes
    the committed dataset diffable and provably reproducible)."""
    edges = []
    for src in node_set:
        for dst in adj.get(src, ()):
            if dst in node_set:
                edges.append((src, dst))
    edges.sort()
    return edges


def _load_ages(profiles_path: Path, wanted_pids: set) -> dict:
    ages = {}
    with open(profiles_path, encoding="latin-1") as f:
        for line in f:
            cols = line.rstrip("\n").split("\t")
            try:
                pid = int(cols[0])
            except (ValueError, IndexError):
                continue
            if pid not in wanted_pids:
                continue
            age = None
            if len(cols) > AGE_COLUMN_INDEX:
                raw = cols[AGE_COLUMN_INDEX].strip()
                if raw.isdigit() and raw != "0":  # Pokec uses 0 for "unknown"
                    age = int(raw)
            ages[pid] = age
    return ages


def build_sample(seed: int = None, target_nodes: int = TARGET_NODE_COUNT) -> Dataset:
    seed = seed if seed is not None else int(os.environ.get("BENCH_RANDOM_SEED", 42))
    rng = random.Random(seed)

    print("[sample] building out-adjacency from raw edge list (one pass)...")
    adj = _build_out_adjacency(EDGES_RAW)

    # Restrict candidate seed nodes to ones with a nontrivial out-degree, so the
    # BFS doesn't stall immediately on a leaf node.
    candidates = [n for n, nbrs in adj.items() if len(nbrs) >= 20]
    seed_node = rng.choice(candidates)
    print(f"[sample] seed node = {seed_node} (BENCH_RANDOM_SEED={seed})")

    node_ids = _bfs_node_set(adj, seed_node, target_nodes, rng)
    print(f"[sample] BFS collected {len(node_ids)} nodes; inducing edges...")
    edges = _induced_edges(adj, node_ids)
    print(f"[sample] induced {len(edges)} edges (avg out-degree {len(edges)/len(node_ids):.2f})")

    if len(edges) > MAX_EDGES_ANY_PLATFORM:
        raise SystemExit(
            f"[sample] ABORT: {len(edges)} edges exceeds the {MAX_EDGES_ANY_PLATFORM} "
            "relationship cap of the most-constrained platform (Neo4j Aura Free). "
            "Lower TARGET_NODE_COUNT — shipping this would let Aura truncate mid-load "
            "and silently invalidate every cross-DB comparison."
        )

    ages = _load_ages(PROFILES_RAW, node_ids)
    nodes = [{"pid": pid, "age": ages.get(pid)} for pid in sorted(node_ids)]

    return Dataset(nodes=nodes, edges=edges, seed=seed, method="bfs_induced_subgraph")


if __name__ == "__main__":
    ds = build_sample()
    ds.to_csv()
    print(f"[sample] wrote {ds.node_count} nodes, {ds.edge_count} edges to {SAMPLE_DIR}/")
