#!/usr/bin/env sh
set -u

/usr/local/bin/xray run -c /app/xray_config.json > /tmp/xray.log 2>&1 &
XRAY_PID=$!

sleep 2
if ! kill -0 "$XRAY_PID" 2>/dev/null; then
  echo "XRAY FAILED TO START. Log:" >&2
  cat /tmp/xray.log >&2 || true
  exit 1
fi

exec python -u bot.py
