# Plan: OTel Live Metrics Pipeline

## Context

The cluster-manager app currently shows cluster CPU/memory metrics from system.compute.node_timeline (15-30 min lag). Goal: add real-time metrics (<30s lag) by deploying an OTel Collector on cluster nodes that pushes to the app, stored in Lakebase for low-latency dashboard queries.

## Architecture

```
┌───────────────────────────────────────────┐
│  Databricks Cluster Node (init script)    │
│  ┌─────────────────────────────────────┐  │
│  │  OTel Collector (hostmetrics)       │  │
│  │  - CPU, memory, disk, network, load │  │
│  │  - 15s collection interval          │  │
│  │  - OTLP/HTTP JSON exporter          │  │
│  │  - OAuth token auth (SP creds)      │  │
│  └──────────────┬──────────────────────┘  │
└─────────────────┼─────────────────────────┘
                  │ POST /api/otel/v1/metrics
                  ▼
┌───────────────────────────────────────────┐
│  Cluster Manager App (FastAPI)            │
│  ┌─────────────────────────────────────┐  │
│  │  OTel Receiver Router               │  │
│  │  - Validate OAuth token             │  │
│  │  - Parse OTLP JSON metrics          │  │
│  │  - Batch insert to Lakebase         │  │
│  └──────────────┬──────────────────────┘  │
│                 ▼                          │
│  ┌─────────────────────────────────────┐  │
│  │  Lakebase (PostgreSQL)              │  │
│  │  - node_metrics table               │  │
│  │  - Indexed by (cluster_id, time)    │  │
│  │  - Retention: 7 days               │  │
│  └──────────────┬──────────────────────┘  │
│                 ▼                          │
│  ┌─────────────────────────────────────┐  │
│  │  Frontend Dashboard                 │  │
│  │  - Real-time metrics charts         │  │
│  │  - Per-node breakdown              │  │
│  │  - Alert on thresholds             │  │
│  └─────────────────────────────────────┘  │
└───────────────────────────────────────────┘
```

## Components to Build

### Component 1: Init Script (OTel Client on Nodes)

**File:** `cluster_manager/otel/init_script.sh`

What it does:
- Downloads OTel Collector contrib binary (or fetches from pre-staged Volume location)
- Writes config YAML with hostmetrics receiver (cpu, memory, disk, network, load)
- Configures otlphttp exporter pointing to app's public URL
- Sets up OAuth token refresh using SP credentials (from env vars injected by cluster policy)
- Starts collector as background daemon
- Collection interval: 15 seconds

Key details:
- Binary pre-staged in UC Volume for fast startup: `/Volumes/main/cluster_manager_app/binaries/otelcol-contrib`
- SP credentials passed via cluster env vars (set by policy or spark_env_vars)
- Token refresh via a helper script that runs every 50 minutes
- Adds metadata: cluster_id, instance_id, is_driver, node_type

### Component 2: OTel Receiver (FastAPI Router)

**File:** `cluster_manager/backend/routers/otel.py`

New router: `POST /api/otel/v1/metrics`
- Accepts OTLP/HTTP JSON payload (Content-Type: application/json)
- Validates OAuth bearer token against Databricks workspace (verify SP token is valid)
- Parses ExportMetricsServiceRequest JSON structure
- Extracts: resource attributes (cluster_id, instance_id, node_type) + metric data points
- Batch-inserts into Lakebase node_metrics table
- Returns ExportMetricsServiceResponse (empty success response)

Dependencies:
- psycopg2-binary (or asyncpg for async) — PostgreSQL driver for Lakebase
- No protobuf dependency needed (using JSON format)

### Component 3: Lakebase Schema & Connection

**File:** `cluster_manager/backend/db.py`

Database: Lakebase Autoscaling project `cluster-metrics`
- Branch: production
- Endpoint: primary

