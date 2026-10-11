#!/usr/bin/env bash
# Import and activate the DocSync workflows in the n8n container (docker compose service `n8n`).
set -euo pipefail
C=${N8N_CONTAINER:-docsync-n8n}
docker exec -u node "$C" n8n import:workflow --separate --input=/workflows
for id in docsync-wf-01 docsync-wf-02 docsync-wf-03 docsync-wf-99; do
  docker exec -u node "$C" n8n publish:workflow --id="$id" 2>/dev/null \
    || docker exec -u node "$C" n8n update:workflow --id="$id" --active=true
done
docker restart "$C" >/dev/null   # activation takes effect after a restart
echo "n8n: workflows imported and activated (container restarted). UI: http://localhost:5678"
