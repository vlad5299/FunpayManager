#!/usr/bin/env sh
set -eu

echo "Starting Xray..."
/usr/local/bin/xray run -c /app/xray_config.json &
XRAY_PID=$!

sleep 2
if ! kill -0 "$XRAY_PID" 2>/dev/null; then
  echo "CRITICAL: Xray exited immediately!" >&2
  wait "$XRAY_PID" || true
  exit 1
fi

echo "Xray is running (PID $XRAY_PID), starting bot_5.py..."
python -u bot_5.py &
BOT_PID=$!

# Если любой из процессов упадёт — гасим контейнер, чтобы Railway перезапустил его целиком
while kill -0 "$XRAY_PID" 2>/dev/null && kill -0 "$BOT_PID" 2>/dev/null; do
  sleep 3
done

echo "Process exited (Xray or bot_5.py), stopping container..." >&2
kill "$XRAY_PID" "$BOT_PID" 2>/dev/null || true
exit 1
