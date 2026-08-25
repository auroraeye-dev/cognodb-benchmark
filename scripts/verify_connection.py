"""Build-order step 1: prove both managed DBs are reachable with a trivial Cypher
query before building anything else. Run this first, on a fresh checkout, to
sanity-check .env before touching adapters/harness code.

Usage: python -m scripts.verify_connection
"""
import os
import sys

from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()


def check(label: str, prefix: str) -> bool:
    """Verify one Bolt DB using the <PREFIX>_URI/_USER/_PASSWORD/_DATABASE env vars.

    _DATABASE is optional: unset means "use the server's default". It matters because
    username and database are per-deployment, not universal — some Aura instances
    authenticate as the instance id and expose the database under that id rather than
    the stock neo4j/neo4j."""
    uri = os.environ.get(f"{prefix}_URI")
    user = os.environ.get(f"{prefix}_USER")
    password = os.environ.get(f"{prefix}_PASSWORD")
    database = os.environ.get(f"{prefix}_DATABASE") or None
    if not uri:
        print(f"[{label}] SKIPPED — {prefix}_URI not set in .env")
        return True
    # Echo the non-secret identity we're actually connecting with, so a
    # wrong-user/wrong-database misconfiguration is visible at a glance.
    print(f"[{label}] connecting as user={user!r} database={database or '<server default>'}")
    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        driver.verify_connectivity()
        with driver.session(database=database) as session:
            value = session.run("RETURN 1 AS x").single()["x"]
        driver.close()
        ok = value == 1
        print(f"[{label}] {'OK' if ok else 'UNEXPECTED RESPONSE'} — RETURN 1 -> {value}")
        return ok
    except Exception as exc:
        print(f"[{label}] FAILED — {exc}")
        return False


if __name__ == "__main__":
    results = [
        check("CognoDB Cloud", "COGNODB"),
        check("Neo4j Aura Free", "NEO4J_AURA"),
    ]
    sys.exit(0 if all(results) else 1)
