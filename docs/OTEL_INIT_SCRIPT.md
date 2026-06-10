# OTel Live Metrics — Complete Setup Guide

Deploy OpenTelemetry Collectors on Databricks cluster nodes to push real-time CPU, memory, disk, and network metrics to the Cluster Manager app.

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  Client Workspace (e.g. DEMO WEST)                                 │
│                                                                    │
│  ┌─── Cluster (multi-node) ───────────────────────────────────┐   │
│  │  Driver Node           Worker 1        Worker 2    ...      │   │
│  │  ┌──────────────┐     ┌──────────┐   ┌──────────┐         │   │
│  │  │ OTel Coll.   │     │ OTel C.  │   │ OTel C.  │         │   │
│  │  │ hostmetrics  │     │ hostm.   │   │ hostm.   │         │   │
│  │  └──────┬───────┘     └────┬─────┘   └────┬─────┘         │   │
│  │         │                   │               │               │   │
│  └─────────┼───────────────────┼───────────────┼───────────────┘   │
│            │                   │               │                    │
│  Secret Scope ─── SP secret ──►│               │                    │
│  (otel-collector)              │               │                    │
└────────────────────────────────┼───────────────┼────────────────────┘
                                 │               │
         ┌───────────────────────┴───────────────┘
         │  POST /api/otel/v1/metrics (Bearer SP-token)
         ▼
┌────────────────────────────────────────────────────────────────────┐
│  FEVM Workspace                                                    │
│                                                                    │
│  ┌─── Cluster Manager App ───────────────────────────────────┐    │
│  │  1. Validate JWT (SP UUID in allowlist?)                   │    │
│  │  2. Parse OTLP metrics                                     │    │
│  │  3. Insert into Lakebase                                   │    │
│  │  4. Serve Live Dashboard                                   │    │
│  └───────────────────────────────────────────────────────────┘    │
│                                                                    │
│  Service Principal: 682e907b-...                                   │
│  (generates M2M OAuth tokens for OTel auth)                        │
└────────────────────────────────────────────────────────────────────┘
```

## Security Model

| Layer | Mechanism |
|-------|-----------|
| Secret storage | Databricks Secret Scope (not in code, not in env vars) |
| Authentication | M2M OAuth via Service Principal (client_credentials flow) |
| Authorization | SP UUID allowlist (`OTEL_ALLOWED_SP_IDS` in app.yaml) |
| Network | IP ACL on FEVM workspace (only client workspace NAT IPs allowed) |
| Token lifecycle | Auto-refresh every 50 min (tokens expire in 60 min) |

---

## Prerequisites

| Requirement | Details |
|-------------|---------|
| Cluster Manager app | Deployed on FEVM workspace |
| Service Principal | Created on FEVM workspace with OAuth secret |
| Databricks CLI | `databricks --version` ≥ 0.229.0 |
| UC Volume | For staging OTel binary |
| Workspace init script path | UC artifact allowlist may block Volume scripts |

---

## Step 1: Create Service Principal

The SP lives on the **FEVM workspace** (where the app is deployed). Client workspaces use it to authenticate.

```bash
# On FEVM workspace
FEVM_PROFILE="FEVM_SERVERLESS_STABLE"

# 1. Create SP (or use existing)
databricks service-principals create \
  --display-name "otel-collector-sp" \
  --profile "$FEVM_PROFILE"
# → Note the application_id (this is your SP_CLIENT_ID)
#   Example: 682e907b-4c32-463f-8b49-01617b942f17

# 2. Generate OAuth secret
# Go to: FEVM workspace → Settings → Identity and access → Service principals
# → Select "otel-collector-sp" → Secrets → Generate secret
# → Copy the SECRET value immediately (shown only once)
#
# Or via API:
databricks api post /api/2.0/token/create \
  --json '{"comment": "otel-collector", "lifetime_seconds": 0}' \
  --profile "$FEVM_PROFILE"
