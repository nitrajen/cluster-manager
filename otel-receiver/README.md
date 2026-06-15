# OTel Metrics Receiver

Minimal Databricks App that collects CPU, memory, disk, and network metrics from Databricks cluster nodes (driver + workers) via OpenTelemetry and writes them to Lakebase.

## Architecture

```
Cluster node (driver + each worker)
  └─ init_script.sh  (runs at cluster startup)
       └─ downloads otelcol-contrib binary
       └─ collects host metrics every 15s
       └─ pushes OTLP/HTTP → [this app]/api/otel/v1/metrics
              └─ writes to Lakebase → node_metrics table
```

## Files

| File | Purpose |
|---|---|
| `app.py` | FastAPI entry point |
| `otel.py` | `/api/otel` receiver endpoint |
| `db.py` | Lakebase connection pool |
| `app.yaml` | Databricks App config (fill in before deploying) |
| `requirements.txt` | Python dependencies |
| `init_script.sh` | Cluster init script — runs on every node at startup |

---

## Setup

### 1. Create a Service Principal

In your Databricks workspace: **Settings → Identity & Access → Service Principals → Add service principal**

- Name it something like `otel-collector`
- Click into it → **Generate secret** → copy the **Client ID** (UUID) and **Secret**
- Add the SP to the `users` group so it can authenticate

---

### 2. Create a Lakebase instance

```bash
databricks lakebase create-project --name cluster-metrics
databricks lakebase create-branch --project cluster-metrics --name main
databricks lakebase create-endpoint --project cluster-metrics --branch main --name primary

# Get the hostname — you'll need it for app.yaml
databricks lakebase get-endpoint --project cluster-metrics --branch main --endpoint primary
```

Copy the `host` value from the output. It looks like:
`abc123.database.cloud.databricks.com`

> Check `databricks lakebase --help` if the subcommand names differ — this is a newer CLI feature.

---

### 3. Store the SP secret in a Secret Scope

The init script reads the SP secret from a Databricks Secret Scope at cluster startup so it never appears in cluster config.

```bash
databricks secrets create-scope otel-collector
databricks secrets put-secret otel-collector sp-client-secret
# Enter your SP secret when prompted
```

---

### 4. Fill in app.yaml

Edit `app.yaml` and replace all placeholder values:

| Variable | Where to get it |
|---|---|
| `DATABRICKS_HOST` | Your workspace URL |
| `LAKEBASE_HOST` | Host from step 2 |
| `LAKEBASE_USER` | Your email address (Lakebase requires a human user token) |
| `OTEL_ALLOWED_SP_IDS` | SP Client ID UUID from step 1 |

---

### 5. Deploy the app

```bash
cd otel-receiver

databricks apps create otel-receiver
databricks apps deploy otel-receiver --source-code-path .
```

Note the app URL from the output — it looks like:
`https://otel-receiver-xxxx.aws.databricksapps.com`

---

### 6. Bootstrap Lakebase (one-time after deploy)

Lakebase only accepts human user tokens as passwords — SPs cannot connect directly. This step seeds the app with your token so it can write incoming metrics.

```bash
TOKEN=$(databricks auth token --host YOUR-WORKSPACE.cloud.databricks.com | grep Token | awk '{print $2}')

curl -X POST https://otel-receiver-xxxx.aws.databricksapps.com/api/otel/bootstrap \
  -H "Authorization: Bearer $TOKEN"
```

Expected response:
```json
{"status": "ok", "node_metrics_rows": 0, "user": "your.email@company.com"}
```

You only need to do this once per deployment. If the app restarts it will re-read the cached token.

---

### 7. Upload the init script to your workspace

```bash
databricks workspace import /Workspace/Users/your.email@company.com/init_otel.sh \
  --file init_script.sh \
  --overwrite
```

---

### 8. Configure your dev cluster

In your cluster's **Advanced Options**:

**Init Scripts** — add:
```
/Workspace/Users/your.email@company.com/init_otel.sh
```

**Environment Variables** — add:
```
OTEL_ENDPOINT=https://otel-receiver-xxxx.aws.databricksapps.com
OTEL_SP_CLIENT_ID=<SP Client ID from step 1>
OTEL_TOKEN_ENDPOINT=https://YOUR-WORKSPACE.cloud.databricks.com/oidc/v1/token
```

Start the cluster. The init script runs automatically on the driver and every worker node.

---

## Verify it's working

**Check the collector log on the driver:**
```bash
# In a cluster notebook cell:
import subprocess
print(subprocess.check_output(["cat", "/var/log/otelcol.log"]).decode())
```

You should see lines like:
```
OTel init: cluster=0612-... node=... driver=true type=...
Got OAuth token (1234 chars)
OTel Collector started with token refresh (PID ...)
```

**Query the table** (from a notebook connected to Lakebase, or via psql):
```sql
SELECT cluster_id, instance_id, is_driver, ts,
       cpu_user_percent, mem_used_percent
FROM node_metrics
ORDER BY ts DESC
LIMIT 50;
```

---

## Data collected

Every 15 seconds per node:

| Column | Description |
|---|---|
| `cluster_id` | Databricks cluster ID |
| `instance_id` | Hostname of the node |
| `is_driver` | `true` for driver, `false` for worker |
| `node_type` | EC2 instance type |
| `ts` | Timestamp |
| `cpu_user_percent` | CPU user % (avg across cores) |
| `cpu_system_percent` | CPU system % |
| `cpu_wait_percent` | CPU iowait % |
| `mem_used_percent` | Memory used % |
| `mem_available_bytes` | Memory available (bytes) |
| `network_sent_bytes` | Network TX bytes |
| `network_received_bytes` | Network RX bytes |
| `disk_used_percent` | Disk used % (max across mountpoints) |
| `load_1m / 5m / 15m` | Load averages |

## Storage estimate

At 100 clusters, 5 nodes each, 10 hours active per day:
- ~1.2M rows/day, ~280 bytes/row
- ~385 MB/day, ~2.5 GB at 7-day retention

Retention is not automatically enforced — add a scheduled purge job if needed:
```sql
DELETE FROM node_metrics WHERE ts < NOW() - INTERVAL '7 days';
```
