#!/bin/sh

set -e

cd /app

# Runtime configuration from environment variables.
PORT="${PORT:-8000}"
INTERVAL="${UPDATE_INTERVAL:-3600}"

# Start the HTTP server first so an existing podcast feed remains
# available while updates are being processed in the background.
echo "[blogcast] Starting HTTP service on port $PORT..."

python /app/blogcast.py serve --port "$PORT" &
SERVE_PID=$!

# Run an immediate update, then continue updating on a fixed schedule.
echo "[blogcast] Running initial update..."

(
  python /app/blogcast.py update || echo "[blogcast] update failed (continuing)."

  while true; do
    sleep "$INTERVAL"
    echo "[blogcast] Scheduled update..."
    python /app/blogcast.py update || echo "[blogcast] update failed (continuing)."
  done
) &

# Keep the container alive as long as the HTTP server is running.
wait $SERVE_PID