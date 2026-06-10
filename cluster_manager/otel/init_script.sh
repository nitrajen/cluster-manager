#!/bin/bash
# Init script: OTel Collector for multi-node clusters
# Runs on EVERY node (driver + workers). Each node reports independently.
set -e

# Configuration — set defaults here for centralized deployment, or override via cluster env vars
OTEL_ENDPOINT="${OTEL_ENDPOINT:-https://YOUR-APP.aws.databricksapps.com}"
OTEL_VOLUME_PATH="${OTEL_VOLUME_PATH:-/Volumes/main/cluster_manager/binaries}"
OTEL_INTERVAL="${OTEL_INTERVAL:-15s}"
OTEL_SP_CLIENT_ID="${OTEL_SP_CLIENT_ID:-YOUR_SP_CLIENT_ID}"
OTEL_SP_CLIENT_SECRET="${OTEL_SP_CLIENT_SECRET:-YOUR_SP_CLIENT_SECRET}"
OTEL_TOKEN_ENDPOINT="${OTEL_TOKEN_ENDPOINT:-https://YOUR-WORKSPACE.cloud.databricks.com/oidc/v1/token}"
OTEL_SECRET_SCOPE="${OTEL_SECRET_SCOPE:-otel-collector}"
OTEL_SECRET_KEY="${OTEL_SECRET_KEY:-sp-client-secret}"

