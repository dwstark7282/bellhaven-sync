#!/usr/bin/env bash
# Daily proposal run. Reads the website + CRM, queues proposals locally; never writes to the CRM.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a
mkdir -p logs
.venv/bin/python -m bellhaven_sync.pipeline >> "logs/pipeline-$(date +%F).log" 2>&1
