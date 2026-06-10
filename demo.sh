#!/usr/bin/env bash
#
# Start the OTel live metrics demo locally.
# Launches the collector (FastAPI server) and mock producer side by side.
#
# Usage:
#   ./demo.sh              # 2 clusters, 3 workers each
#   ./demo.sh 5 8          # 5 clusters, 8 workers each
#
# Ctrl+C to stop both processes.

set -e

CLUSTERS=${1:-2}
WORKERS=${2:-3}
INTERVAL=10
PORT=8000
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$SCRIPT_DIR"

# Activate venv
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
elif [ -f venv/bin/activate ]; then
    source venv/bin/activate
fi

# Check dependencies
python -c "import psycopg2, fastapi" 2>/dev/null || {
    echo "Missing dependencies. Run: pip install -e '.[dev]'"
    exit 1
}

# Kill any existing server on the port
lsof -ti tcp:$PORT 2>/dev/null | xargs kill 2>/dev/null || true

echo "╔══════════════════════════════════════════════════════╗"
echo "║        OTel Live Metrics Demo                       ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  Collector:  http://localhost:$PORT                   ║"
echo "║  Dashboard:  http://localhost:$PORT/live-metrics       ║"
echo "║  Clusters:   $CLUSTERS (${WORKERS} workers each)                  ║"
echo "║  Interval:   ${INTERVAL}s                                   ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# Start collector in background
echo "[1/2] Starting collector..."
uvicorn cluster_manager.backend.app:app --host 0.0.0.0 --port $PORT --log-level warning &
SERVER_PID=$!

# Wait for server to be ready
for i in $(seq 1 30); do
    if curl -s -o /dev/null http://localhost:$PORT/api/live-metrics/active 2>/dev/null; then
        break
    fi
    sleep 1
done

if ! kill -0 $SERVER_PID 2>/dev/null; then
    echo "ERROR: Server failed to start. Check .env file and Lakebase connection."
    exit 1
fi

echo "       Collector ready (PID $SERVER_PID)"
echo ""

# Start mock producer in foreground
echo "[2/2] Starting mock producer..."
echo "      Ctrl+C to stop"
echo ""

cleanup() {
    echo ""
    echo "Stopping..."
    kill $SERVER_PID 2>/dev/null
    wait $SERVER_PID 2>/dev/null
    echo "Done."
}
trap cleanup EXIT INT TERM

python cluster_manager/otel/mock_producer.py \
    --endpoint http://localhost:$PORT \
    --clusters $CLUSTERS \
    --workers $WORKERS \
    --interval $INTERVAL