Table schema:
```sql
CREATE TABLE node_metrics (
    id BIGSERIAL PRIMARY KEY,
    cluster_id VARCHAR(64) NOT NULL,
    instance_id VARCHAR(64) NOT NULL,
    is_driver BOOLEAN NOT NULL DEFAULT FALSE,
    node_type VARCHAR(64),
    timestamp TIMESTAMPTZ NOT NULL,
    cpu_user_percent DOUBLE PRECISION,
    cpu_system_percent DOUBLE PRECISION,
    cpu_wait_percent DOUBLE PRECISION,
    mem_used_percent DOUBLE PRECISION,
    mem_swap_percent DOUBLE PRECISION,
    network_sent_bytes BIGINT,
    network_received_bytes BIGINT,
    disk_used_percent DOUBLE PRECISION,
    load_1m DOUBLE PRECISION,
    load_5m DOUBLE PRECISION,
    load_15m DOUBLE PRECISION
);

CREATE INDEX idx_node_metrics_cluster_time ON node_metrics (cluster_id, timestamp DESC);
CREATE INDEX idx_node_metrics_time ON node_metrics (timestamp DESC);
```

Retention: Background task deletes rows older than 7 days (runs hourly).

Connection management:
- OAuth token generated via `databricks postgres generate-database-credential`
- Token cached in app state, refreshed every 50 minutes
- Connection pool via psycopg2 connection pool or asyncpg.Pool

### Component 4: Live Metrics API

**File:** `cluster_manager/backend/routers/live_metrics.py`

Endpoints for frontend:
- `GET /api/live-metrics/{cluster_id}` — Latest metrics for all nodes in a cluster (last 5 min)
- `GET /api/live-metrics/{cluster_id}/history?minutes=60` — Time series for charting
- `GET /api/live-metrics/alerts` — Clusters exceeding thresholds (CPU>80%, mem>90%)
- `GET /api/live-metrics/active` — List of clusters currently reporting live metrics

### Component 5: Frontend Dashboard

**File:** `cluster_manager/ui/routes/_sidebar/live-metrics.tsx`

New page in sidebar navigation:
- Real-time table showing all clusters with live metrics (auto-refresh every 15s)
- Click cluster → per-node time-series charts (CPU, memory, network)
- Alert badges for overloaded nodes
- Status indicator: "Live" vs "Stale" (no data in last 2 min)

### Component 6: Cluster Policy Template

**File:** `cluster_manager/otel/policy_template.json`

Example cluster policy that:
- Sets init script path to the OTel init script in Volume
- Injects SP credentials as env vars (OTEL_SP_CLIENT_ID, OTEL_SP_CLIENT_SECRET)
- Sets app endpoint URL (OTEL_ENDPOINT)

## Implementation Order

1. **Lakebase setup** — Create project, database, table → verify: psql connect and query works
2. **DB connection module** — db.py with pool + token refresh → verify: app starts, can INSERT/SELECT
3. **OTel receiver router** — Parse OTLP JSON, insert to Lakebase → verify: curl with sample payload succeeds
4. **Init script** — OTel Collector config + OAuth → verify: start test cluster, see data arrive in Lakebase
5. **Live metrics API** — Query endpoints for frontend → verify: hit API, get data from step 4
6. **Frontend dashboard** — Charts + auto-refresh → verify: see live data in browser
7. **Retention + cleanup** — Hourly purge of old rows → verify: data older than 7d deleted

## Key Decisions

- **OTLP/HTTP JSON** (not protobuf) — simpler to parse in Python, no proto compilation needed
- **Lakebase** (not Delta) — sub-10ms query latency for dashboard, no warehouse dependency
- **OAuth via SP** — secure, tokens auto-refreshed, no static keys to rotate
- **15s collection interval** — good resolution without flooding (4 writes/min/node)
- **7-day retention** — enough for trend analysis, keeps Lakebase storage manageable
- **Pre-staged binary in Volume** — avoids GitHub download on every cluster start (faster, more reliable)

## Verification Plan

1. Create Lakebase project manually via CLI, run CREATE TABLE
2. Start app locally, confirm /api/otel/v1/metrics accepts sample payload
3. Confirm data appears in Lakebase via psql
4. Deploy init script to a test cluster
5. Confirm cluster nodes push metrics to app endpoint
6. Open frontend, see live charts updating
7. Kill cluster, confirm data stops arriving (status goes "Stale")
