# Enhanced OTel Metrics for Proactive Cluster Health Monitoring

## Goal

Expand metric collection to enable:
1. Proactive alerting (detect failures before they happen)
2. Historical analysis (pattern recognition across failures)
3. ML training data (anomaly detection, capacity planning)

---

## Phase 1: Expand Collector Config + DB Schema (immediate)

### New Metrics to Collect

| Metric | OTel Name | DB Column | Alert Value |
|--------|-----------|-----------|-------------|
| Disk I/O time | `system.disk.io_time` | `disk_io_time_ms` | High = slow shuffle |
| Disk ops | `system.disk.operations` | `disk_ops_read`, `disk_ops_write` | Spike = shuffle storm |
| Network errors | `system.network.errors` | `network_errors` | >0 = degradation |
| Network drops | `system.network.dropped` | `network_drops` | >0 = packet loss |
| Paging ops (swap in/out) | `system.paging.operations` | `paging_in`, `paging_out` | Active swap = OOM soon |
| Process count | `system.processes.count` | `process_count` | Spike = fork bomb |
| Memory available | `system.memory.usage` (free+cached) | `mem_available_bytes` | Dropping fast = OOM |
| Filesystem inodes | `system.filesystem.inodes.usage` | `inodes_used_percent` | 100% = silent failures |

### Changes Required

**1. Collector config** (`init_script.sh` receivers section):
```yaml
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
```

**2. Lakebase schema** — ALTER TABLE add columns:
```sql
ALTER TABLE node_metrics ADD COLUMN disk_io_time_ms FLOAT;
ALTER TABLE node_metrics ADD COLUMN disk_ops_read BIGINT;
ALTER TABLE node_metrics ADD COLUMN disk_ops_write BIGINT;
ALTER TABLE node_metrics ADD COLUMN network_errors BIGINT;
ALTER TABLE node_metrics ADD COLUMN network_drops BIGINT;
ALTER TABLE node_metrics ADD COLUMN paging_in BIGINT;
ALTER TABLE node_metrics ADD COLUMN paging_out BIGINT;
ALTER TABLE node_metrics ADD COLUMN process_count INT;
ALTER TABLE node_metrics ADD COLUMN mem_available_bytes BIGINT;
ALTER TABLE node_metrics ADD COLUMN inodes_used_percent FLOAT;
```

**3. Backend parser** (`otel.py`):
- Add new metric names to parser
- Add columns to INSERT_SQL
- Add to row template

**4. Init script** — update receiver config section

### Verification
- Deploy updated init script
- Restart cluster
- Check new columns populated in Lakebase
- Verify dashboard still works (new columns are nullable)

---

## Phase 2: Proactive Alert Engine

### Alert Rules

| Alert | Condition | Severity | Action |
|-------|-----------|----------|--------|
| OOM approaching | `mem_used_percent > 85` AND rising for 3 readings | CRITICAL | Notify + suggest right-size |
| Disk full | `disk_used_percent > 90` OR growth > 5%/min | CRITICAL | Notify + suggest cleanup |
| CPU saturated | `cpu_user + cpu_system > 90` sustained 5+ min | WARNING | Notify |
| Swap storm | `paging_out > 1000` ops/interval | CRITICAL | OOM imminent |
| Network degraded | `network_errors > 0` OR `network_drops > 100` | WARNING | Shuffle retries likely |
| Straggler detected | One worker CPU < 10% while cluster avg > 60% | WARNING | Data skew probable |
| Disk I/O bottleneck | `disk_io_time_ms > 500` sustained | WARNING | Shuffle on slow disk |
| Inode exhaustion | `inodes_used_percent > 95` | CRITICAL | Small file explosion |

### Architecture

```
node_metrics table ← new metrics
       │
       ▼
┌─── Alert Evaluator (periodic, every 30s) ───┐
│  • Query last 5 min of metrics per cluster    │
│  • Evaluate rules against windowed data       │
│  • Detect trends (rising/falling/sustained)   │
│  • Deduplicate (don't re-fire same alert)     │
│  └── Write to alerts table                    │
└───────────────────────────────────────────────┘
       │
       ▼
  alerts table → API → Dashboard banner
                     → Slack/Teams webhook (future)
```

