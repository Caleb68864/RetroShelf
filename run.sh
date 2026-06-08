#!/usr/bin/env bash
# RetroShelf launcher (macOS/Linux). Sets up a venv, installs deps, and runs
# the server for local testing.
#
# Usage:
#   ./run.sh                                  # uses .env, or defaults to ManyBooks (public test feed)
#   ./run.sh "http://kavita:5000/api/opds/KEY"  # point at your Kavita OPDS URL
#   KAVITA_OPDS_URL=... ./run.sh              # or via environment / .env file
set -euo pipefail
cd "$(dirname "$0")"

# Load .env if present (KAVITA_OPDS_URL, APP_PORT, etc.).
if [ -f .env ]; then set -a; . ./.env; set +a; fi

# First positional arg overrides the OPDS URL (handy for quick testing).
if [ "${1:-}" != "" ]; then KAVITA_OPDS_URL="$1"; fi

# Default to the public ManyBooks OPDS feed so the app runs with zero config.
if [ "${KAVITA_OPDS_URL:-}" = "" ]; then
  KAVITA_OPDS_URL="https://manybooks.net/opds"
  echo "No KAVITA_OPDS_URL set — defaulting to the public ManyBooks feed for testing."
  echo "Point at your own library with:  ./run.sh \"http://kavita:5000/api/opds/YOUR_KEY\""
fi
export KAVITA_OPDS_URL
export APP_PORT="${APP_PORT:-8099}"

# Create the virtualenv on first run.
PY="python3"
command -v "$PY" >/dev/null 2>&1 || PY="python"
if [ ! -x ".venv/bin/python" ]; then
  echo "Creating virtualenv (.venv)…"
  "$PY" -m venv .venv
fi
echo "Installing dependencies…"
.venv/bin/python -m pip install -q --upgrade pip >/dev/null
.venv/bin/python -m pip install -q -r requirements.txt

echo
echo "RetroShelf starting on  http://0.0.0.0:${APP_PORT}"
echo "Open it from an iPad at http://<this-computer-ip>:${APP_PORT}"
echo
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "${APP_PORT}"