```

> **Important**: The OAuth secret is shown only once at creation. Store it immediately.

### What you'll have after this step:

| Value | Example |
|-------|---------|
| `SP_CLIENT_ID` | `682e907b-4c32-463f-8b49-01617b942f17` |
| `SP_CLIENT_SECRET` | `dose176dc48...` (long string) |
| `TOKEN_ENDPOINT` | `https://fevm-serverless-stable-3n0ihb.cloud.databricks.com/oidc/v1/token` |

---

## Step 2: Store Secret in Scope (on Client Workspace)

The SP secret must be accessible to cluster nodes at boot time. **Never hardcode it** in init scripts or env vars.

### Option A: Using manage.sh (recommended)

```bash
cd cluster_manager/otel/

# 1. Create config.env from template
cp config.env.example config.env

# 2. Edit config.env with your values
#    - FEVM_PROFILE, CLIENT_WORKSPACE_PROFILES
#    - SP_CLIENT_ID, TOKEN_ENDPOINT
#    - APP_URL, etc.

# 3. Store secret in scope (prompts for secret interactively)
./manage.sh secret-store
#   ✓ Scope 'otel-collector' exists (or created)
#   ✓ Secret stored: otel-collector/sp-client-secret
```

### Option B: Manual CLI

```bash
CLIENT_PROFILE="DEMO WEST"
SECRET_SCOPE="otel-collector"
SECRET_KEY="sp-client-secret"

# Create scope
databricks secrets create-scope "$SECRET_SCOPE" -p "$CLIENT_PROFILE"

# Store secret (paste when prompted)
databricks secrets put-secret "$SECRET_SCOPE" "$SECRET_KEY" -p "$CLIENT_PROFILE"
# → Paste your SP_CLIENT_SECRET value

# Verify
databricks secrets list-secrets "$SECRET_SCOPE" -p "$CLIENT_PROFILE"
# Should show: sp-client-secret
```

### How the init script uses it at runtime:

```
Cluster boots → init_script.sh runs on each node
  → Sees OTEL_SP_CLIENT_SECRET is placeholder
  → Gets node's token from /databricks/.credentials
  → Calls: GET ${DATABRICKS_HOST}/api/2.0/secrets/get?scope=otel-collector&key=sp-client-secret
  → Receives the actual SP secret
  → Uses it for M2M OAuth token generation
```

---

## Step 3: Stage OTel Collector Binary

Download and upload `otelcol-contrib` to a Unity Catalog Volume (faster startup than downloading from GitHub on each cluster start):

```bash
# Download for Linux x86_64 (cluster architecture)
VERSION=0.116.0
curl -L -o otelcol-contrib.tar.gz \
  "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${VERSION}/otelcol-contrib_${VERSION}_linux_amd64.tar.gz"

tar xzf otelcol-contrib.tar.gz otelcol-contrib

# Upload to Volume
databricks fs cp otelcol-contrib \
  dbfs:/Volumes/main/cluster_manager/binaries/otelcol-contrib \
  --profile "$CLIENT_PROFILE"

rm otelcol-contrib otelcol-contrib.tar.gz
```

> If no Volume binary found, the init script falls back to downloading from GitHub.

---

## Step 4: Deploy Init Script to Workspace

The init script must be accessible from the cluster at boot. Two deployment paths:

### Path A: Workspace file (recommended — bypasses UC artifact allowlist)

```bash
# Using manage.sh
./manage.sh deploy-script
#   ✓ Init script deployed to DEMO WEST

# Or manually:
databricks workspace import \
  "/Workspace/Users/you@databricks.com/init_scripts/init_otel_multinode.sh" \
  --file cluster_manager/otel/init_script.sh \
  --format AUTO --overwrite \
  --profile "$CLIENT_PROFILE"
```

### Path B: UC Volume

```bash
databricks fs cp cluster_manager/otel/init_script.sh \
  dbfs:/Volumes/main/cluster_manager/init_scripts/init_otel_multinode.sh \
  --profile "$CLIENT_PROFILE"
```

