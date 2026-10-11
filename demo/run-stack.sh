#!/usr/bin/env bash
# End-to-end demo through n8n: GitLab push (simulated) → n8n → engine → model "MR" → approve link → Confluence (mock).
# Requires: docker compose up -d --build && ./n8n/import.sh
set -euo pipefail
ENGINE=${ENGINE:-http://localhost:8088}
N8N=${N8N:-http://localhost:5678}
PY=${PY:-.venv/bin/python}

step() { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }
wait_status() {  # wait_status <status> <count>
  for i in $(seq 1 30); do
    n=$(curl -s "$ENGINE/api/v1/runs?status=$1" | $PY -c 'import sys,json; print(len(json.load(sys.stdin)))')
    [ "$n" -ge "$2" ] && return 0; sleep 1
  done; echo "timeout waiting for $1"; exit 1
}

step "0. Clean data, sample repositories, pages in mock Confluence"
docker compose exec -T docsync docsync demo reset >/dev/null
docker compose exec -T docsync docsync demo seed >/dev/null
curl -s -X POST "$ENGINE/api/v1/confluence/bootstrap" | $PY -c 'import sys,json; [print("  ", p["status"], p["url"]) for p in json.load(sys.stdin)]'

step "1. Developer merged to main: Orders → Billing via gRPC (simulated GitLab Push Hook → n8n)"
DOCSYNC_DATA_DIR=./data $PY -m docsync.cli demo push --step grpc --n8n-url "$N8N/webhook/docsync/gitlab" | grep '"status"'
wait_status PROPOSED 1
RUN=$(curl -s "$ENGINE/api/v1/runs?status=PROPOSED" | $PY -c 'import sys,json; print(json.load(sys.stdin)[0]["run_id"])')
curl -s "$ENGINE/api/v1/runs/$RUN" | $PY -c '
import sys,json; r=json.load(sys.stdin)
print("   run:", r["run_id"], r["status"], "| model branch:", r["branch"])
for e in r["changeset"]["events"]: print("   event:", e["type"], e["from"], "→", e.get("to"), e.get("protocol"), e.get("operations"))
print("   affected pages:", [p["title"] for p in r["preview"]["pages"]])
print("   approve link sent to the analyst:", r["preview"]["approveUrl"])'

step "2. Analyst approved (click on the link from the notification → n8n → merge + publish + audit)"
curl -s "$N8N/webhook/docsync/approve?run=$RUN" | $PY -c 'import sys,json; d=json.load(sys.stdin); print(d.get("text", d))'

step "3. Second merge: Orders publishes orders.created to Kafka"
DOCSYNC_DATA_DIR=./data $PY -m docsync.cli demo push --step kafka --n8n-url "$N8N/webhook/docsync/gitlab" | grep '"status"'
wait_status PROPOSED 1
RUN2=$(curl -s "$ENGINE/api/v1/runs?status=PROPOSED" | $PY -c 'import sys,json; print(json.load(sys.stdin)[0]["run_id"])')
curl -s "$N8N/webhook/docsync/approve?run=$RUN2" | $PY -c 'import sys,json; d=json.load(sys.stdin); print(d.get("text", d))'

step "Summary"
curl -s "$ENGINE/api/v1/runs" | $PY -c 'import sys,json; [print("  ", r["status"], r["service_id"], r["merge_sha"][:8], r["published_at"]) for r in json.load(sys.stdin)]'
echo "   notifications: $ENGINE/api/v1/notifications"
echo "   pages:         $ENGINE/mock-confluence/"
echo "   n8n:           $N8N  (executions)"
