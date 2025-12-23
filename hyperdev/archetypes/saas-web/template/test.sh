#!/usr/bin/env bash
set -euo pipefail

echo "===> 1) Is the app listening on PORT?"
PORT="${PORT:-${APP_PORT:-5000}}"
ss -lntp | grep -q ":${PORT} " && echo "✅ listening on ${PORT}" || (echo "❌ not listening on ${PORT}" && exit 1)

echo "===> 2) Direct health check (bypass nginx)"
curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null && echo "✅ direct /health OK" || (echo "❌ direct /health failed" && exit 1)

echo "===> 3) Nginx syntax check"
sudo nginx -t >/dev/null && echo "✅ nginx config OK" || (echo "❌ nginx config fail" && exit 1)

echo "===> Done ✅"
