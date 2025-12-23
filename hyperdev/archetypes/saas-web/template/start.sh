#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

# Load .env if present
if [ -f ".env" ]; then
  set -a
  . ./.env
  set +a
fi

PORT="${PORT:-${APP_PORT:-5000}}"

# Create venv if missing (safe for first boot)
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

. ./.venv/bin/activate
pip -q install --upgrade pip >/dev/null
pip -q install -r requirements.txt >/dev/null

exec gunicorn -w 2 -b 127.0.0.1:${PORT} app:app
