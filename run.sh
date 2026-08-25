#!/usr/bin/env bash
# Single-command entrypoint: dataset -> self-hosted DBs up -> full benchmark suite
# -> results JSON -> charts -> markdown tables. Requires .env populated (see
# .env.example) with CognoDB + Neo4j Aura credentials at minimum.
#
# Usage:
#   ./run.sh                 # everything, all 5 DBs
#   ./run.sh --db cognodb    # just one DB (useful while developing an adapter)
#   ./run.sh --skip-dataset  # reuse dataset/sample/*.csv already on disk
#   ./run.sh --skip-docker   # self-hosted containers already running
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

DB="all"
SKIP_DATASET=0
SKIP_DOCKER=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --db) DB="$2"; shift 2 ;;
    --skip-dataset) SKIP_DATASET=1; shift ;;
    --skip-docker) SKIP_DOCKER=1; shift ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

if [[ ! -f .env ]]; then
  echo "Missing .env — copy .env.example to .env and fill in credentials first." >&2
  exit 1
fi

if [[ ! -d venv ]]; then
  echo "[setup] creating venv..."
  python3 -m venv venv
fi
source venv/bin/activate
pip install -q -r requirements.txt

if [[ "$SKIP_DATASET" -eq 0 && ! -f dataset/sample/edges.csv ]]; then
  echo "[dataset] downloading raw SNAP soc-Pokec..."
  python -m dataset.download
  echo "[dataset] sampling (seeded)..."
  python -m dataset.sample
fi

if [[ "$SKIP_DOCKER" -eq 0 && "$DB" != "cognodb" && "$DB" != "neo4j_aura" ]]; then
  echo "[docker] starting self-hosted DBs..."
  docker compose -f docker/docker-compose.yml up -d
  echo "[docker] waiting for containers to become healthy..."
  sleep 15
fi

echo "[harness] running benchmark suite (db=$DB)..."
python -m harness.runner --db "$DB"

echo "[report] generating markdown tables..."
python report.py

echo "[charts] generating charts..."
python -m charts.generate

echo "Done. See results/RESULTS.md and charts/output/*.png"
