# OTel Live Metrics — Cluster Init Script Deployment

Deploy an OpenTelemetry Collector on Databricks cluster nodes to push real-time CPU, memory, disk, and network metrics to the Cluster Manager app.

## Architecture

```
┌─────────────────────────────────────────────┐
│  Databricks Cluster Node                    │
│  ┌───────────────────────────────────────┐  │
│  │  OTel Collector (hostmetrics)         │  │
│  │  CPU, Memory, Disk, Network, Load     │  │
│  │  15s interval → OTLP/HTTP JSON       │  │
│  └──────────────────┬────────────────────┘  │
└─────────────────────┼───────────────────────┘
                      │ POST /api/otel/v1/metrics
                      ▼
┌─────────────────────────────────────────────┐
│  Cluster Manager App                        │
│  → Lakebase (PostgreSQL) → Dashboard       │
└─────────────────────────────────────────────┘
```

## Prerequisites

1. **Cluster Manager app deployed** with OTel endpoint accessible
2. **OAuth token** or Service Principal credentials for authentication
3. **UC Volume** to stage the OTel Collector binary (faster startup than GitHub download)

## Step 1: Stage the Collector Binary

Download and upload `otelcol-contrib` to a Unity Catalog Volume:

```bash
# Download for Linux x86_64 (cluster architecture)
VERSION=0.116.0
curl -L -o otelcol-contrib.tar.gz \
  "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${VERSION}/otelcol-contrib_${VERSION}_linux_amd64.tar.gz"

tar xzf otelcol-contrib.tar.gz otelcol-contrib

# Upload to Volume
databricks fs cp otelcol-contrib \
  dbfs:/Volumes/main/cluster_manager/binaries/otelcol-contrib \
  --profile YOUR_PROFILE
```

## Step 2: Create the Init Script

Save this as `init_otel_metrics.sh` and upload to a Volume:

