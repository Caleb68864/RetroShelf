#!/usr/bin/env bash
# Start RetroShelf against the live ManyBooks OPDS feed and run the Playwright E2E.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
KAVITA_OPDS_URL="https://manybooks.net/opds" \
KAVITA_BASE_URL="https://manybooks.net" \
BRIDGE_ID_SECRET="e2e" \
SHOW_COVERS="true" \
  $PY -m uvicorn app.main:app --host 127.0.0.1 --port 8099 >/tmp/uvicorn_e2e.log 2>&1 &
UV=$!
sleep 4
PYTHONPATH=. $PY tools/e2e_playwright.py http://127.0.0.1:8099
rc=$?
kill $UV 2>/dev/null
wait $UV 2>/dev/null
if [ $rc -ne 0 ]; then echo "--- uvicorn_e2e.log tail ---"; tail -30 /tmp/uvicorn_e2e.log; fi
exit $rc