# If no secret provided via env, fetch from Databricks Secret Scope
if [ -z "${OTEL_SP_CLIENT_SECRET}" ] || [ "${OTEL_SP_CLIENT_SECRET}" = "YOUR_SP_CLIENT_SECRET" ]; then
  if [ -n "${DATABRICKS_HOST:-}" ]; then
    _TOKEN=$(python3 -c "
import json, os
for p in ['/databricks/.credentials', '/tmp/.databricks_token']:
    try:
        d = json.load(open(p)); print(d.get('token','')); break
    except: pass
else:
    print(os.environ.get('DATABRICKS_TOKEN',''))
" 2>/dev/null)
    if [ -n "$_TOKEN" ]; then
      OTEL_SP_CLIENT_SECRET=$(curl -s \
        -H "Authorization: Bearer $_TOKEN" \
        "${DATABRICKS_HOST}/api/2.0/secrets/get?scope=${OTEL_SECRET_SCOPE}&key=${OTEL_SECRET_KEY}" \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('value',''))" 2>/dev/null)
      if [ -n "$OTEL_SP_CLIENT_SECRET" ]; then
        echo "Fetched SP secret from scope ${OTEL_SECRET_SCOPE}"
      fi
    fi
  fi
fi

if [ -z "$OTEL_SP_CLIENT_SECRET" ] || [ "$OTEL_SP_CLIENT_SECRET" = "YOUR_SP_CLIENT_SECRET" ]; then
  echo "ERROR: No SP secret available (env var or scope). Collector will not start."
  exit 0
fi

# Cluster metadata
CLUSTER_ID="${DB_CLUSTER_ID:-unknown}"
INSTANCE_ID=$(hostname)
NODE_TYPE="${DB_INSTANCE_TYPE:-unknown}"

# Detect driver vs worker
# DB_IS_DRIVER is set on some DBR versions but not all (missing on DBR 17.x)
if [ "${DB_IS_DRIVER}" = "TRUE" ] || [ "${DB_IS_DRIVER}" = "true" ]; then
  IS_DRIVER="true"
elif [ "${DB_IS_DRIVER}" = "FALSE" ] || [ "${DB_IS_DRIVER}" = "false" ]; then
  IS_DRIVER="false"
else
  # DBR 17+ doesn't set DB_IS_DRIVER. Use Spark conf: driver has spark.driver.host = own IP
  MY_IP=$(hostname -I | awk '{print $1}')
  DRIVER_IP=$(grep -s 'spark.driver.host' /databricks/spark/conf/spark-defaults.conf 2>/dev/null | awk '{print $2}')
  if [ -n "$DRIVER_IP" ] && [ "$MY_IP" = "$DRIVER_IP" ]; then
    IS_DRIVER="true"
  else
    IS_DRIVER="false"
  fi
fi

echo "OTel init: cluster=$CLUSTER_ID node=$INSTANCE_ID driver=$IS_DRIVER type=$NODE_TYPE"

# Install Collector
INSTALL_DIR="/opt/otelcol"
mkdir -p "$INSTALL_DIR"

if [ -f "${OTEL_VOLUME_PATH}/otelcol-contrib" ]; then
    cp "${OTEL_VOLUME_PATH}/otelcol-contrib" "$INSTALL_DIR/otelcol-contrib"
    echo "Copied collector from volume"
else
    VERSION=0.116.0
    curl -sL -o /tmp/otelcol.tar.gz \
      "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${VERSION}/otelcol-contrib_${VERSION}_linux_amd64.tar.gz"
    tar xzf /tmp/otelcol.tar.gz -C "$INSTALL_DIR" otelcol-contrib
    rm -f /tmp/otelcol.tar.gz
    echo "Downloaded collector v${VERSION}"
fi
chmod +x "$INSTALL_DIR/otelcol-contrib"

# Token refresh helper — generates fresh SP M2M token
get_oauth_token() {
  curl -s -X POST "${OTEL_TOKEN_ENDPOINT}" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=client_credentials&client_id=${OTEL_SP_CLIENT_ID}&client_secret=${OTEL_SP_CLIENT_SECRET}&scope=all-apis" \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null
}

TOKEN=$(get_oauth_token)

if [ -z "$TOKEN" ]; then
    echo "ERROR: Failed to get OAuth token. Collector will not start."
    exit 0
fi
echo "Got OAuth token (${#TOKEN} chars)"

# Write config helper — generates collector config with current token
write_config() {
  local token="$1"
  cat > "$INSTALL_DIR/config.yaml" << CFGEOF
receivers:
  hostmetrics:
    collection_interval: ${OTEL_INTERVAL}
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
      filesystem:
        metrics:
          system.filesystem.utilization:
            enabled: true
      network:
      load:
      paging:

processors:
  batch:
    timeout: 10s
    send_batch_size: 8

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

exporters:
  otlphttp:
    endpoint: ${OTEL_ENDPOINT}/api/otel
    encoding: json
    headers:
      Authorization: "Bearer ${token}"
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s

service:
  pipelines:
    metrics:
      receivers: [hostmetrics]
      processors: [resource, batch]
      exporters: [otlphttp]
CFGEOF
}

write_config "$TOKEN"

# Token refresh wrapper — restarts collector every 50 min with fresh token
cat > "$INSTALL_DIR/run_with_refresh.sh" << 'RUNEOF'
#!/bin/bash
INSTALL_DIR="/opt/otelcol"
REFRESH_SECONDS=3000  # 50 minutes

while true; do
  "$INSTALL_DIR/otelcol-contrib" --config "$INSTALL_DIR/config.yaml" &
  COLLECTOR_PID=$!
  echo "$(date) Collector started PID=$COLLECTOR_PID"

  sleep $REFRESH_SECONDS

  # Refresh token
  NEW_TOKEN=$(curl -s -X POST "${OTEL_TOKEN_ENDPOINT}" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=client_credentials&client_id=${OTEL_SP_CLIENT_ID}&client_secret=${OTEL_SP_CLIENT_SECRET}&scope=all-apis" \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)

  if [ -n "$NEW_TOKEN" ]; then
    # Rewrite config with new token
    sed -i "s|Authorization: \"Bearer .*\"|Authorization: \"Bearer ${NEW_TOKEN}\"|" "$INSTALL_DIR/config.yaml"
    echo "$(date) Token refreshed (${#NEW_TOKEN} chars)"
  else
    echo "$(date) WARNING: Token refresh failed, keeping old token"
  fi

  # Restart collector
  kill $COLLECTOR_PID 2>/dev/null
  wait $COLLECTOR_PID 2>/dev/null
done
RUNEOF
chmod +x "$INSTALL_DIR/run_with_refresh.sh"

# Export env vars for the refresh script
export OTEL_TOKEN_ENDPOINT OTEL_SP_CLIENT_ID OTEL_SP_CLIENT_SECRET

# Start collector with token refresh loop
"$INSTALL_DIR/run_with_refresh.sh" > /var/log/otelcol.log 2>&1 &
echo $! > "$INSTALL_DIR/collector.pid"

echo "OTel Collector started with token refresh (PID $(cat $INSTALL_DIR/collector.pid))"
echo "  Cluster: $CLUSTER_ID | Instance: $INSTANCE_ID | Driver: $IS_DRIVER | Type: $NODE_TYPE"
echo "  Endpoint: $OTEL_ENDPOINT | Interval: $OTEL_INTERVAL"
echo "  Token refresh: every 50 minutes"
