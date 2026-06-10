#!/bin/bash
# ============================================================================
# OTel Collector Init Script for Databricks Cluster Nodes
# ============================================================================
# Deploys OpenTelemetry Collector with hostmetrics receiver on every node
# (driver + workers). Pushes CPU, memory, disk, network, load metrics to
# the cluster-manager app's OTLP/HTTP receiver endpoint.
#
# Required environment variables (set via cluster policy or spark_env_vars):
#   OTEL_ENDPOINT        - App endpoint URL (e.g. https://cluster-manager-xxx.databricksapps.com)
#   OTEL_SP_CLIENT_ID    - Service principal client ID for OAuth
#   OTEL_SP_CLIENT_SECRET - Service principal client secret
#   OTEL_TOKEN_ENDPOINT  - OAuth token URL (e.g. https://<workspace>.cloud.databricks.com/oidc/v1/token)
#
# Optional:
#   OTEL_VOLUME_PATH     - Volume path to pre-staged binary (skips download)
#   OTEL_COLLECTION_INTERVAL - Collection interval (default: 15s)
# ============================================================================

set -euo pipefail

LOG_FILE="/tmp/otel-init.log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "[OTel Init] Starting at $(date -u)"

# --- Configuration ---
OTEL_VERSION="${OTEL_VERSION:-0.104.0}"
COLLECTION_INTERVAL="${OTEL_COLLECTION_INTERVAL:-15s}"
INSTALL_DIR="/usr/local/bin"
CONFIG_DIR="/etc/otel"
TOKEN_FILE="/tmp/otel-token"

# Validate required env vars
for var in OTEL_ENDPOINT OTEL_SP_CLIENT_ID OTEL_SP_CLIENT_SECRET OTEL_TOKEN_ENDPOINT; do
    if [ -z "${!var:-}" ]; then
        echo "[OTel Init] ERROR: $var not set. Skipping OTel setup."
        exit 0  # Don't fail cluster startup
    fi
done

# --- Detect node metadata ---
CLUSTER_ID="${DB_CLUSTER_ID:-unknown}"
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || echo "unknown")
NODE_TYPE=$(curl -s http://169.254.169.254/latest/meta-data/instance-type 2>/dev/null || echo "unknown")

# Determine if driver
if [ "${DB_IS_DRIVER:-false}" = "TRUE" ] || [ "${DB_IS_DRIVER:-false}" = "true" ]; then
    IS_DRIVER="true"
else
    IS_DRIVER="false"
fi

echo "[OTel Init] cluster=$CLUSTER_ID instance=$INSTANCE_ID type=$NODE_TYPE driver=$IS_DRIVER"

# --- Install OTel Collector ---
mkdir -p "$CONFIG_DIR"

if [ -n "${OTEL_VOLUME_PATH:-}" ] && [ -f "${OTEL_VOLUME_PATH}/otelcol-contrib" ]; then
    echo "[OTel Init] Using pre-staged binary from Volume"
    cp "${OTEL_VOLUME_PATH}/otelcol-contrib" "$INSTALL_DIR/otelcol-contrib"
    chmod +x "$INSTALL_DIR/otelcol-contrib"
else
    echo "[OTel Init] Downloading OTel Collector v${OTEL_VERSION}"
    curl -sL "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTEL_VERSION}/otelcol-contrib_${OTEL_VERSION}_linux_amd64.tar.gz" \
        | tar xz -C "$INSTALL_DIR" otelcol-contrib
    chmod +x "$INSTALL_DIR/otelcol-contrib"
fi

echo "[OTel Init] Collector installed: $($INSTALL_DIR/otelcol-contrib --version 2>&1 | head -1)"

# --- OAuth Token Management ---
# Generate initial token and set up refresh
generate_token() {
    local response
    response=$(curl -s -X POST "$OTEL_TOKEN_ENDPOINT" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "grant_type=client_credentials" \
        -d "client_id=$OTEL_SP_CLIENT_ID" \
        -d "client_secret=$OTEL_SP_CLIENT_SECRET" \
        -d "scope=all-apis")

    local token
    token=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)

    if [ -z "$token" ]; then
        echo "[OTel Token] ERROR: Failed to get token. Response: $response"
        return 1
    fi

    echo "$token" > "$TOKEN_FILE"
    echo "[OTel Token] Token refreshed at $(date -u)"
}

