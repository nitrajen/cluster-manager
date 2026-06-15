# OTel Metrics Receiver

Minimal Databricks App that collects CPU, memory, disk, and network metrics from Databricks cluster nodes (driver + workers) via OpenTelemetry and appends them to a Delta table.

## Architecture

```
Cluster node (driver + each worker)
  └─ init_script.sh  (runs at cluster startup)
       └─ downloads otelcol-contrib binary
       └─ collects host metrics every 15s
       └─ pushes OTLP/HTTP → [this app]/api/otel/v1/metrics
              └─ appends to Delta table: main.cluster_manager.node_metrics
```

## Files

| File | Purpose |
|---|---|
| `app.py` | FastAPI entry point |
| `otel.py` | `/api/otel/v1/metrics` receiver endpoint |
| `db.py` | Delta table writer via SQL Warehouse |
| `app.yaml` | Databricks App config (fill in before deploying) |
| `requirements.txt` | Python dependencies |
| `init_script.sh` | Cluster init script — runs on every node at startup |

## Latency note

Writes go through a SQL Warehouse (statement execution API), not a direct DB connection.

- **Warm serverless warehouse**: ~0.5–2s per write — fine for 15s metrics
- **Cold warehouse**: 1–3 min to start, then OTel retries flush through
- **Recommendation**: use a Serverless SQL Warehouse — cold starts are ~2s

---

## Setup

### 1. Create a Service Principal

In your Databricks workspace: **Settings → Identity & Access → Service Principals → Add service principal**

- Name it something like `otel-collector`
- Click into it → **Generate secret** → copy the **Client ID** (UUID) and **Secret**
- Add the SP to the `users` group so it can authenticate to the workspace

---

### 2. Store the SP secret in a Secret Scope

The init script reads the SP secret from a Databricks Secret Scope at cluster startup so it never appears in cluster config.

```bash
databricks secrets create-scope otel-collector
databricks secrets put-secret otel-collector sp-client-secret
# Enter your SP secret when prompted
```

---

### 3. Fill in app.yaml

Edit `app.yaml` and replace all placeholder values:

| Variable | Value |
|---|---|
| `DATABRICKS_HOST` | Your workspace URL |
| `DELTA_CATALOG` | Unity Catalog catalog name (default: `main`) |
| `DELTA_SCHEMA` | Schema name (default: `cluster_manager`) |
| `SQL_WAREHOUSE_ID` | SQL Warehouse ID — leave blank to auto-discover, or paste ID from warehouse settings |
| `OTEL_ALLOWED_SP_IDS` | SP Client ID UUID from step 1 |

The Delta table `node_metrics` is created automatically on first deploy inside `{DELTA_CATALOG}.{DELTA_SCHEMA}`.

---

### 4. Deploy the app

```bash
cd otel-receiver

databricks apps create otel-receiver
databricks apps deploy otel-receiver --source-code-path .
```

Note the app URL from the output — it looks like:
`https://otel-receiver-xxxx.aws.databricksapps.com`

Check it started correctly:
```bash
curl https://otel-receiver-xxxx.aws.databricksapps.com/health
# → {"status": "healthy", "table": "`main`.`cluster_manager`.`node_metrics`", ...}
```

---

### 5. Upload the init script to your workspace

```bash
databricks workspace import /Workspace/Users/your.email@company.com/init_otel.sh \
  --file init_script.sh \
  --overwrite
```

---

### 6. Configure your dev cluster

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

**Check the collector log on the driver** (run in a notebook cell):
```python
import subprocess
print(subprocess.check_output(["cat", "/var/log/otelcol.log"]).decode())
```

You should see:
```
OTel init: cluster=0612-... node=... driver=true type=...
Got OAuth token (1234 chars)
OTel Collector started with token refresh (PID ...)
```

**Query the Delta table** from a Databricks notebook:
```sql
SELECT cluster_id, instance_id, is_driver, ts,
       cpu_user_percent, mem_used_percent
FROM main.cluster_manager.node_metrics
ORDER BY ts DESC
LIMIT 50
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
- ~1.2M rows/day, ~280 bytes/row on disk
- ~385 MB/day, ~2.5 GB at 7-day retention (Delta with Parquet compression will be smaller)

Retention is not automatically enforced — add a scheduled Databricks Job:
```sql
DELETE FROM main.cluster_manager.node_metrics WHERE ts < NOW() - INTERVAL 7 DAYS;
```
