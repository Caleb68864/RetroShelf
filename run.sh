#!/usr/bin/env bash
# RetroShelf launcher (macOS/Linux). Sets up a venv, installs deps, and runs
# the server for local testing.
#
# Usage:
#   ./run.sh                                  # uses .env, or defaults to ManyBooks (public test feed)
#   ./run.sh "http://kavita:5000/api/opds/KEY"  # point at your Kavita OPDS URL
#   KAVITA_OPDS_URL=... ./run.sh              # or via environment / .env file
#
# By default this also opens the firewall port (firewalld/ufw) so other devices
# on your LAN can reach RetroShelf — you may be prompted for your sudo password.
# Set RS_OPEN_FIREWALL=0 to skip that (e.g. if you manage the firewall yourself).
set -euo pipefail
cd "$(dirname "$0")"

# Best-effort: open the listen port in the host firewall for LAN access. Never
# fatal — if it can't, the app still starts and we print how to do it manually.
open_firewall_port() {
  local port="$1"
  [ "${RS_OPEN_FIREWALL:-1}" = "0" ] && return 0
  command -v sudo >/dev/null 2>&1 || return 0

  local has_systemctl=0
  command -v systemctl >/dev/null 2>&1 && has_systemctl=1

  # Detect the active firewall. Prefer `systemctl is-active` (reliable, no
  # D-Bus/polkit round-trip); fall back to `firewall-cmd --state` for firewalld.
  local tool=""
  local -a add=()
  if command -v firewall-cmd >/dev/null 2>&1 \
     && { { [ "$has_systemctl" = 1 ] && systemctl is-active --quiet firewalld; } \
          || firewall-cmd --state >/dev/null 2>&1; }; then
    tool="firewalld"; add=(firewall-cmd "--add-port=${port}/tcp")
  elif command -v ufw >/dev/null 2>&1 && [ "$has_systemctl" = 1 ] \
       && systemctl is-active --quiet ufw; then
    tool="ufw"; add=(ufw allow "${port}/tcp")
  else
    return 0   # no recognised active firewall — nothing to do
  fi

  echo "Ensuring ${tool} allows ${port}/tcp for LAN access…"
  if sudo -n "${add[@]}" >/dev/null 2>&1; then
    return 0   # passwordless sudo — silent success
  fi
  echo "  (you may be prompted for your password; set RS_OPEN_FIREWALL=0 to skip)"
  sudo "${add[@]}" >/dev/null 2>&1 \
    || echo "  ! Couldn't open ${port}/tcp automatically — open it manually if devices can't connect."
  return 0
}

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

open_firewall_port "${APP_PORT}"

echo
echo "RetroShelf starting on  http://0.0.0.0:${APP_PORT}"
echo "Open it from an iPad at http://<this-computer-ip>:${APP_PORT}"
echo
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "${APP_PORT}"