### New DB table:
```sql
CREATE TABLE alerts (
    id SERIAL PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    instance_id TEXT,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    value FLOAT,
    threshold FLOAT,
    message TEXT,
    is_driver BOOLEAN,
    fired_at TIMESTAMP NOT NULL,
    resolved_at TIMESTAMP,
    acknowledged BOOLEAN DEFAULT FALSE
);
```

### Backend:
- Background task running every 30s
- Evaluates rules against recent metrics
- Writes new alerts, resolves stale ones
- API endpoint for current alerts (already exists: `/api/live/alerts`)

---

## Phase 3: Delta Lake Archive (ML-ready)

### Dual-write from OTel endpoint

When metrics arrive:
1. Insert into Lakebase (hot path, 7-day retention) — existing
2. Append to Delta Lake table (cold path, unlimited) — new

### Delta table schema
```sql
CREATE TABLE main.cluster_manager.node_metrics_history (
    cluster_id STRING,
    instance_id STRING,
    is_driver BOOLEAN,
    node_type STRING,
    ts TIMESTAMP,
    -- All metric columns (same as Lakebase)
    cpu_user_percent FLOAT,
    cpu_system_percent FLOAT,
    cpu_wait_percent FLOAT,
    mem_used_percent FLOAT,
    mem_available_bytes LONG,
    disk_used_percent FLOAT,
    disk_io_time_ms FLOAT,
    disk_ops_read LONG,
    disk_ops_write LONG,
    network_sent_bytes LONG,
    network_received_bytes LONG,
    network_errors LONG,
    network_drops LONG,
    paging_in LONG,
    paging_out LONG,
    load_1m FLOAT,
    load_5m FLOAT,
    load_15m FLOAT,
    process_count INT,
    inodes_used_percent FLOAT,
    -- Partition key
    date DATE GENERATED ALWAYS AS (CAST(ts AS DATE))
) USING DELTA
PARTITIONED BY (date, cluster_id)
TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true');
```

### Write mechanism options:
- **Option A**: Direct write from app via Databricks SQL connector (simple, adds latency)
- **Option B**: Lakebase → Delta sync (automated, but depends on Lakebase retention)
- **Option C**: Separate Spark streaming job reading from Lakebase (overkill for now)

Recommend **Option A** for MVP — one SQL INSERT per batch alongside Lakebase insert.

---

## Phase 4: ML Pattern Detection

### Use Cases

1. **Failure precursor detection**
   - Input: 30 min of metrics before known failures (OOM, timeout, crash)
   - Model: Isolation Forest or LSTM on multivariate time series
   - Output: "This pattern preceded failure 80% of the time" → early warning

2. **Anomaly detection**
   - Input: Per-cluster baseline (normal operating range)
   - Model: Statistical (z-score on sliding window) or ML (autoencoder)
   - Output: "CPU pattern unusual for this cluster type at this time"

3. **Recurrence detection**
   - Input: Alert history + cluster config
   - Model: Pattern matching (same cluster, same alert, same day-of-week?)
   - Output: "Cluster X fails every Monday 9am — scheduled job too large"

4. **Capacity planning**
   - Input: Peak usage trends over weeks
   - Model: Linear regression + seasonality
   - Output: "At current growth, cluster Y will OOM within 2 weeks"

### Training data requirements
- Minimum 2 weeks of continuous data
- Labels: cluster events (termination reasons, OOM events from system tables)
- Feature engineering: rolling averages, rate-of-change, variance

---

## Implementation Order

| Phase | Effort | Value | Dependencies |
|-------|--------|-------|-------------|
| 1: Expand metrics | 2-3 hours | High (foundation for everything) | None |
| 2: Alert engine | 4-6 hours | High (immediate proactive value) | Phase 1 |
| 3: Delta archive | 2-3 hours | Medium (enables ML later) | Phase 1 |
| 4: ML patterns | 1-2 weeks | Very High (predictive) | Phase 3 + 2 weeks of data |

---

## Immediate Action (Phase 1)

Files to modify:
1. `cluster_manager/otel/init_script.sh` — expanded scrapers
2. `cluster_manager/backend/routers/otel.py` — parse new metrics
3. Lakebase migration — ALTER TABLE
4. `cluster_manager/otel/push_to_deployed.sh` — match local config
5. Deploy + redeploy init script to workspace