```bash
#!/bin/bash
# Init script: Deploy OTel Collector for live metrics
# Sends CPU, memory, disk, network metrics to Cluster Manager app.

set -e

# --- Configuration (set via cluster env vars or spark_env_vars) ---
OTEL_ENDPOINT="${OTEL_ENDPOINT:-https://cluster-manager-7474645572615955.aws.databricksapps.com}"
OTEL_VOLUME_PATH="${OTEL_VOLUME_PATH:-/Volumes/main/cluster_manager/binaries}"
OTEL_INTERVAL="${OTEL_INTERVAL:-15s}"

# Cluster metadata (auto-detected from Databricks environment)
CLUSTER_ID="${DB_CLUSTER_ID}"
INSTANCE_ID=$(hostname)
IS_DRIVER="${DB_IS_DRIVER:-false}"
NODE_TYPE="${DB_INSTANCE_TYPE:-unknown}"

# --- Install Collector ---
INSTALL_DIR="/opt/otelcol"
mkdir -p "$INSTALL_DIR"

# Copy from Volume (pre-staged, fast) or download
if [ -f "${OTEL_VOLUME_PATH}/otelcol-contrib" ]; then
    cp "${OTEL_VOLUME_PATH}/otelcol-contrib" "$INSTALL_DIR/otelcol-contrib"
else
    VERSION=0.116.0
    curl -sL -o /tmp/otelcol.tar.gz \
      "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${VERSION}/otelcol-contrib_${VERSION}_linux_amd64.tar.gz"
    tar xzf /tmp/otelcol.tar.gz -C "$INSTALL_DIR" otelcol-contrib
    rm /tmp/otelcol.tar.gz
fi
chmod +x "$INSTALL_DIR/otelcol-contrib"

# --- Generate OAuth Token ---
# Option A: Use SP credentials from env vars
if [ -n "$OTEL_SP_CLIENT_ID" ] && [ -n "$OTEL_SP_CLIENT_SECRET" ]; then
    TOKEN=$(curl -s -X POST "${DATABRICKS_HOST}/oidc/v1/token" \
      -H "Content-Type: application/x-www-form-urlencoded" \
      -d "grant_type=client_credentials&client_id=${OTEL_SP_CLIENT_ID}&client_secret=${OTEL_SP_CLIENT_SECRET}&scope=all-apis" \
      | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
fi

# Option B: Use cluster's OAuth token (available on Databricks Runtime)
if [ -z "$TOKEN" ] && [ -f "/databricks/secrets/token" ]; then
    TOKEN=$(cat /databricks/secrets/token)
fi

# Option C: Static token (for testing only)
if [ -z "$TOKEN" ]; then
    TOKEN="${OTEL_TOKEN:-}"
fi

if [ -z "$TOKEN" ]; then
    echo "WARNING: No OAuth token available for OTel metrics. Skipping."
    exit 0
fi

# --- Write Collector Config ---
cat > "$INSTALL_DIR/config.yaml" << EOF
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
      disk:
      filesystem:
        metrics:
          system.filesystem.utilization:
            enabled: true
      network:
      load:

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
      Authorization: "Bearer ${TOKEN}"

service:
  pipelines:
    metrics:
      receivers: [hostmetrics]
      processors: [resource, batch]
      exporters: [otlphttp]
EOF

# --- Token Refresh Daemon ---
# OAuth tokens expire in 60 minutes. Refresh every 50 minutes.
cat > "$INSTALL_DIR/refresh_token.sh" << 'REFRESH_EOF'
#!/bin/bash
while true; do
    sleep 3000  # 50 minutes
    if [ -n "$OTEL_SP_CLIENT_ID" ] && [ -n "$OTEL_SP_CLIENT_SECRET" ]; then
        NEW_TOKEN=$(curl -s -X POST "${DATABRICKS_HOST}/oidc/v1/token" \
          -H "Content-Type: application/x-www-form-urlencoded" \
          -d "grant_type=client_credentials&client_id=${OTEL_SP_CLIENT_ID}&client_secret=${OTEL_SP_CLIENT_SECRET}&scope=all-apis" \
          | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null)
        if [ -n "$NEW_TOKEN" ]; then
            sed -i "s/Authorization: .*/Authorization: \"Bearer ${NEW_TOKEN}\"/" /opt/otelcol/config.yaml
            # Signal collector to reload config
            kill -HUP $(cat /opt/otelcol/collector.pid) 2>/dev/null
        fi
    fi
done
REFRESH_EOF
chmod +x "$INSTALL_DIR/refresh_token.sh"

# --- Start Collector ---
"$INSTALL_DIR/otelcol-contrib" --config "$INSTALL_DIR/config.yaml" \
  > /var/log/otelcol.log 2>&1 &
echo $! > "$INSTALL_DIR/collector.pid"

# Start token refresh daemon
"$INSTALL_DIR/refresh_token.sh" &
echo $! > "$INSTALL_DIR/refresh.pid"

echo "OTel Collector started (PID $(cat $INSTALL_DIR/collector.pid))"
echo "  Cluster: $CLUSTER_ID"
echo "  Instance: $INSTANCE_ID"
echo "  Driver: $IS_DRIVER"
echo "  Endpoint: $OTEL_ENDPOINT"
```

Upload the script:

```bash
databricks fs cp init_otel_metrics.sh \
  dbfs:/Volumes/main/cluster_manager/init_scripts/init_otel_metrics.sh \
  --profile YOUR_PROFILE
```

## Step 3: Configure the Cluster

### Option A: Cluster Policy (recommended for fleet-wide)

```json
{
  "init_scripts.0.volumes.destination": {
    "type": "fixed",
    "value": "/Volumes/main/cluster_manager/init_scripts/init_otel_metrics.sh"
  },
  "spark_env_vars.OTEL_ENDPOINT": {
    "type": "fixed",
    "value": "https://cluster-manager-7474645572615955.aws.databricksapps.com"
  },
  "spark_env_vars.OTEL_SP_CLIENT_ID": {
    "type": "fixed",
    "value": "YOUR_SP_CLIENT_ID"
  },
  "spark_env_vars.OTEL_SP_CLIENT_SECRET": {
    "type": "fixed",
    "value": "{{secrets/otel/sp-secret}}"
  }
}
```