> **Note**: Some workspaces have UC Artifact Allowlist enabled which blocks init scripts from Volumes. Use workspace path in that case.

---

## Step 5: Configure Cluster

### Required Environment Variables

Set these on the cluster (via env vars, policy, or spark_env_vars):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OTEL_ENDPOINT` | **Yes** | — | App URL (e.g., `https://cluster-manager-xxx.aws.databricksapps.com`) |
| `OTEL_SP_CLIENT_ID` | **Yes** | — | Service Principal application ID |
| `OTEL_TOKEN_ENDPOINT` | **Yes** | — | FEVM workspace token endpoint |
| `OTEL_SECRET_SCOPE` | No | `otel-collector` | Secret scope name |
| `OTEL_SECRET_KEY` | No | `sp-client-secret` | Secret key within scope |
| `OTEL_SP_CLIENT_SECRET` | No | — | Direct secret (only for testing, prefer scope) |
| `OTEL_VOLUME_PATH` | No | `/Volumes/main/cluster_manager/binaries` | Binary location |
| `OTEL_INTERVAL` | No | `15s` | Collection interval |

### Option A: Cluster Policy (fleet-wide, recommended)

```json
{
  "init_scripts.0.workspace.destination": {
    "type": "fixed",
    "value": "/Workspace/Users/you@databricks.com/init_scripts/init_otel_multinode.sh",
    "hidden": true
  },
  "spark_env_vars.OTEL_ENDPOINT": {
    "type": "fixed",
    "value": "https://cluster-manager-7474645572615955.aws.databricksapps.com",
    "hidden": true
  },
  "spark_env_vars.OTEL_SP_CLIENT_ID": {
    "type": "fixed",
    "value": "682e907b-4c32-463f-8b49-01617b942f17",
    "hidden": true
  },
  "spark_env_vars.OTEL_TOKEN_ENDPOINT": {
    "type": "fixed",
    "value": "https://fevm-serverless-stable-3n0ihb.cloud.databricks.com/oidc/v1/token",
    "hidden": true
  }
}
```

Using `"hidden": true` prevents users from seeing or modifying these values.

The SP secret is **NOT in the policy** — it's fetched from Secret Scope at runtime.

### Option B: Per-Cluster (manual)

1. **Cluster → Advanced Options → Init Scripts**:
   ```
   Workspace: /Workspace/Users/you@databricks.com/init_scripts/init_otel_multinode.sh
   ```

2. **Cluster → Advanced Options → Spark → Environment Variables**:
   ```
   OTEL_ENDPOINT=https://cluster-manager-7474645572615955.aws.databricksapps.com
   OTEL_SP_CLIENT_ID=682e907b-4c32-463f-8b49-01617b942f17
   OTEL_TOKEN_ENDPOINT=https://fevm-serverless-stable-3n0ihb.cloud.databricks.com/oidc/v1/token
   ```

---

## Step 6: Add IP to FEVM Allowlist

The FEVM workspace has IP Access Control Lists. Client workspace cluster nodes must be able to reach the app.

```bash
# Find client workspace NAT IP
# Go to: Client workspace → Admin Console → Network → NAT Gateway
# Or check with your cloud provider's VPC NAT configuration

# Add to FEVM:
# FEVM workspace → Settings → Network → IP Access Lists → Add
# Label: "DEMO WEST NAT"
# IP: x.x.x.x/32
```

---

## Step 7: Bootstrap Lakebase (one-time after deploy)

