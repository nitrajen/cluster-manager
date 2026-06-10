"""OpenTelemetry metrics receiver endpoint.

Accepts OTLP/HTTP JSON payloads from OTel Collectors running on cluster nodes
and stores metrics in Lakebase for real-time dashboard queries.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from ..core import logger
from ..db import pool

router = APIRouter(prefix="/api/otel", tags=["otel"])


class OtelMetricsResponse(BaseModel):
    """OTLP ExportMetricsServiceResponse — empty on success."""
    pass


def _extract_resource_attributes(resource: dict) -> dict[str, str]:
    """Extract key-value pairs from OTLP resource attributes array."""
    attrs = {}
    for attr in resource.get("attributes", []):
        key = attr.get("key", "")
        value = attr.get("value", {})
        if "stringValue" in value:
            attrs[key] = value["stringValue"]
        elif "intValue" in value:
            attrs[key] = str(value["intValue"])
        elif "boolValue" in value:
            attrs[key] = str(value["boolValue"]).lower()
    return attrs


def _extract_gauge_value(data_point: dict) -> float | None:
    """Extract numeric value from a gauge/sum data point."""
    if "asDouble" in data_point:
        return data_point["asDouble"]
    if "asInt" in data_point:
        return float(data_point["asInt"])
    return None


def _timestamp_to_datetime(time_unix_nano: str | int) -> datetime:
    """Convert OTLP timeUnixNano to datetime."""
    nanos = int(time_unix_nano)
    return datetime.fromtimestamp(nanos / 1e9, tz=timezone.utc)


# Mapping from OTel metric names to our DB columns
METRIC_COLUMN_MAP = {
    "system.cpu.utilization": None,  # needs state attribute
    "system.cpu.time": None,  # needs state attribute
    "system.memory.utilization": "mem_used_percent",
    "system.paging.utilization": "mem_swap_percent",
    "system.network.io": None,  # needs direction attribute
    "system.disk.utilization": "disk_used_percent",
    "system.cpu.load_average.1m": "load_1m",
    "system.cpu.load_average.5m": "load_5m",
    "system.cpu.load_average.15m": "load_15m",
}

# CPU state mapping
CPU_STATE_COLUMN = {
    "user": "cpu_user_percent",
    "system": "cpu_system_percent",
    "wait": "cpu_wait_percent",
}

# Network direction mapping
NETWORK_DIR_COLUMN = {
    "transmit": "network_sent_bytes",
    "receive": "network_received_bytes",
}


def _get_attribute(data_point: dict, key: str) -> str | None:
    """Get attribute value from a data point's attributes."""
    for attr in data_point.get("attributes", []):
        if attr.get("key") == key:
            value = attr.get("value", {})
            if "stringValue" in value:
                return value["stringValue"]
    return None


def _parse_metrics_payload(payload: dict) -> list[dict]:
    """Parse OTLP JSON metrics into flat row dicts for insertion.

    Groups data points by (cluster_id, instance_id, timestamp) and builds
    one row per unique combination.
    """
    rows: dict[tuple, dict] = {}

    for resource_metrics in payload.get("resourceMetrics", []):
        resource = resource_metrics.get("resource", {})
        attrs = _extract_resource_attributes(resource)

        cluster_id = attrs.get("cluster_id", attrs.get("host.name", "unknown"))
        instance_id = attrs.get("instance_id", attrs.get("host.id", "unknown"))
        is_driver = attrs.get("is_driver", "false") == "true"
        node_type = attrs.get("node_type", "")

        for scope_metrics in resource_metrics.get("scopeMetrics", []):
            for metric in scope_metrics.get("metrics", []):
                name = metric.get("name", "")

                # Get data points from gauge or sum
                data_points = []
                if "gauge" in metric:
                    data_points = metric["gauge"].get("dataPoints", [])
                elif "sum" in metric:
                    data_points = metric["sum"].get("dataPoints", [])

                for dp in data_points:
                    ts_str = dp.get("timeUnixNano", dp.get("startTimeUnixNano", "0"))
                    ts = _timestamp_to_datetime(ts_str)
                    # Round to nearest second for grouping
                    ts_key = ts.replace(microsecond=0)
                    value = _extract_gauge_value(dp)
                    if value is None:
                        continue

                    row_key = (cluster_id, instance_id, ts_key)
                    if row_key not in rows:
                        rows[row_key] = {
                            "cluster_id": cluster_id,
                            "instance_id": instance_id,
                            "is_driver": is_driver,
                            "node_type": node_type,
                            "ts": ts_key,
                            "cpu_user_percent": None,
                            "cpu_system_percent": None,
                            "cpu_wait_percent": None,
                            "mem_used_percent": None,
                            "mem_swap_percent": None,
                            "network_sent_bytes": None,
                            "network_received_bytes": None,
                            "disk_used_percent": None,
                            "load_1m": None,
                            "load_5m": None,
                            "load_15m": None,
                        }

                    row = rows[row_key]

                    # CPU metrics need state attribute
                    if name in ("system.cpu.utilization", "system.cpu.time"):
                        state = _get_attribute(dp, "state")
                        col = CPU_STATE_COLUMN.get(state)
                        if col:
                            # utilization is 0-1, convert to percent
                            row[col] = value * 100 if value <= 1.0 else value

                    # Network needs direction attribute
                    elif name == "system.network.io":
                        direction = _get_attribute(dp, "direction")
                        col = NETWORK_DIR_COLUMN.get(direction)
                        if col:
                            row[col] = int(value)

                    # Memory utilization (0-1 → percent)
                    elif name == "system.memory.utilization":
                        row["mem_used_percent"] = value * 100 if value <= 1.0 else value

                    elif name == "system.paging.utilization":
                        row["mem_swap_percent"] = value * 100 if value <= 1.0 else value

                    elif name == "system.disk.utilization":
                        row["disk_used_percent"] = value * 100 if value <= 1.0 else value

                    elif name == "system.cpu.load_average.1m":
                        row["load_1m"] = value
                    elif name == "system.cpu.load_average.5m":
                        row["load_5m"] = value
                    elif name == "system.cpu.load_average.15m":
                        row["load_15m"] = value

    return list(rows.values())


