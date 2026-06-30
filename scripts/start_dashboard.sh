#!/usr/bin/env bash
# Start the biller dashboard. Run from repo root.
set -euo pipefail
cd "$(dirname "$0")/.."

# Stop any previous dashboard instance
pkill -f "dashboard.app" 2>/dev/null || true
sleep 1

echo "Starting dashboard at http://127.0.0.1:8050"
exec .venv/bin/python -m dashboard.app
