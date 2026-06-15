"""Delta table writer for OTel node metrics.

Write pattern: incoming rows are buffered in memory and flushed to Delta
in a background task every FLUSH_INTERVAL_SECONDS (default 30s). This keeps
Delta commits infrequent and file sizes reasonable instead of generating a
new Parquet file per OTel push.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import Disposition, Format, StatementState

logger = logging.getLogger(__name__)

CATALOG = os.getenv("DELTA_CATALOG", "main")
SCHEMA = os.getenv("DELTA_SCHEMA", "cluster_manager")
TABLE = "node_metrics"
FULL_TABLE = f"`{CATALOG}`.`{SCHEMA}`.`{TABLE}`"

FLUSH_INTERVAL = int(os.getenv("FLUSH_INTERVAL_SECONDS", "30"))
FLUSH_MAX_ROWS = int(os.getenv("FLUSH_MAX_ROWS", "5000"))  # eager flush if buffer fills up

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

_CHUNK = 500  # rows per INSERT statement (larger chunks = fewer commits)


def _lit(val) -> str:
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
        raise RuntimeError("Warehouse still starting — will retry next flush")


def _write(rows: list[dict], ws: WorkspaceClient, warehouse_id: str) -> None:
    """Execute INSERT statements for a batch of rows."""
    col_list = ", ".join(_COLS)
    for i in range(0, len(rows), _CHUNK):
        chunk = rows[i : i + _CHUNK]
        values = ",\n  ".join(_row(r) for r in chunk)
        _run(ws, warehouse_id, f"INSERT INTO {FULL_TABLE} ({col_list})\nVALUES\n  {values}")


def ensure_schema(ws: WorkspaceClient, warehouse_id: str) -> None:
    _run(ws, warehouse_id, f"CREATE SCHEMA IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`")
    _run(ws, warehouse_id, SCHEMA_SQL)
    logger.info(f"Delta schema ready: {FULL_TABLE}")


# ── Write buffer ───────────────────────────────────────────────────────────────

class _WriteBuffer:
    """
    Accumulates incoming metric rows and flushes to Delta on a fixed interval.

    Keeps Delta commits infrequent (1 per FLUSH_INTERVAL instead of 1 per request),
    producing properly sized Parquet files and avoiding the small file problem.
    Rows are best-effort: if a flush fails they are logged and dropped rather than
    growing an unbounded retry queue.
    """

    def __init__(self):
        self._rows: list[dict] = []
        self._lock = threading.Lock()
        self._ws: WorkspaceClient | None = None
        self._warehouse_id: str | None = None

    def configure(self, ws: WorkspaceClient, warehouse_id: str) -> None:
        self._ws = ws
        self._warehouse_id = warehouse_id

    def add(self, rows: list[dict]) -> None:
        """Accept rows from a request handler — returns immediately, no I/O."""
        with self._lock:
            self._rows.extend(rows)
            should_flush = len(self._rows) >= FLUSH_MAX_ROWS

        if should_flush:
            # Eager synchronous flush when buffer is full
            self._flush_now()

    def drain(self) -> list[dict]:
        with self._lock:
            rows, self._rows = self._rows, []
            return rows

    def _flush_now(self) -> None:
        rows = self.drain()
        if not rows or not self._ws:
            return
        try:
            _write(rows, self._ws, self._warehouse_id)
            logger.info(f"Delta: flushed {len(rows)} rows to {FULL_TABLE}")
        except Exception as e:
            logger.error(f"Delta flush failed — {len(rows)} rows dropped: {e}")

    async def flush_loop(self) -> None:
        """Background task: flush buffer to Delta every FLUSH_INTERVAL seconds."""
        logger.info(f"Delta write buffer started (flush every {FLUSH_INTERVAL}s, max {FLUSH_MAX_ROWS} rows)")
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            if self._rows:
                # Run blocking I/O in thread pool so we don't block the event loop
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._flush_now)


buffer = _WriteBuffer()
