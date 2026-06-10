# Plan: Central Admin Console — Live Metrics as Primary Hub

## Context

The cluster-manager app is pivoting from a multi-page cluster browser to a **central admin console** where **Live Metrics is the primary view**. The list of clusters currently reporting live metrics becomes the authoritative source for what data appears in Clusters, Optimization, and Policies pages.

Today each page fetches its own independent data (all clusters from workspace). After this change, a **shared monitored-clusters context** drives all pages, with a **cluster picker/filter** allowing per-cluster or multi-cluster views.

## Architecture Overview

```
┌──────────────────────────────────────────────────────┐
│  App Shell (_sidebar.tsx)                            │
│  ┌────────────────────────────────────────────────┐  │
│  │  MonitoredClustersProvider (React Context)     │  │
│  │  - Fetches live-metrics/active → cluster list  │  │
│  │  - Stores selection state (selected cluster IDs)│  │
│  │  - Provides: clusters, selected, setSelected   │  │
│  └──────────────────────┬─────────────────────────┘  │
│                         │                             │
│  ┌──────────────────────▼─────────────────────────┐  │
│  │  ClusterPicker (persistent toolbar component)  │  │
│  │  - Multi-select dropdown/chip filter           │  │
│  │  - "All clusters" / individual selection       │  │
│  │  - Persisted in URL search params              │  │
│  └──────────────────────┬─────────────────────────┘  │
│                         │                             │
│  ┌──────────────────────▼─────────────────────────┐  │
│  │  Page Content (per route)                      │  │
│  │  - Live Metrics: full view (landing page)      │  │
│  │  - Clusters: filtered by selected IDs          │  │
│  │  - Optimization: analysis for selected only    │  │
│  │  - Policies: policies governing selected       │  │
│  └────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
```

## Components to Build

### 1. MonitoredClustersContext (React Context + Provider)

**File:** `cluster_manager/ui/lib/monitored-clusters-context.tsx`

Responsibilities:
- Fetches `/api/live-metrics/active` on mount (same as Live Metrics page today)
- Stores full list of monitored cluster IDs + metadata (cluster_id, node_count, state)
- Stores **selection state**: `Set<cluster_id>` (default: all)
- Exposes: `monitoredClusters`, `selectedClusterIds`, `setSelectedClusterIds`, `isAllSelected`, `toggleCluster`, `selectAll`, `clearSelection`
- Auto-refreshes every 30s (slower than Live Metrics 15s — just for cluster list, not metrics)

```tsx
interface MonitoredClustersContextValue {
  clusters: ClusterLiveStatus[];
  isLoading: boolean;
  selectedIds: Set<string>;
  setSelectedIds: (ids: Set<string>) => void;
  toggleCluster: (id: string) => void;
  selectAll: () => void;
  clearSelection: () => void;
}
```

### 2. ClusterPicker Component

**File:** `cluster_manager/ui/components/cluster-picker.tsx`

UI behavior:
- Compact toolbar component shown below sidebar header on all pages
- Dropdown with checkboxes for each monitored cluster
- Shows "{N} of {total} clusters" summary when collapsed
- "All" toggle button for quick select/deselect all
- Search/filter within dropdown for large cluster lists
- Cluster items show: cluster_id (truncated), node_count, status dot (live/stale)
- Selection persisted to URL via TanStack Router search params (`?clusters=id1,id2`)

### 3. Backend: Filtered Endpoints

**File:** `cluster_manager/backend/routers/clusters.py` (modify)

Changes:
- `GET /api/clusters` accepts optional `cluster_ids` query param (comma-separated)
- When `cluster_ids` provided: return only those clusters (intersect with workspace clusters)
- When omitted: existing behavior (all clusters)

**File:** `cluster_manager/backend/routers/optimization.py` (modify)

Changes:
- All optimization endpoints accept optional `cluster_ids` filter
- `/api/optimization/oversized` → filter results to selected clusters only
- `/api/optimization/cost-recommendations` → same
- `/api/optimization/autoscaling` → same
- etc.

**File:** `cluster_manager/backend/routers/policies.py` (modify)

