#!/usr/bin/env bash
set -euo pipefail
export PORT="${PORT:-5000}"
exec gunicorn -w 2 -b 127.0.0.1:${PORT} app:app