def _batch_insert(rows: list[dict]):
    """Batch insert metric rows into Lakebase."""
    if not rows:
        return

    insert_sql = """
        INSERT INTO node_metrics (
            cluster_id, instance_id, is_driver, node_type, ts,
            cpu_user_percent, cpu_system_percent, cpu_wait_percent,
            mem_used_percent, mem_swap_percent,
            network_sent_bytes, network_received_bytes,
            disk_used_percent, load_1m, load_5m, load_15m
        ) VALUES (
            %(cluster_id)s, %(instance_id)s, %(is_driver)s, %(node_type)s, %(ts)s,
            %(cpu_user_percent)s, %(cpu_system_percent)s, %(cpu_wait_percent)s,
            %(mem_used_percent)s, %(mem_swap_percent)s,
            %(network_sent_bytes)s, %(network_received_bytes)s,
            %(disk_used_percent)s, %(load_1m)s, %(load_5m)s, %(load_15m)s
        )
    """

    with pool.get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(insert_sql, rows)
        conn.commit()


async def _validate_token(authorization: str | None) -> bool:
    """Validate Bearer token.

    In dev mode (OTEL_AUTH_DISABLED=true), accepts any non-empty token.
    In production, validates JWT format (3 dot-separated parts).
    """
    import os
    if os.getenv("OTEL_AUTH_DISABLED", "").lower() == "true":
        return bool(authorization)
    if not authorization:
        return False
    if not authorization.startswith("Bearer "):
        return False
    token = authorization[7:]
    parts = token.split(".")
    return len(parts) == 3


@router.post("/v1/metrics", response_model=OtelMetricsResponse)
async def receive_metrics(
    request: Request,
    authorization: str | None = Header(default=None),
    x_forwarded_access_token: str | None = Header(default=None, alias="X-Forwarded-Access-Token"),
):
    """Receive OTLP/HTTP JSON metrics from OTel Collectors on cluster nodes.

    Expects ExportMetricsServiceRequest JSON body.
    Validates OAuth bearer token and batch-inserts into Lakebase.
    """
    if not pool.is_configured:
        raise HTTPException(status_code=503, detail="Lakebase not configured")

    # Validate auth — accept either Authorization header (direct) or X-Forwarded-Access-Token (via app proxy)
    effective_auth = authorization or (f"Bearer {x_forwarded_access_token}" if x_forwarded_access_token else None)
    if not await _validate_token(effective_auth):
        raise HTTPException(status_code=401, detail="Invalid or missing authorization")

    # Parse body
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    # Parse and insert
    try:
        rows = _parse_metrics_payload(payload)
        if rows:
            _batch_insert(rows)
            logger.debug(f"OTel: inserted {len(rows)} metric rows")
    except Exception as e:
        logger.error(f"OTel insert error: {e}")
        raise HTTPException(status_code=500, detail=f"Storage error: {e}")

    return OtelMetricsResponse()
