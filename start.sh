#!/usr/bin/env sh
set -e

/usr/local/bin/xray run -c /app/xray_config.json &
XRAY_PID=$!

sleep 2
kill -0 "$XRAY_PID" 2>/dev/null || { echo "ERROR: Xray failed to start" >&2; exit 1; }

exec python -u bot_5.py
