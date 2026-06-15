# OTel Metrics Receiver

Minimal Databricks App that collects CPU, memory, disk, and network metrics from Databricks cluster nodes (driver + workers) and serves them for live UI and historical analysis.

## Architecture

```
Cluster node (driver + each worker)
  └─ init_script.sh  (runs at startup on every node)
       └─ downloads otelcol-contrib binary
       └─ collects host metrics every 15s
       └─ pushes OTLP/HTTP → [this app]/api/otel/v1/metrics

Receiver app (Databricks App — long-running uvicorn process)
  └─ on every push:
       ├─ HotCache  (in-memory ring buffer, last 15 min)
       │     └─ GET /api/otel/live  ← UI reads here, sub-millisecond
       └─ WriteBuffer (accumulates rows, flushes every 30s)
             └─ Delta table: {DELTA_CATALOG}.{DELTA_SCHEMA}.node_metrics
                   └─ historical queries, notebooks, optimization
```

## Files

| File | Purpose |
|---|---|
| `app.py` | FastAPI entry point, lifespan, warehouse init |
| `otel.py` | `/api/otel/v1/metrics` receiver + `/api/otel/live` read endpoint |
| `db.py` | HotCache (in-memory) + WriteBuffer (Delta flush) |
| `maintenance.py` | Daily Databricks notebook: OPTIMIZE, VACUUM, retention DELETE |
| `init_script.sh` | Cluster init script — runs on every node at startup |
| `app.yaml` | Databricks App config — fill in before deploying |
| `requirements.txt` | Python dependencies |

## API Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/otel/v1/metrics` | POST | Receives OTLP/HTTP pushes from cluster nodes |
| `/api/otel/live` | GET | Returns recent metrics from memory for live UI |
| `/health` | GET | Health check, shows table name and buffered row count |

### Live endpoint query params

```
GET /api/otel/live?cluster_id=0612-123456-abc&minutes=5
```

| Param | Default | Description |
|---|---|---|
| `cluster_id` | none | Filter to one cluster (omit for all) |
| `minutes` | 5 | Lookback window (max `HOT_WINDOW_MINUTES`) |

Response:
```json
{
  "rows": [{"cluster_id": "...", "instance_id": "...", "is_driver": true, "ts": "2024-01-01T12:00:00+00:00", "cpu_user_percent": 12.3, ...}],
  "count": 42,
  "clusters": ["0612-123456-abc", "0612-789012-def"]
}
```

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

---

## Setup

### 1. Create a Service Principal

**Settings → Identity & Access → Service Principals → Add service principal**

- Name it `otel-collector`
- Click into it → **Generate secret** → copy the **Client ID** (UUID) and **Secret**
- Add it to the `users` group

---

### 2. Store the SP secret in a Secret Scope

The init script reads the secret from here at cluster startup — it never appears in cluster config.

```bash
databricks secrets create-scope otel-collector
databricks secrets put-secret otel-collector sp-client-secret
# paste your SP secret when prompted
```

---

### 3. Fill in app.yaml

| Variable | Value |
|---|---|
| `DATABRICKS_HOST` | Your workspace URL |
| `DELTA_CATALOG` | Unity Catalog name (default: `main`) |
| `DELTA_SCHEMA` | Schema name (default: `cluster_manager`) |
| `SQL_WAREHOUSE_ID` | Warehouse ID for Delta writes — leave blank to auto-discover. Use a Serverless warehouse. |
| `OTEL_ALLOWED_SP_IDS` | SP Client ID UUID from step 1 |
| `HOT_WINDOW_MINUTES` | How long to keep data in memory for live reads (default: `15`) |
| `FLUSH_INTERVAL_SECONDS` | How often to flush to Delta (default: `30`) |

---

### 4. Deploy the app

```bash
cd otel-receiver

databricks apps create otel-receiver
databricks apps deploy otel-receiver --source-code-path .
```

Note the app URL: `https://otel-receiver-xxxx.aws.databricksapps.com`

Verify it's up:
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

### 6. Configure your cluster

In **Advanced Options**:

**Init Scripts:**
```
/Workspace/Users/your.email@company.com/init_otel.sh
```

**Environment Variables:**
```
OTEL_ENDPOINT=https://otel-receiver-xxxx.aws.databricksapps.com
OTEL_SP_CLIENT_ID=<SP Client ID from step 1>
OTEL_TOKEN_ENDPOINT=https://YOUR-WORKSPACE.cloud.databricks.com/oidc/v1/token
```

Start the cluster — the init script runs automatically on driver and every worker.

---

## Verify it's working

**Check the collector log** (run in a notebook cell on the cluster):
```python
import subprocess
print(subprocess.check_output(["cat", "/var/log/otelcol.log"]).decode())
```

Expected output:
```
OTel init: cluster=0612-... node=... driver=true type=...
Got OAuth token (1234 chars)
OTel Collector started with token refresh (PID ...)
```

**Check live data** (within 15s of cluster start):
```bash
curl "https://otel-receiver-xxxx.aws.databricksapps.com/api/otel/live?minutes=1"
```

**Query historical data** from a Databricks notebook:
```sql
SELECT cluster_id, instance_id, is_driver, ts, cpu_user_percent, mem_used_percent
FROM main.cluster_manager.node_metrics
ORDER BY ts DESC
LIMIT 50
```

---

## Maintenance

Import `maintenance.py` into your Databricks workspace and schedule it as a daily Job.

It runs:
1. `DELETE` rows older than 7 days (retention)
2. `OPTIMIZE ... ZORDER BY (cluster_id, ts)` (compaction + query performance)
3. `VACUUM` (remove stale files)
4. Row count / date range sanity check

---

## Data loss on restart

| Scenario | Delta loss | Live UI gap |
|---|---|---|
| Graceful redeploy | 0 rows (shutdown flush runs) | ~15s (one OTel interval) |
| Crash, app back in < 30s | ~0 (OTel collector retries) | ~15s |
| Crash, app down > 30s | up to 30s of buffered rows | until app returns |

## Storage estimate

At 100 clusters, 5 nodes each, 10 hours active per day:
- ~1.2M rows/day
- ~2.5 GB at 7-day retention (before Parquet compression, actual will be smaller)