# Generate initial token
generate_token || { echo "[OTel Init] Token generation failed. Exiting."; exit 0; }

# Token refresh cron (every 50 minutes)
cat > /tmp/otel-refresh-token.sh << 'REFRESH_EOF'
#!/bin/bash
source /tmp/otel-env.sh
curl -s -X POST "$OTEL_TOKEN_ENDPOINT" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=client_credentials" \
    -d "client_id=$OTEL_SP_CLIENT_ID" \
    -d "client_secret=$OTEL_SP_CLIENT_SECRET" \
    -d "scope=all-apis" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" > /tmp/otel-token 2>/dev/null
REFRESH_EOF
chmod +x /tmp/otel-refresh-token.sh

# Save env for refresh script
cat > /tmp/otel-env.sh << ENV_EOF
export OTEL_TOKEN_ENDPOINT="$OTEL_TOKEN_ENDPOINT"
export OTEL_SP_CLIENT_ID="$OTEL_SP_CLIENT_ID"
export OTEL_SP_CLIENT_SECRET="$OTEL_SP_CLIENT_SECRET"
ENV_EOF

# Schedule token refresh every 50 minutes
(crontab -l 2>/dev/null; echo "*/50 * * * * /tmp/otel-refresh-token.sh") | crontab - 2>/dev/null || true

# --- Write OTel Collector Config ---
cat > "$CONFIG_DIR/config.yaml" << EOF
receivers:
  hostmetrics:
    collection_interval: ${COLLECTION_INTERVAL}
    scrapers:
      cpu:
        metrics:
          system.cpu.utilization:
            enabled: true
      memory:
        metrics:
          system.memory.utilization:
            enabled: true
      disk:
        metrics:
          system.disk.utilization:
            enabled: true
      network:
      load:
      paging:
        metrics:
          system.paging.utilization:
            enabled: true

processors:
  resource:
    attributes:
      - key: cluster_id
        value: "${CLUSTER_ID}"
        action: upsert
      - key: instance_id
        value: "${INSTANCE_ID}"
        action: upsert
      - key: is_driver
        value: "${IS_DRIVER}"
        action: upsert
      - key: node_type
        value: "${NODE_TYPE}"
        action: upsert
  batch:
    timeout: 15s
    send_batch_size: 100

exporters:
  otlphttp:
    endpoint: "${OTEL_ENDPOINT}/api/otel"
    headers:
      Authorization: "Bearer \${file:/tmp/otel-token}"
    encoding: json
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s
      max_elapsed_time: 300s

service:
  pipelines:
    metrics:
      receivers: [hostmetrics]
      processors: [resource, batch]
      exporters: [otlphttp]
  telemetry:
    logs:
      level: warn
      output_paths: ["/tmp/otel-collector.log"]
EOF

echo "[OTel Init] Config written to $CONFIG_DIR/config.yaml"

# --- Start Collector ---
nohup "$INSTALL_DIR/otelcol-contrib" --config "$CONFIG_DIR/config.yaml" \
    > /tmp/otel-collector.log 2>&1 &

OTEL_PID=$!
echo "[OTel Init] Collector started (PID: $OTEL_PID)"

# Verify it's running after 3 seconds
sleep 3
if kill -0 "$OTEL_PID" 2>/dev/null; then
    echo "[OTel Init] Collector running successfully"
else
    echo "[OTel Init] WARNING: Collector may have crashed. Check /tmp/otel-collector.log"
    tail -20 /tmp/otel-collector.log 2>/dev/null || true
fi

echo "[OTel Init] Complete at $(date -u)"
