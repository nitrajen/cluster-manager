"""Live metrics API endpoints — query real-time OTel data from Lakebase."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from ..core import logger
from ..db import pool


def _get_conn_fallback(user_token: str | None = None):
    """Get a DB connection — pool first, then direct with user token."""
    if pool._pool:
        return pool.get_conn()
    if user_token:
        return pool.get_conn_with_token(user_token)
    return pool.get_conn()  # will raise if pool unavailable

router = APIRouter(prefix="/api/live-metrics", tags=["live-metrics"])


class NodeMetric(BaseModel):
    cluster_id: str
    instance_id: str
    is_driver: bool
    node_type: str | None
    ts: datetime
    cpu_user_percent: float | None
    cpu_system_percent: float | None
    cpu_wait_percent: float | None
    mem_used_percent: float | None
    mem_swap_percent: float | None
    network_sent_bytes: int | None
    network_received_bytes: int | None
    disk_used_percent: float | None
    load_1m: float | None
    load_5m: float | None
    load_15m: float | None


class ClusterLiveStatus(BaseModel):
    cluster_id: str
    node_count: int
    latest_ts: datetime
    avg_cpu: float | None
    avg_mem: float | None
    max_cpu: float | None
    max_mem: float | None
    is_stale: bool  # No data in last 2 min


class LiveAlert(BaseModel):
    cluster_id: str
    instance_id: str
    is_driver: bool
    alert_type: str  # "high_cpu", "high_memory", "high_disk"
    value: float
    threshold: float
    ts: datetime


def _rows_to_dicts(cursor) -> list[dict]:
    """Convert cursor results to list of dicts."""
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


@router.get("/active", response_model=list[ClusterLiveStatus])
def get_active_clusters(
    x_forwarded_access_token: str | None = Header(default=None, alias="X-Forwarded-Access-Token"),
):
    """List clusters currently reporting live metrics."""
    if not pool.is_configured:
        return []

    # Cache user token and bootstrap Lakebase pool if not ready
    if x_forwarded_access_token:
        pool.cache_user_token(x_forwarded_access_token)
        if not pool._pool:
            try:
                pool._ensure_token()
            except Exception:
                pass

    sql = """
        WITH recent AS (
            SELECT cluster_id, instance_id,
                   cpu_user_percent, cpu_system_percent, mem_used_percent, ts
            FROM node_metrics
            WHERE ts > NOW() - INTERVAL '30 minutes'
        ),
        per_node AS (
            SELECT cluster_id, instance_id,
                   MAX(ts) as latest_ts,
                   MAX(cpu_user_percent) FILTER (WHERE cpu_user_percent IS NOT NULL) as cpu_user,
                   MAX(cpu_system_percent) FILTER (WHERE cpu_system_percent IS NOT NULL) as cpu_sys,
                   MAX(mem_used_percent) FILTER (WHERE mem_used_percent IS NOT NULL) as mem_used
            FROM recent
            GROUP BY cluster_id, instance_id
        )
        SELECT
            cluster_id,
            COUNT(*) as node_count,
            MAX(latest_ts) as latest_ts,
            AVG(COALESCE(cpu_user, 0) + COALESCE(cpu_sys, 0)) as avg_cpu,
            AVG(mem_used) as avg_mem,
            MAX(COALESCE(cpu_user, 0) + COALESCE(cpu_sys, 0)) as max_cpu,
            MAX(mem_used) as max_mem
        FROM per_node
        GROUP BY cluster_id
        ORDER BY latest_ts DESC
    """
    try:
        with _get_conn_fallback(x_forwarded_access_token) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = _rows_to_dicts(cur)
    except Exception as e:
        logger.error(f"Live metrics query error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    now = datetime.now(timezone.utc)
    results = []
    for row in rows:
        latest_ts = row["latest_ts"]
        if latest_ts.tzinfo is None:
            latest_ts = latest_ts.replace(tzinfo=timezone.utc)
        stale = (now - latest_ts).total_seconds() > 120

        results.append(ClusterLiveStatus(
            cluster_id=row["cluster_id"],
            node_count=row["node_count"],
            latest_ts=latest_ts,
            avg_cpu=round(row["avg_cpu"], 1) if row["avg_cpu"] else None,
            avg_mem=round(row["avg_mem"], 1) if row["avg_mem"] else None,
            max_cpu=round(row["max_cpu"], 1) if row["max_cpu"] else None,
            max_mem=round(row["max_mem"], 1) if row["max_mem"] else None,
            is_stale=stale,
        ))

    return results


@router.get("/{cluster_id}", response_model=list[NodeMetric])
def get_cluster_metrics(
    cluster_id: str,
    x_forwarded_access_token: str | None = Header(default=None, alias="X-Forwarded-Access-Token"),
):
    """Get latest metrics for all nodes in a cluster (last 5 minutes)."""
    if not pool.is_configured:
        return []

    sql = """
        SELECT * FROM node_metrics
        WHERE cluster_id = %s AND ts > NOW() - INTERVAL '5 minutes'
        ORDER BY ts DESC
        LIMIT 200
    """
    try:
        with _get_conn_fallback(x_forwarded_access_token) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (cluster_id,))
                rows = _rows_to_dicts(cur)
    except Exception as e:
        logger.error(f"Live metrics query error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return [NodeMetric(**row) for row in rows]


@router.get("/{cluster_id}/history", response_model=list[NodeMetric])
def get_cluster_history(
    cluster_id: str,
    minutes: Annotated[int, Query(ge=1, le=1440)] = 60,
    x_forwarded_access_token: str | None = Header(default=None, alias="X-Forwarded-Access-Token"),
):
    """Get time-series metrics for a cluster over a time window."""
    if not pool.is_configured:
        return []

    sql = """
        SELECT * FROM node_metrics
        WHERE cluster_id = %s AND ts > NOW() - make_interval(mins => %s)
        ORDER BY ts ASC
        LIMIT 5000
    """
    try:
        with _get_conn_fallback(x_forwarded_access_token) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (cluster_id, minutes))
                rows = _rows_to_dicts(cur)
    except Exception as e:
        logger.error(f"Live metrics history error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return [NodeMetric(**row) for row in rows]


@router.get("/alerts", response_model=list[LiveAlert])
def get_alerts(
    x_forwarded_access_token: str | None = Header(default=None, alias="X-Forwarded-Access-Token"),
):
    """Get nodes exceeding resource thresholds in the last 5 minutes."""
    if not pool.is_configured:
        return []

    cpu_threshold = 80.0
    mem_threshold = 90.0
    disk_threshold = 90.0

    sql = """
        WITH latest AS (
            SELECT *,
                   ROW_NUMBER() OVER (PARTITION BY cluster_id, instance_id ORDER BY ts DESC) as rn
            FROM node_metrics
            WHERE ts > NOW() - INTERVAL '5 minutes'
        )
        SELECT cluster_id, instance_id, is_driver, ts,
               COALESCE(cpu_user_percent, 0) + COALESCE(cpu_system_percent, 0) as total_cpu,
               mem_used_percent, disk_used_percent
        FROM latest
        WHERE rn = 1
          AND (
            (COALESCE(cpu_user_percent, 0) + COALESCE(cpu_system_percent, 0)) > %s
            OR mem_used_percent > %s
            OR disk_used_percent > %s
          )
        ORDER BY ts DESC
    """
    try:
        with _get_conn_fallback(x_forwarded_access_token) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (cpu_threshold, mem_threshold, disk_threshold))
                rows = _rows_to_dicts(cur)
    except Exception as e:
        logger.error(f"Live alerts query error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    alerts = []
    for row in rows:
        ts = row["ts"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        if row["total_cpu"] and row["total_cpu"] > cpu_threshold:
            alerts.append(LiveAlert(
                cluster_id=row["cluster_id"],
                instance_id=row["instance_id"],
                is_driver=row["is_driver"],
                alert_type="high_cpu",
                value=round(row["total_cpu"], 1),
                threshold=cpu_threshold,
                ts=ts,
            ))
        if row["mem_used_percent"] and row["mem_used_percent"] > mem_threshold:
            alerts.append(LiveAlert(
                cluster_id=row["cluster_id"],
                instance_id=row["instance_id"],
                is_driver=row["is_driver"],
                alert_type="high_memory",
                value=round(row["mem_used_percent"], 1),
                threshold=mem_threshold,
                ts=ts,
            ))
        if row["disk_used_percent"] and row["disk_used_percent"] > disk_threshold:
            alerts.append(LiveAlert(
                cluster_id=row["cluster_id"],
                instance_id=row["instance_id"],
                is_driver=row["is_driver"],
                alert_type="high_disk",
                value=round(row["disk_used_percent"], 1),
                threshold=disk_threshold,
                ts=ts,
            ))

    return alerts
