#!/usr/bin/env bash
# Start fake Kavita + RetroShelf, run the real-HTTP smoke driver, tear down.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
$PY tools/fake_kavita.py 5599 >/tmp/fake_kavita.log 2>&1 &
FAKE=$!
sleep 1
KAVITA_OPDS_URL="http://127.0.0.1:5599/api/opds/KEY" BRIDGE_ID_SECRET="smoke" \
  $PY -m uvicorn app.main:app --host 127.0.0.1 --port 8099 >/tmp/uvicorn.log 2>&1 &
UV=$!
sleep 3
PYTHONPATH=. $PY tools/smoke.py http://127.0.0.1:8099
rc=$?
kill $FAKE $UV 2>/dev/null
wait $FAKE $UV 2>/dev/null
if [ $rc -ne 0 ]; then echo "--- uvicorn.log tail ---"; tail -20 /tmp/uvicorn.log; fi
exit $rc