### Option B: Per-Cluster Configuration

In the cluster's Advanced Options → Init Scripts:

```
/Volumes/main/cluster_manager/init_scripts/init_otel_metrics.sh
```

Add environment variables in Spark → Environment Variables:

```
OTEL_ENDPOINT=https://cluster-manager-7474645572615955.aws.databricksapps.com
OTEL_SP_CLIENT_ID=your-sp-client-id
OTEL_SP_CLIENT_SECRET={{secrets/otel/sp-secret}}
```

### Option C: Using Databricks Secrets (most secure)

```bash
# Create secret scope
databricks secrets create-scope otel

# Store SP credentials
databricks secrets put-secret otel sp-client-id --string-value "YOUR_CLIENT_ID"
databricks secrets put-secret otel sp-secret --string-value "YOUR_SECRET"
```

Reference in env vars:
```
OTEL_SP_CLIENT_ID={{secrets/otel/sp-client-id}}
OTEL_SP_CLIENT_SECRET={{secrets/otel/sp-secret}}
```

## Step 4: Verify

1. Start the cluster
2. Check init script logs in the cluster's Event Log → Init Scripts tab
3. Verify data in the dashboard:
   ```bash
   curl -s "https://YOUR_APP_URL/api/live-metrics/active" \
     -H "Authorization: Bearer YOUR_TOKEN"
   ```
4. SSH to a node and check collector health:
   ```bash
   cat /var/log/otelcol.log | tail -20
   curl -s http://localhost:8888/metrics | grep otelcol_exporter_sent_metric_points
   ```

## Metrics Collected

| Metric | Column | Unit |
|--------|--------|------|
| CPU User | `cpu_user_percent` | % |
| CPU System | `cpu_system_percent` | % |
| CPU Wait | `cpu_wait_percent` | % |
| Memory Used | `mem_used_percent` | % |
| Memory Swap | `mem_swap_percent` | % |
| Network TX | `network_sent_bytes` | bytes |
| Network RX | `network_received_bytes` | bytes |
| Disk Used | `disk_used_percent` | % |
| Load 1m/5m/15m | `load_1m/5m/15m` | avg |

## Configuration Reference

| Env Var | Default | Description |
|---------|---------|-------------|
| `OTEL_ENDPOINT` | (required) | Cluster Manager app URL |
| `OTEL_VOLUME_PATH` | `/Volumes/main/cluster_manager/binaries` | Path to pre-staged binary |
| `OTEL_INTERVAL` | `15s` | Collection interval |
| `OTEL_SP_CLIENT_ID` | — | Service Principal client ID |
| `OTEL_SP_CLIENT_SECRET` | — | SP secret (use Databricks Secrets) |
| `OTEL_TOKEN` | — | Static token (testing only) |

## Troubleshooting

**No data appearing:**
- Check `/var/log/otelcol.log` on the node for export errors
- Verify the app endpoint is reachable from cluster nodes
- Confirm OAuth token is valid (check for 401 errors in log)

**Permission denied on binary:**
- Ensure `chmod +x` succeeded in init script
- Check Volume permissions for the binary file

**Token refresh not working:**
- Verify SP credentials are correct
- Check `DATABRICKS_HOST` is set (auto-populated on Databricks Runtime)
- Look for refresh errors in `/var/log/otelcol.log`

**High CPU from collector:**
- Increase `collection_interval` to `30s` or `60s`
- Reduce scrapers (remove `disk`/`filesystem` if not needed)

## Data Retention

Metrics older than 7 days are automatically purged from Lakebase. The retention job runs on each app restart. For longer retention, modify `purge_old_metrics()` in `cluster_manager/backend/db.py`.
