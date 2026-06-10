# Plan: Multi-Workspace Cluster Registry

## Context

App deployed on FEVM workspace — has zero clusters. Real clusters live on client workspaces (DEMO WEST, etc). Currently clusters.py uses single WorkspaceClient pointing at FEVM → empty list.

Goal: Registered workspaces auto-discovered from OTel init script setup. FEVM app pulls cluster data from all registered workspaces.

## Architecture

```
Client Workspace (DEMO WEST)              FEVM App (deployed here)
┌───────────────────────────┐            ┌─────────────────────────────┐
│ Cluster + OTel init script│──metrics──▶│ /api/otel/v1/metrics        │
│                           │            │                             │
│ SP with clusters:read     │◀──poll─────│ WorkspaceClient(host, SP)   │
└───────────────────────────┘            │                             │
                                         │ Lakebase: workspaces table  │
                                         └─────────────────────────────┘
```

## Approach: ENV-based MVP

Store workspace credentials as JSON in app env var (encrypted at rest by Databricks Apps). Avoids secret scope complexity for v1.

```yaml
# app.yaml (value set via `databricks apps update` at deploy, not committed)
- name: REGISTERED_WORKSPACES
  value: ""
```

Runtime format (JSON array):
```json
[
  {
    "url": "https://demo-west.cloud.databricks.com",
    "name": "DEMO WEST",
    "client_id": "682e907b-...",
    "client_secret": "dose...",
    "token_endpoint": "https://demo-west.cloud.databricks.com/oidc/v1/token"
  }
]
```

Same SP already used for OTel (already has workspace access). Just needs `clusters:read` permission added.

## Implementation

### 1. Create `cluster_manager/backend/workspace_registry.py`

```python
class WorkspaceRegistry:
    """Manages M2M OAuth connections to registered client workspaces."""

    _workspaces: list[dict]  # parsed from REGISTERED_WORKSPACES env
    _tokens: dict[str, tuple[str, float]]  # url → (token, expiry)

    def load_from_env(self):
        """Parse REGISTERED_WORKSPACES JSON from env."""

    def get_client(self, workspace_url: str) -> WorkspaceClient:
        """Get authenticated client for workspace (M2M OAuth, cached token)."""

    def list_all_clusters(self) -> list[tuple[dict, ClusterDetails]]:
        """Aggregate clusters from all active workspaces, parallel with timeout."""

    @property
    def workspaces(self) -> list[dict]:
        """List registered workspaces (no secrets)."""
```

Token fetch reuses same M2M OAuth pattern as init script (client_credentials grant).

### 2. Modify `cluster_manager/backend/routers/clusters.py`

- `list_clusters` → query local workspace (FEVM) + all registered workspaces
- Each cluster tagged with `workspace_name` and `workspace_url`
- If no registered workspaces, falls back to current single-workspace behavior
- Parallel fetch with 10s timeout per workspace; failures logged, don't break response

### 3. Modify `cluster_manager/backend/models.py`

Add to ClusterSummary:
```python
workspace_name: str | None = None   # "DEMO WEST" or None for local
workspace_url: str | None = None    # workspace URL for routing actions
```

### 4. Modify `cluster_manager/backend/core.py`

Initialize registry in `_default_lifespan`:
```python
from .workspace_registry import registry
registry.load_from_env()
app.state.workspace_registry = registry
```

### 5. Route actions to correct workspace

`get_cluster`, `start_cluster`, `stop_cluster`, `get_cluster_events` need workspace routing:
- Add optional query param `?workspace_url=...`
- If provided, use registry client; otherwise use local FEVM client
- Frontend passes `workspace_url` from ClusterSummary when navigating to detail

### 6. Add `GET /api/workspaces` endpoint

Simple endpoint listing registered workspaces (name, url, last_poll_status). No secrets exposed.

### 7. Frontend update

- Cluster list: show workspace badge/pill per cluster
- Cluster detail/actions: pass `workspace_url` in API calls

## Files to Create/Modify

| File | Action |
|------|--------|
| `cluster_manager/backend/workspace_registry.py` | Create — M2M OAuth multi-workspace client |
| `cluster_manager/backend/routers/clusters.py` | Modify — aggregate from registry |
| `cluster_manager/backend/models.py` | Modify — add workspace fields to ClusterSummary |
| `cluster_manager/backend/core.py` | Modify — init registry in lifespan |
| `cluster_manager/backend/routers/workspace.py` | Create — add /api/workspaces list |
| `app.yaml` | Modify — add REGISTERED_WORKSPACES env var (empty default) |
| `cluster_manager/ui/routes/_sidebar/clusters/index.tsx` | Modify — workspace badge on clusters |
| `cluster_manager/ui/lib/api.ts` | Modify — pass workspace_url param |

## Verification

1. Set `REGISTERED_WORKSPACES` with DEMO WEST creds in app.yaml
2. Deploy → `/api/clusters` returns DEMO WEST clusters
3. Cluster detail page works (routes to correct workspace)
4. Start/stop works on remote workspace cluster
5. UI shows "DEMO WEST" badge per cluster
6. Graceful degradation: remove creds → falls back to empty (FEVM only)
