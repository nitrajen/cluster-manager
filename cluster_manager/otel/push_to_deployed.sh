#!/usr/bin/env bash
#
# Push real laptop metrics to the deployed Databricks App.
# Generates a fresh OAuth token and runs the OTel Collector.
#
# Usage: ./cluster_manager/otel/push_to_deployed.sh
#
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PROFILE="${DATABRICKS_PROFILE:-FEVM_SERVERLESS_STABLE}"
APP_URL="https://cluster-manager-7474645572615955.aws.databricksapps.com"

echo "Generating OAuth token (profile: $PROFILE)..."
TOKEN=$(databricks auth token --profile "$PROFILE" -o json | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

if [ -z "$TOKEN" ]; then
    echo "ERROR: Failed to get token. Run: databricks auth login --profile $PROFILE"
    exit 1
fi

echo "Token acquired (${#TOKEN} chars)"
echo "Pushing metrics to: $APP_URL/api/otel/v1/metrics"
echo "Press Ctrl+C to stop"
echo ""

# Generate config with token inline (OTel Collector doesn't support env vars)
RUNTIME_CONFIG=$(mktemp /tmp/otel_deployed_XXXX.yaml)
cat > "$RUNTIME_CONFIG" << EOF
receivers:
  hostmetrics:
    collection_interval: 15s
    scrapers:
      cpu:
        metrics:
          system.cpu.utilization:
            enabled: true
      memory:
        metrics:
          system.memory.utilization:
            enabled: true
          system.memory.usage:
            enabled: true
      disk:
        metrics:
          system.disk.io_time:
            enabled: true
          system.disk.operations:
            enabled: true
      filesystem:
        metrics:
          system.filesystem.utilization:
            enabled: true
          system.filesystem.inodes.usage:
            enabled: true
      network:
        metrics:
          system.network.errors:
            enabled: true
          system.network.dropped:
            enabled: true
      load:
      paging:
        metrics:
          system.paging.operations:
            enabled: true
      processes:

processors:
  batch:
    timeout: 10s
    send_batch_size: 8

  resource:
    attributes:
      - key: cluster_id
        value: "local-laptop-001"
        action: upsert
      - key: instance_id
        value: "macbook-laurent"
        action: upsert
      - key: is_driver
        value: "true"
        action: upsert
      - key: node_type
        value: "macbook-pro-m1"
        action: upsert

exporters:
  otlphttp:
    endpoint: ${APP_URL}/api/otel
    encoding: json
    headers:
      Authorization: "Bearer ${TOKEN}"

service:
  pipelines:
    metrics:
      receivers: [hostmetrics]
      processors: [resource, batch]
      exporters: [otlphttp]
EOF

cleanup() {
    rm -f "$RUNTIME_CONFIG"
}
trap cleanup EXIT INT TERM

"$PROJECT_DIR/cluster_manager/otel/otelcol-contrib" --config "$RUNTIME_CONFIG"
