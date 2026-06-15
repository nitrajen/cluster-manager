"""Delta table writer for OTel node metrics via Databricks SQL Warehouse."""
from __future__ import annotations

import logging
import os
from datetime import datetime

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import Disposition, Format, StatementState

logger = logging.getLogger(__name__)

CATALOG = os.getenv("DELTA_CATALOG", "main")
SCHEMA = os.getenv("DELTA_SCHEMA", "cluster_manager")
TABLE = "node_metrics"
FULL_TABLE = f"`{CATALOG}`.`{SCHEMA}`.`{TABLE}`"

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {FULL_TABLE} (
    cluster_id              STRING    NOT NULL,
    instance_id             STRING    NOT NULL,
    is_driver               BOOLEAN   NOT NULL,
    node_type               STRING,
    ts                      TIMESTAMP NOT NULL,
    cpu_user_percent        DOUBLE,
    cpu_system_percent      DOUBLE,
    cpu_wait_percent        DOUBLE,
    mem_used_percent        DOUBLE,
    mem_swap_percent        DOUBLE,
    mem_available_bytes     BIGINT,
    network_sent_bytes      BIGINT,
    network_received_bytes  BIGINT,
    network_errors          BIGINT,
    network_drops           BIGINT,
    disk_used_percent       DOUBLE,
    disk_io_time_ms         DOUBLE,
    disk_ops_read           BIGINT,
    disk_ops_write          BIGINT,
    load_1m                 DOUBLE,
    load_5m                 DOUBLE,
    load_15m                DOUBLE,
    paging_in               BIGINT,
    paging_out              BIGINT,
    process_count           BIGINT,
    inodes_used_percent     DOUBLE
)
USING DELTA
PARTITIONED BY (DATE(ts))
"""

_COLS = [
    "cluster_id", "instance_id", "is_driver", "node_type", "ts",
    "cpu_user_percent", "cpu_system_percent", "cpu_wait_percent",
    "mem_used_percent", "mem_swap_percent", "mem_available_bytes",
    "network_sent_bytes", "network_received_bytes", "network_errors", "network_drops",
    "disk_used_percent", "disk_io_time_ms", "disk_ops_read", "disk_ops_write",
    "load_1m", "load_5m", "load_15m",
    "paging_in", "paging_out", "process_count", "inodes_used_percent",
]

_CHUNK = 100  # rows per INSERT statement


def _lit(val) -> str:
    """Render a Python value as a Spark SQL literal."""
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, datetime):
        return f"TIMESTAMP '{val.strftime('%Y-%m-%d %H:%M:%S')}'"
    if isinstance(val, str):
        return "'" + val.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return str(val)


def _row(r: dict) -> str:
    return "(" + ", ".join(_lit(r.get(c)) for c in _COLS) + ")"


def _run(ws: WorkspaceClient, warehouse_id: str, sql: str) -> None:
    resp = ws.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=sql,
        format=Format.JSON_ARRAY,
        disposition=Disposition.INLINE,
        wait_timeout="50s",
    )
    if resp.status.state == StatementState.FAILED:
        msg = resp.status.error.message if resp.status.error else "unknown"
        raise RuntimeError(f"SQL failed: {msg}")
    if resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        raise RuntimeError("Warehouse still starting — OTel collector will retry")


def ensure_schema(ws: WorkspaceClient, warehouse_id: str) -> None:
    _run(ws, warehouse_id, f"CREATE SCHEMA IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`")
    _run(ws, warehouse_id, SCHEMA_SQL)
    logger.info(f"Delta schema ready: {FULL_TABLE}")


def insert(rows: list[dict], ws: WorkspaceClient, warehouse_id: str) -> None:
    if not rows:
        return
    col_list = ", ".join(_COLS)
    for i in range(0, len(rows), _CHUNK):
        chunk = rows[i : i + _CHUNK]
        values = ",\n  ".join(_row(r) for r in chunk)
        _run(ws, warehouse_id, f"INSERT INTO {FULL_TABLE} ({col_list})\nVALUES\n  {values}")
    logger.debug(f"Delta: inserted {len(rows)} rows into {FULL_TABLE}")
