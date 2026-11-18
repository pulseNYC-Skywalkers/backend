#!/usr/bin/env sh
# Build data/wound_care.duckdb from the bundled mock API (no network, no API key).
# Used at Docker image build time so the deployed dashboard has data.
set -eu
cd "$(dirname "$0")/.."
PY="${PYTHON:-python}"

export EHR_API_BASE_URL=http://127.0.0.1:8099
export MOCK_THROTTLE_RATE=0 MOCK_OUTAGE_RATE=0   # fast, clean build

"$PY" -m mock_api.server > /tmp/mock_api.log 2>&1 &
MOCK_PID=$!
trap 'kill $MOCK_PID 2>/dev/null || true' EXIT

i=0
until "$PY" -c "import httpx; httpx.get('http://127.0.0.1:8099/health').raise_for_status()" 2>/dev/null; do
  i=$((i+1)); [ "$i" -gt 30 ] && { echo "mock API did not start"; cat /tmp/mock_api.log; exit 1; }
  sleep 1
done

"$PY" -m orchestration.runner --reset
"$PY" -m pipeline.extract.extractor --force
"$PY" -m pipeline.resolve.resolver
"$PY" -m pipeline.decide.rules
