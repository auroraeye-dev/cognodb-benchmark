#!/usr/bin/env bash
# Provision a GCP VM in us-east4 and run the benchmark from inside CognoDB's region.
#
# WHY THIS EXISTS
# The laptop-client results have a 302 ms round trip to CognoDB. That floor is larger than
# every engine time we measure, so seven cells in the results matrix are upper bounds
# ("unresolvable") rather than measurements. Running the client in us-east4 collapses RTT
# to single-digit milliseconds and turns those bounds into real numbers.
#
# WHAT IT PROTECTS
# Results are written to results/client-us-east4/ via BENCH_RESULTS_SUBDIR, so the existing
# laptop numbers are never overwritten. If this run fails or is abandoned, the tagged
# submission (git tag v1-submission) is untouched and remains submittable.
#
# PREREQUISITES (one-time, interactive — cannot be automated):
#   brew install --cask google-cloud-sdk
#   gcloud auth login
#   gcloud config set project YOUR_PROJECT_ID
#
# USAGE
#   ./scripts/setup_vm.sh create     # provision + bootstrap the VM
#   ./scripts/setup_vm.sh run        # run the benchmark on it, fetch results back
#   ./scripts/setup_vm.sh delete     # TEAR IT DOWN (do this — it bills hourly)
set -euo pipefail

VM_NAME="${VM_NAME:-cognodb-bench}"
ZONE="${ZONE:-us-east4-a}"          # same region as CognoDB Cloud (GCP us-east4)
MACHINE="${MACHINE:-e2-standard-2}" # 2 vCPU / 8 GB: enough to host the 3 Docker engines
                                    # AND run the client without the client itself
                                    # becoming the bottleneck. The DB containers are
                                    # still capped to 0.5 CPU / 256 MB by compose, so
                                    # engine parity with the laptop run is preserved.
REPO="${REPO:-https://github.com/auroraeye-dev/cognodb-benchmark.git}"
SUBDIR="client-us-east4"

need_gcloud() {
  command -v gcloud >/dev/null 2>&1 || {
    echo "ERROR: gcloud not installed. Run:  brew install --cask google-cloud-sdk" >&2
    exit 1
  }
  gcloud auth list --filter=status:ACTIVE --format='value(account)' | grep -q . || {
    echo "ERROR: not authenticated. Run:  gcloud auth login" >&2
    exit 1
  }
}

case "${1:-}" in
create)
  need_gcloud
  echo "==> Creating $VM_NAME ($MACHINE) in $ZONE"
  gcloud compute instances create "$VM_NAME" \
    --zone="$ZONE" --machine-type="$MACHINE" \
    --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
    --boot-disk-size=50GB

  echo "==> Waiting for SSH"
  until gcloud compute ssh "$VM_NAME" --zone="$ZONE" --command="true" 2>/dev/null; do sleep 5; done

  echo "==> Bootstrapping (docker, python3.12, repo)"
  gcloud compute ssh "$VM_NAME" --zone="$ZONE" --command='
    set -eux
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker.io docker-compose-v2 python3.12 python3.12-venv git
    sudo usermod -aG docker "$USER"
  '
  gcloud compute ssh "$VM_NAME" --zone="$ZONE" --command="git clone -q $REPO ~/bench || true"

  echo
  echo "==> VM ready. Now copy your .env up (it is gitignored, so it is NOT in the clone):"
  echo "    gcloud compute scp .env $VM_NAME:~/bench/.env --zone=$ZONE"
  echo "    ./scripts/setup_vm.sh run"
  ;;

run)
  need_gcloud
  echo "==> Confirming RTT from inside the region (this is the whole point)"
  gcloud compute ssh "$VM_NAME" --zone="$ZONE" --command='
    cd ~/bench
    python3.12 -m venv venv 2>/dev/null || true
    ./venv/bin/pip install -q -r requirements.txt
    ./venv/bin/python -m scripts.verify_connection
  '
  echo "==> Running full suite -> results/'"$SUBDIR"'/"
  gcloud compute ssh "$VM_NAME" --zone="$ZONE" --command="
    set -eux
    cd ~/bench
    export BENCH_RESULTS_SUBDIR=$SUBDIR
    python3 -m dataset.download && ./venv/bin/python -m dataset.sample
    docker compose -f docker/docker-compose.yml up -d
    sleep 20
    for db in cognodb neo4j_aura memgraph falkordb; do
      ./venv/bin/python -m harness.runner --db \$db --load-only || true
      ./venv/bin/python -m harness.runner --db \$db --no-load  || true
    done
    ./venv/bin/python report.py
    ./venv/bin/python -m charts.generate
  "
  echo "==> Fetching results back (laptop results untouched)"
  mkdir -p "results/$SUBDIR"
  gcloud compute scp --recurse "$VM_NAME:~/bench/results/$SUBDIR/*" "results/$SUBDIR/" --zone="$ZONE"
  echo "==> Done. Compare: results/*.json (laptop) vs results/$SUBDIR/*.json (in-region)"
  ;;

delete)
  need_gcloud
  echo "==> Deleting $VM_NAME — it bills hourly while it exists"
  gcloud compute instances delete "$VM_NAME" --zone="$ZONE" --quiet
  ;;

*)
  sed -n '1,20p' "$0"; exit 1 ;;
esac
