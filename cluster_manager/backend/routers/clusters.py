"""Cluster management API endpoints."""

from datetime import datetime, timezone
from typing import Annotated

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.compute import ClusterDetails, State
from fastapi import APIRouter, HTTPException, Query, Request

from ..core import Dependency, execute_sql, get_warehouse_id, logger
from ..models import (
    AutoScaleConfig,
    ClusterActionResponse,
    ClusterDetail,
    ClusterEvent,
    ClusterEventsResponse,
    ClusterMetricsPoint,
    ClusterMetricsResponse,
    ClusterSource,
    ClusterState,
    ClusterSummary,
    NodeMetricsSnapshot,
)
from ..workspace_registry import registry

router = APIRouter(prefix="/api/clusters", tags=["clusters"])


def _state_to_enum(state: State | None) -> ClusterState:
    """Convert SDK State to our ClusterState enum."""
    if state is None:
        return ClusterState.UNKNOWN
    try:
        return ClusterState(state.value)
    except ValueError:
        return ClusterState.UNKNOWN


def _source_to_enum(source: str | None) -> ClusterSource | None:
    """Convert SDK source string to our ClusterSource enum."""
    if source is None:
        return None
    try:
        return ClusterSource(source)
    except ValueError:
        return None


def _ms_to_datetime(ms: int | None) -> datetime | None:
    """Convert milliseconds timestamp to datetime."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _calculate_uptime_minutes(cluster: ClusterDetails) -> int:
    """Calculate cluster uptime in minutes."""
    if cluster.state not in [State.RUNNING, State.RESIZING, State.RESTARTING]:
        return 0
    if cluster.start_time is None:
        return 0
    start = _ms_to_datetime(cluster.start_time)
    if start is None:
        return 0
    now = datetime.now(timezone.utc)
    return int((now - start).total_seconds() / 60)


def _estimate_dbu_per_hour(cluster: ClusterDetails) -> float:
    """Estimate DBU per hour based on cluster configuration.

    This is a rough estimate. Actual DBU rates depend on instance types.
    """
    if cluster.state not in [State.RUNNING, State.RESIZING]:
        return 0.0

    # Base estimate: 1 DBU per worker per hour (rough approximation)
    num_workers = cluster.num_workers or 0
    if cluster.autoscale:
        # Use average of min/max for autoscaling clusters
        num_workers = (cluster.autoscale.min_workers + cluster.autoscale.max_workers) / 2

    # Add 1 for driver
    return (num_workers + 1) * 1.0


def _format_termination_reason(reason) -> str | None:
    """Format TerminationReason object to a readable string."""
    if reason is None:
        return None
    # TerminationReason has code, type, and parameters attributes
    parts = []
    if hasattr(reason, 'code') and reason.code:
        parts.append(str(reason.code.value) if hasattr(reason.code, 'value') else str(reason.code))
    if hasattr(reason, 'type') and reason.type:
        parts.append(str(reason.type.value) if hasattr(reason.type, 'value') else str(reason.type))
    if hasattr(reason, 'parameters') and reason.parameters:
        # Include key parameters like inactivity_duration
        for key, value in reason.parameters.items():
            parts.append(f"{key}={value}")
    return " - ".join(parts) if parts else None


def _cluster_to_summary(cluster: ClusterDetails) -> ClusterSummary:
    """Convert SDK ClusterDetails to our ClusterSummary model."""
    autoscale = None
    if cluster.autoscale:
        autoscale = AutoScaleConfig(
            min_workers=cluster.autoscale.min_workers,
            max_workers=cluster.autoscale.max_workers,
        )

    return ClusterSummary(
        cluster_id=cluster.cluster_id,
        cluster_name=cluster.cluster_name or "Unnamed Cluster",
        state=_state_to_enum(cluster.state),
        creator_user_name=cluster.creator_user_name,
        node_type_id=cluster.node_type_id,
        driver_node_type_id=cluster.driver_node_type_id,
        num_workers=cluster.num_workers,
        autoscale=autoscale,
        spark_version=cluster.spark_version,
        cluster_source=_source_to_enum(cluster.cluster_source.value if cluster.cluster_source else None),
        start_time=_ms_to_datetime(cluster.start_time),
        last_activity_time=_ms_to_datetime(getattr(cluster, 'last_activity_time', None)),
        uptime_minutes=_calculate_uptime_minutes(cluster),
        estimated_dbu_per_hour=_estimate_dbu_per_hour(cluster),
        policy_id=cluster.policy_id,
    )


def _cluster_to_detail(cluster: ClusterDetails) -> ClusterDetail:
    """Convert SDK ClusterDetails to our ClusterDetail model."""
    summary = _cluster_to_summary(cluster)

    # Use getattr for optional attributes that may not exist on all cluster types
    disk_spec = getattr(cluster, 'disk_spec', None)
    enable_elastic_disk = getattr(cluster, 'enable_elastic_disk', None)

    return ClusterDetail(
        **summary.model_dump(),
        terminated_time=_ms_to_datetime(cluster.terminated_time),
        termination_reason=_format_termination_reason(cluster.termination_reason),
        state_message=cluster.state_message,
        default_tags=cluster.default_tags or {},
        custom_tags=cluster.custom_tags or {},
        aws_attributes=cluster.aws_attributes.as_dict() if cluster.aws_attributes else None,
        azure_attributes=cluster.azure_attributes.as_dict() if cluster.azure_attributes else None,
        gcp_attributes=cluster.gcp_attributes.as_dict() if cluster.gcp_attributes else None,
        spark_conf=cluster.spark_conf or {},
        spark_env_vars=cluster.spark_env_vars or {},
        init_scripts=[s.as_dict() for s in (cluster.init_scripts or [])],
        cluster_log_conf=cluster.cluster_log_conf.as_dict() if cluster.cluster_log_conf else None,
        # policy_id already included from summary.model_dump()
        enable_elastic_disk=enable_elastic_disk,
        disk_spec=disk_spec.as_dict() if disk_spec else None,
        single_user_name=cluster.single_user_name,
        data_security_mode=cluster.data_security_mode.value if cluster.data_security_mode else None,
    )


@router.get("", response_model=list[ClusterSummary])
def list_clusters(
    ws: Dependency.Client,
    state: Annotated[ClusterState | None, Query(description="Filter by cluster state")] = None,
    cluster_ids: Annotated[list[str] | None, Query(description="Filter to specific cluster IDs")] = None,
    limit: Annotated[int, Query(ge=1, le=500, description="Maximum number of clusters to return")] = 100,
) -> list[ClusterSummary]:
    """List clusters from all registered workspaces.

    Aggregates clusters from the local workspace and all registered remote workspaces.
    Each cluster is tagged with its source workspace_name/workspace_url.
    """
    logger.info(f"Listing clusters - limit: {limit}, multi_workspace: {registry.is_multi_workspace}")

    summaries: list[ClusterSummary] = []

    if registry.is_multi_workspace:
        # Fetch from all registered workspaces in parallel
        results = registry.fetch_clusters_from_all(local_ws=ws)
        for ws_name, ws_url, cluster in results:
            summary = _cluster_to_summary(cluster)
            summary.workspace_name = ws_name
            summary.workspace_url = ws_url
            summaries.append(summary)
        logger.info(f"Aggregated {len(summaries)} clusters from {len(registry.entries) + 1} workspace(s)")
    else:
        # Single workspace mode (backward compatible)
        try:
            for i, cluster in enumerate(ws.clusters.list(page_size=limit)):
                if i >= limit:
                    break
                summaries.append(_cluster_to_summary(cluster))
            logger.info(f"Retrieved {len(summaries)} clusters from local workspace")
        except Exception as e:
            logger.error(f"Failed to list clusters: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to list clusters: {e}")

    # Filter by cluster_ids if provided
    if cluster_ids:
        id_set = set(cluster_ids)
        summaries = [s for s in summaries if s.cluster_id in id_set]

    # Filter by state if provided
    if state:
        summaries = [s for s in summaries if s.state == state]

    # Sort by state (running first) then by name
    state_order = {
        ClusterState.RUNNING: 0,
        ClusterState.PENDING: 1,
        ClusterState.RESTARTING: 2,
        ClusterState.RESIZING: 3,
        ClusterState.TERMINATING: 4,
        ClusterState.TERMINATED: 5,
        ClusterState.ERROR: 6,
        ClusterState.UNKNOWN: 7,
    }
    summaries.sort(key=lambda s: (state_order.get(s.state, 99), s.cluster_name))

    # Apply limit
    summaries = summaries[:limit]
    logger.info(f"Returning {len(summaries)} clusters")
    return summaries


def _resolve_ws(ws: WorkspaceClient, workspace_url: str | None) -> WorkspaceClient:
    """Resolve the correct workspace client (remote or local)."""
    if workspace_url:
        client = registry.get_client(workspace_url)
        if client:
            return client
        logger.warning(f"Workspace {workspace_url} not in registry, using local")
    return ws


@router.get("/{cluster_id}", response_model=ClusterDetail)
def get_cluster(
    cluster_id: str,
    ws: Dependency.Client,
    workspace_url: Annotated[str | None, Query(description="Source workspace URL")] = None,
) -> ClusterDetail:
    """Get detailed information about a specific cluster."""
    logger.info(f"Getting cluster {cluster_id} (workspace: {workspace_url})")
    target_ws = _resolve_ws(ws, workspace_url)

    try:
        cluster = target_ws.clusters.get(cluster_id)
        detail = _cluster_to_detail(cluster)
        detail.workspace_url = workspace_url
        if workspace_url:
            entry = registry.get_entry_by_url(workspace_url)
            detail.workspace_name = entry.name if entry else None
        return detail
    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e)
        logger.error(f"Failed to get cluster {cluster_id}: [{error_type}] {error_msg}")
        raise HTTPException(
            status_code=404,
            detail=f"Cluster not found: {cluster_id}. Error: {error_msg}"
        )


@router.post("/{cluster_id}/start", response_model=ClusterActionResponse)
def start_cluster(
    cluster_id: str,
    ws: Dependency.Client,
    workspace_url: Annotated[str | None, Query(description="Source workspace URL")] = None,
) -> ClusterActionResponse:
    """Start a terminated or stopped cluster."""
    logger.info(f"Starting cluster {cluster_id} (workspace: {workspace_url})")
    target_ws = _resolve_ws(ws, workspace_url)

    try:
        cluster = target_ws.clusters.get(cluster_id)
        if cluster.state == State.RUNNING:
            return ClusterActionResponse(
                success=True,
                message="Cluster is already running",
                cluster_id=cluster_id,
            )
        if cluster.state not in [State.TERMINATED, State.ERROR]:
            return ClusterActionResponse(
                success=False,
                message=f"Cannot start cluster in state: {cluster.state.value}",
                cluster_id=cluster_id,
            )

        target_ws.clusters.start(cluster_id)
        return ClusterActionResponse(
            success=True,
            message="Cluster start initiated",
            cluster_id=cluster_id,
        )
    except Exception as e:
        logger.error(f"Failed to start cluster {cluster_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{cluster_id}/stop", response_model=ClusterActionResponse)
def stop_cluster(
    cluster_id: str,
    ws: Dependency.Client,
    workspace_url: Annotated[str | None, Query(description="Source workspace URL")] = None,
) -> ClusterActionResponse:
    """Stop a running cluster."""
    logger.info(f"Stopping cluster {cluster_id} (workspace: {workspace_url})")
    target_ws = _resolve_ws(ws, workspace_url)

    try:
        cluster = target_ws.clusters.get(cluster_id)
        if cluster.state == State.TERMINATED:
            return ClusterActionResponse(
                success=True,
                message="Cluster is already stopped",
                cluster_id=cluster_id,
            )
        if cluster.state not in [State.RUNNING, State.PENDING, State.RESIZING, State.RESTARTING]:
            return ClusterActionResponse(
                success=False,
                message=f"Cannot stop cluster in state: {cluster.state.value}",
                cluster_id=cluster_id,
            )

        target_ws.clusters.delete(cluster_id)
        return ClusterActionResponse(
            success=True,
            message="Cluster stop initiated",
            cluster_id=cluster_id,
        )
    except Exception as e:
        logger.error(f"Failed to stop cluster {cluster_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{cluster_id}/events", response_model=ClusterEventsResponse)
def get_cluster_events(
    cluster_id: str,
    ws: Dependency.Client,
    workspace_url: Annotated[str | None, Query(description="Source workspace URL")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ClusterEventsResponse:
    """Get recent events for a cluster."""
    logger.info(f"Getting events for cluster {cluster_id} (workspace: {workspace_url})")
    target_ws = _resolve_ws(ws, workspace_url)

    try:
        events = []
        for i, event in enumerate(target_ws.clusters.events(cluster_id=cluster_id, page_size=limit)):
            if i >= limit:
                break
            events.append(ClusterEvent(
                cluster_id=cluster_id,
                timestamp=_ms_to_datetime(event.timestamp) or datetime.now(timezone.utc),
                event_type=event.type.value if event.type else "UNKNOWN",
                details=event.details.as_dict() if event.details else {},
            ))

        return ClusterEventsResponse(
            events=events,
            next_page_token=None,
            total_count=len(events),
        )
    except Exception as e:
        logger.error(f"Failed to get events for cluster {cluster_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{cluster_id}/metrics", response_model=ClusterMetricsResponse)
def get_cluster_metrics(
    cluster_id: str,
    ws: Dependency.Client,
    config: Dependency.Config,
    workspace_url: Annotated[str | None, Query(description="Source workspace URL")] = None,
    minutes: Annotated[int, Query(ge=5, le=360)] = 60,
) -> ClusterMetricsResponse:
    """Get live CPU and memory metrics from system.compute.node_timeline.

    Returns per-minute aggregated time series and current per-node breakdown.
    """
    logger.info(f"Getting {minutes}min metrics for cluster {cluster_id} (workspace: {workspace_url})")
    target_ws = _resolve_ws(ws, workspace_url)

    warehouse_id = get_warehouse_id(target_ws, config)

    sql = f"""
    SELECT
        start_time,
        instance_id,
        driver,
        cpu_user_percent,
        cpu_system_percent,
        cpu_wait_percent,
        mem_used_percent,
        network_sent_bytes,
        network_received_bytes,
        node_type
    FROM system.compute.node_timeline
    WHERE cluster_id = '{cluster_id}'
        AND start_time >= CURRENT_TIMESTAMP - INTERVAL {minutes} MINUTE
    ORDER BY start_time ASC
    """

    try:
        rows = execute_sql(target_ws, warehouse_id, sql)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Could not fetch metrics for {cluster_id}: {e}")
        return ClusterMetricsResponse(
            cluster_id=cluster_id, time_series=[], current_nodes=[], minutes=minutes
        )

    if not rows:
        return ClusterMetricsResponse(
            cluster_id=cluster_id, time_series=[], current_nodes=[], minutes=minutes
        )

    # Aggregate by timestamp
    from collections import defaultdict
    ts_buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        ts_buckets[row["start_time"]].append(row)

    time_series = []
    for ts_str, nodes in sorted(ts_buckets.items()):
        n = len(nodes)
        cpu_user = sum(float(r["cpu_user_percent"] or 0) for r in nodes) / n
        cpu_system = sum(float(r["cpu_system_percent"] or 0) for r in nodes) / n
        cpu_wait = sum(float(r["cpu_wait_percent"] or 0) for r in nodes) / n
        mem = sum(float(r["mem_used_percent"] or 0) for r in nodes) / n
        net_sent = sum(int(r["network_sent_bytes"] or 0) for r in nodes)
        net_recv = sum(int(r["network_received_bytes"] or 0) for r in nodes)

        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")) if isinstance(ts_str, str) else ts_str

        time_series.append(ClusterMetricsPoint(
            timestamp=ts,
            cpu_percent=round(cpu_user + cpu_system, 2),
            memory_percent=round(mem, 2),
            cpu_user_percent=round(cpu_user, 2),
            cpu_system_percent=round(cpu_system, 2),
            cpu_wait_percent=round(cpu_wait, 2),
            network_sent_bytes=net_sent,
            network_received_bytes=net_recv,
        ))

    # Current nodes = latest timestamp
    latest_ts = max(ts_buckets.keys())
    current_nodes = []
    for row in ts_buckets[latest_ts]:
        cpu_u = float(row["cpu_user_percent"] or 0)
        cpu_s = float(row["cpu_system_percent"] or 0)
        current_nodes.append(NodeMetricsSnapshot(
            instance_id=row["instance_id"] or "unknown",
            node_type=row["node_type"] or "unknown",
            is_driver=row["driver"] in (True, "true", "True"),
            cpu_percent=round(cpu_u + cpu_s, 2),
            memory_percent=round(float(row["mem_used_percent"] or 0), 2),
            network_sent_bytes=int(row["network_sent_bytes"] or 0),
            network_received_bytes=int(row["network_received_bytes"] or 0),
        ))

    # Sort: driver first, then by instance_id
    current_nodes.sort(key=lambda n: (not n.is_driver, n.instance_id))

    return ClusterMetricsResponse(
        cluster_id=cluster_id,
        time_series=time_series,
        current_nodes=current_nodes,
        minutes=minutes,
    )