The app needs a human user token to write to Lakebase (SPs can't directly write). Bootstrap caches this token.

```bash
# Via manage.sh
./manage.sh bootstrap
#   ✓ Bootstrap successful

# Or via browser: just visit the app URL (auto-bootstraps on first authenticated request)
```

---

## Step 8: Validate

```bash
# Full pipeline check
./manage.sh validate
#   ✓ Secret 'sp-client-secret' exists in scope 'otel-collector'
#   ✓ SP token generated (905 chars)
#   ✓ App endpoint reachable (HTTP 200)
#   ✓ Test metric accepted (HTTP 200)
#   ✓ All checks passed

# Or check status
./manage.sh status
```

### Verify on running cluster:

1. Start cluster with init script attached
2. Check cluster Event Log → Init Scripts tab for:
   ```
   OTel init: cluster=0610-085051-xos0wi6q node=ip-10-0-1-42 driver=true type=m5.xlarge
   Fetched SP secret from scope otel-collector
   Got OAuth token (905 chars)
   OTel Collector started with token refresh (PID 12345)
   ```
3. Open Live Metrics page in Cluster Manager app

---

## Secret Rotation

When rotating SP credentials:

```bash
# 1. Generate new secret in FEVM workspace UI
#    (Settings → Service Principals → otel-collector-sp → Secrets → Generate)

# 2. Store new secret in scope(s)
./manage.sh secret-rotate
# → Paste new secret when prompted

# 3. Validate
./manage.sh validate

# 4. Delete OLD secret from FEVM workspace UI
#    (only after validate passes)

# Running clusters will pick up new secret on next token refresh (within 50 min)
# or on cluster restart
```

---

## How It Works: Token Lifecycle

```
┌─────────────────────────────────────────────────────┐
│  Cluster Node Boot                                   │
│                                                     │
│  1. init_script.sh runs                             │
│  2. OTEL_SP_CLIENT_SECRET = placeholder             │
│  3. Fetch node token from /databricks/.credentials  │
│  4. GET secrets/get?scope=otel-collector            │
│     → Gets real SP secret                           │
│  5. POST {TOKEN_ENDPOINT}/oidc/v1/token             │
│     → client_credentials grant                      │
│     → Gets 60-min access token                      │
│  6. Start OTel Collector with Bearer token          │
│                                                     │
│  ┌─── Token Refresh Loop (every 50 min) ──────┐    │
│  │  - Request new token with same SP creds     │    │
│  │  - Update collector config                  │    │
│  │  - Restart collector process                │    │
│  └─────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────┘
```

---

## Backend Authorization Flow

When the app receives a metric push:

```python
# 1. Extract token from Authorization: Bearer <token>
# 2. Decode JWT payload (no signature verification — trust workspace OIDC)
# 3. Read 'sub' claim:
#    - Contains "@" → human user → ALLOW
#    - UUID format  → Service Principal → check allowlist
# 4. Compare against OTEL_ALLOWED_SP_IDS env var
#    - SP in list → ALLOW (HTTP 200)
#    - SP not in list → DENY (HTTP 403)
```

Add/remove SPs from `app.yaml`:
```yaml
- name: OTEL_ALLOWED_SP_IDS
  value: "682e907b-4c32-463f-8b49-01617b942f17,another-sp-uuid"
```

---

## Multi-Node Driver Detection

The init script runs on **every node** (driver + all workers). It detects role via:

1. `DB_IS_DRIVER` env var (set on DBR < 17)
2. Fallback: compare node IP with `spark.driver.host` in spark-defaults.conf (DBR 17+)

This metadata (`is_driver=true/false`) flows through to the dashboard for grouped display.

---

## Metrics Collected

| Metric | DB Column | Unit | Notes |
|--------|-----------|------|-------|
| CPU User | `cpu_user_percent` | % | Per-state from system.cpu.utilization |
| CPU System | `cpu_system_percent` | % | Kernel time |
| CPU Wait | `cpu_wait_percent` | % | I/O wait |
| Memory Used | `mem_used_percent` | % | From utilization or computed from usage |
| Memory Swap | `mem_swap_percent` | % | Paging utilization |
| Network TX | `network_sent_bytes` | bytes | Total bytes sent |
| Network RX | `network_received_bytes` | bytes | Total bytes received |
| Disk Used | `disk_used_percent` | % | Max across real filesystems |
| Load 1/5/15m | `load_1m/5m/15m` | avg | System load averages |

---

## Configuration Reference

### Init Script Variables

| Variable | Default | Required | Source |
|----------|---------|----------|--------|
| `OTEL_ENDPOINT` | placeholder | Yes | Cluster env / policy |
| `OTEL_SP_CLIENT_ID` | placeholder | Yes | Cluster env / policy |
| `OTEL_TOKEN_ENDPOINT` | placeholder | Yes | Cluster env / policy |
| `OTEL_SP_CLIENT_SECRET` | — | No* | Secret Scope (auto-fetched) |
| `OTEL_SECRET_SCOPE` | `otel-collector` | No | Cluster env / policy |
| `OTEL_SECRET_KEY` | `sp-client-secret` | No | Cluster env / policy |
| `OTEL_VOLUME_PATH` | `/Volumes/main/cluster_manager/binaries` | No | Cluster env |
| `OTEL_INTERVAL` | `15s` | No | Cluster env |

*Secret is auto-fetched from scope. Only set directly for testing.

### App Environment (app.yaml)

| Variable | Value | Description |
|----------|-------|-------------|
| `OTEL_AUTH_DISABLED` | `false` | Enable token + allowlist validation |
| `OTEL_ALLOWED_SP_IDS` | comma-separated UUIDs | SPs allowed to push metrics |

---

## Admin CLI (manage.sh)

```bash
cd cluster_manager/otel/
./manage.sh <command>

Commands:
  setup           Full wizard: scope + secret + deploy + validate
  secret-store    Create scope and store SP secret
  secret-rotate   Rotate to new SP secret
  deploy-script   Upload init script to workspace
  add-ip          Instructions for FEVM IP allowlist
  bootstrap       Cache user token for Lakebase writes
  validate        End-to-end pipeline verification
  status          Show current config state
```

Requires `config.env` (copy from `config.env.example`).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ERROR: No SP secret available` | Scope not created or key missing | Run `./manage.sh secret-store` |
| `ERROR: Failed to get OAuth token` | Wrong SP_CLIENT_ID or expired secret | Check ID, rotate secret |
| No data in dashboard | IP blocked by FEVM ACL | Add client workspace NAT IP |
| `HTTP 403: SP xxx not in allowlist` | New SP not added to app.yaml | Add UUID to `OTEL_ALLOWED_SP_IDS`, redeploy |
| `HTTP 401` from push endpoint | Malformed or expired token | Check token refresh loop in `/var/log/otelcol.log` |
| `HTTP 503: Lakebase not configured` | Missing LAKEBASE_* env vars in app | Check `app.yaml` env section |
| Stale metrics in dashboard | Token refresh failed silently | SSH to node, check `/var/log/otelcol.log` |
| Driver shows as worker | DBR 17+ + spark-defaults.conf not ready | Known limitation on fast-boot clusters |
| Binary download timeout | GitHub blocked or slow | Stage binary in UC Volume |

### Debug on cluster node:

```bash
# Check collector is running
cat /opt/otelcol/collector.pid | xargs ps

# Check logs
tail -50 /var/log/otelcol.log

# Test token generation manually
source /opt/otelcol/config.yaml  # won't work directly, but see the endpoint
curl -s -X POST "$OTEL_TOKEN_ENDPOINT" \
  -d "grant_type=client_credentials&client_id=$OTEL_SP_CLIENT_ID&client_secret=$OTEL_SP_CLIENT_SECRET&scope=all-apis"
```

---

## Automation Opportunities

| Task | Current | Can Automate |
|------|---------|-------------|
| SP creation | Manual UI | Databricks API / Terraform |
| Secret rotation | `./manage.sh secret-rotate` | Cron job + API |
| Init script deploy | `./manage.sh deploy-script` | Bundle deploy hook |
| IP allowlist | Manual | Terraform / API |
| Bootstrap | Browser / CLI | App startup hook |
| Policy attachment | Manual UI | Cluster policy API |
| Multi-workspace rollout | Sequential CLI | Parallel script / DAB |

See next section for automation implementation.