Changes:
- `GET /api/policies` accepts optional `cluster_ids` filter
- When provided: return only policies that are assigned to at least one of the selected clusters
- Need to cross-reference cluster→policy_id mapping

### 4. Frontend: Page Updates

#### Live Metrics (landing page)
**File:** `cluster_manager/ui/routes/_sidebar/live-metrics.tsx` (modify)

Changes:
- Consumes `MonitoredClustersContext` selection
- Highlights selected clusters in table
- Clicking cluster in picker syncs with detail view
- This page also populates the context (it's the data source)

#### Clusters Page
**File:** `cluster_manager/ui/routes/_sidebar/clusters/index.tsx` (modify)

Changes:
- Pass `selectedIds` from context to `useClusters({ cluster_ids: [...] })`
- When selection is empty/all → show all (existing behavior)
- Remove global policy filter (or keep as secondary filter within selected set)
- Add banner: "Showing {N} monitored clusters" with link back to Live Metrics

#### Optimization Page
**File:** `cluster_manager/ui/routes/_sidebar/optimization.tsx` (modify)

Changes:
- All hooks receive `cluster_ids` param from context
- Summary cards reflect only selected clusters
- Tab content filtered to selected set
- Header shows "Optimization — {N} clusters" or cluster name if single

#### Policies Page
**File:** `cluster_manager/ui/routes/_sidebar/policies.tsx` (modify)

Changes:
- `usePolicies({ cluster_ids: [...] })` → backend returns relevant policies
- When single cluster selected: show its policy detail directly
- When multiple: show policies with cluster count badge

### 5. Sidebar Navigation Update

**File:** `cluster_manager/ui/routes/_sidebar.tsx` (modify)

Changes:
- Reorder: Live Metrics → first item (primary/landing)
- Set default route to `/live-metrics` instead of `/clusters`
- Add ClusterPicker component between nav items and page content
- Visual emphasis on Live Metrics nav item (bold, primary color)

### 6. API Hook Updates

**File:** `cluster_manager/ui/lib/api.ts` (modify)

Changes to existing hooks:
- `useClusters(opts?: { cluster_ids?: string[] })` — pass filter to backend
- `useOversizedClusters(cluster_ids?)` — same pattern for all optimization hooks
- `usePolicies(cluster_ids?)` — filter param
- New hook: `useMonitoredClusters()` — wraps `useLiveActiveClusters` with longer stale time

## Implementation Order

1. **MonitoredClustersContext** → verify: context renders, provides cluster list
2. **ClusterPicker component** → verify: renders in sidebar, selection works
3. **Backend filter params** → verify: `/api/clusters?cluster_ids=x,y` returns filtered
4. **API hook updates** → verify: hooks pass filter, queries refetch on change
5. **Sidebar reorder + default route** → verify: app opens to Live Metrics
6. **Live Metrics integration** → verify: selection syncs with picker
7. **Clusters page** → verify: shows only selected clusters
8. **Optimization page** → verify: recommendations scoped to selected
9. **Policies page** → verify: policies filtered by cluster selection

## Key Decisions

- **Context over URL-only state**: Context shares selection across pages without prop drilling. URL params for deep-linking/bookmarking.
- **"All" default**: When no explicit selection, show everything (backward compat). Picker shows "All clusters" state.
- **Backend filtering**: Server-side filtering for Optimization (SQL-based) and Policies (policy→cluster mapping). Client-side filtering for Clusters (already fetched in full).
- **Live Metrics as source of truth**: Only clusters actively reporting OTel metrics appear in picker. Non-monitored clusters accessible via "Show all workspace clusters" escape hatch.
- **No breaking changes**: All filter params optional. Existing behavior preserved when params omitted.

## UX Considerations

- Picker visible on all pages but non-intrusive (collapsed to one line)
- Selection persists across page navigation (context + URL sync)
- Empty state: "No clusters monitored yet — deploy OTel to get started"
- Single-cluster mode: pages show cluster name in header, detail-oriented view
- Quick actions from picker: jump to Live Metrics detail for a cluster

## Out of Scope (Future)

- User-specific saved filter presets
- Cluster groups/tags for bulk selection
- Cross-workspace monitoring
- Historical playback (replay metrics from a time window)
