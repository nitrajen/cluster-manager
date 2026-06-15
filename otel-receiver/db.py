"""Lakebase (PostgreSQL) connection pool for OTel metrics storage."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.pool

logger = logging.getLogger(__name__)

LAKEBASE_HOST = os.getenv("LAKEBASE_HOST", "")
LAKEBASE_DATABASE = os.getenv("LAKEBASE_DATABASE", "otel_metrics")
LAKEBASE_USER = os.getenv("LAKEBASE_USER", "")
LAKEBASE_PROJECT = os.getenv("LAKEBASE_PROJECT", "cluster-metrics")
LAKEBASE_BRANCH = os.getenv("LAKEBASE_BRANCH", "production")
LAKEBASE_ENDPOINT = os.getenv("LAKEBASE_ENDPOINT", "primary")

TOKEN_REFRESH_INTERVAL = 50 * 60  # 50 min; tokens last 60

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS node_metrics (
    id                      BIGSERIAL PRIMARY KEY,
    cluster_id              VARCHAR(64)  NOT NULL,
    instance_id             VARCHAR(64)  NOT NULL,
    is_driver               BOOLEAN      NOT NULL DEFAULT FALSE,
    node_type               VARCHAR(64),
    ts                      TIMESTAMPTZ  NOT NULL,
    cpu_user_percent        DOUBLE PRECISION,
    cpu_system_percent      DOUBLE PRECISION,
    cpu_wait_percent        DOUBLE PRECISION,
    mem_used_percent        DOUBLE PRECISION,
    mem_swap_percent        DOUBLE PRECISION,
    mem_available_bytes     BIGINT,
    network_sent_bytes      BIGINT,
    network_received_bytes  BIGINT,
    network_errors          BIGINT,
    network_drops           BIGINT,
    disk_used_percent       DOUBLE PRECISION,
    disk_io_time_ms         DOUBLE PRECISION,
    disk_ops_read           BIGINT,
    disk_ops_write          BIGINT,
    load_1m                 DOUBLE PRECISION,
    load_5m                 DOUBLE PRECISION,
    load_15m                DOUBLE PRECISION,
    paging_in               BIGINT,
    paging_out              BIGINT,
    process_count           BIGINT,
    inodes_used_percent     DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_node_metrics_cluster_time
    ON node_metrics (cluster_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_node_metrics_time
    ON node_metrics (ts DESC);
"""


class LakebasePool:
    def __init__(self):
        self._pool: psycopg2.pool.ThreadedConnectionPool | None = None
        self._token: str | None = None
        self._token_expiry: float = 0
        self._lock = threading.Lock()
        self._host = LAKEBASE_HOST
        self._database = LAKEBASE_DATABASE
        self._user = LAKEBASE_USER
        self._cached_user_token: str | None = None

    def _generate_token(self) -> str:
        # Fast path: static token set by admin (e.g. PAT in env)
        static = os.getenv("LAKEBASE_TOKEN", "")
        if static:
            return static

        endpoint_path = (
            f"projects/{LAKEBASE_PROJECT}/branches/{LAKEBASE_BRANCH}"
            f"/endpoints/{LAKEBASE_ENDPOINT}"
        )

        # Try Databricks SDK (works when app runs with SP that has access)
        try:
            from databricks.sdk import WorkspaceClient
            ws = WorkspaceClient()
            resp = ws.api_client.do(
                "POST",
                "/api/2.0/postgres/credentials",
                body={"endpoint": endpoint_path},
            )
            if isinstance(resp, dict):
                return resp.get("token", resp.get("password", ""))
        except Exception as e:
            logger.warning(f"SDK token generation failed: {e}")

        # Fall back to cached human user token (set via /api/otel/bootstrap)
        if self._cached_user_token:
            return self._cached_user_token

        raise RuntimeError(
            "Cannot generate Lakebase token. Call /api/otel/bootstrap with a user token first."
        )

    def cache_user_token(self, token: str) -> None:
        """Cache a human user token for Lakebase auth. Called by /bootstrap."""
        self._cached_user_token = token
        try:
            parts = token.split(".")
            if len(parts) == 3:
                import base64
                payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
                claims = json.loads(base64.b64decode(payload))
                sub = claims.get("sub", "")
                if "@" in sub and not self._user:
                    self._user = sub
        except Exception:
            pass
        # Force pool recreation with new token
        self._token_expiry = 0
        logger.info("Cached user token for Lakebase")

    def _ensure_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expiry:
            return self._token
        with self._lock:
            if self._token and now < self._token_expiry:
                return self._token
            self._token = self._generate_token()
            self._token_expiry = now + TOKEN_REFRESH_INTERVAL
            self._recreate_pool()
            return self._token

    def _recreate_pool(self) -> None:
        if self._pool:
            try:
                self._pool.closeall()
            except Exception:
                pass
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=10,
            host=self._host, port=5432,
            database=self._database,
            user=self._user,
            password=self._token,
            sslmode="require",
            connect_timeout=10,
        )
        logger.info(f"Lakebase pool ready: {self._host}/{self._database}")

    def initialize(self, host: str | None = None, user: str | None = None) -> None:
        if host:
            self._host = host
        if user:
            self._user = user
        if not self._host or not self._user:
            logger.warning("Lakebase not configured (LAKEBASE_HOST / LAKEBASE_USER missing)")
            return
        self._ensure_token()

    def ensure_schema(self) -> None:
        if not self._pool:
            return
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()
        logger.info("node_metrics schema ready")

    @contextmanager
    def get_conn(self):
        self._ensure_token()
        if not self._pool:
            raise RuntimeError("Lakebase pool unavailable")
        conn = self._pool.getconn()
        try:
            yield conn
        finally:
            self._pool.putconn(conn)

    def insert(self, rows: list[dict], user_token: str | None = None) -> None:
        if not rows:
            return

        sql = """
            INSERT INTO node_metrics (
                cluster_id, instance_id, is_driver, node_type, ts,
                cpu_user_percent, cpu_system_percent, cpu_wait_percent,
                mem_used_percent, mem_swap_percent, mem_available_bytes,
                network_sent_bytes, network_received_bytes, network_errors, network_drops,
                disk_used_percent, disk_io_time_ms, disk_ops_read, disk_ops_write,
                load_1m, load_5m, load_15m,
                paging_in, paging_out, process_count, inodes_used_percent
            ) VALUES (
                %(cluster_id)s, %(instance_id)s, %(is_driver)s, %(node_type)s, %(ts)s,
                %(cpu_user_percent)s, %(cpu_system_percent)s, %(cpu_wait_percent)s,
                %(mem_used_percent)s, %(mem_swap_percent)s, %(mem_available_bytes)s,
                %(network_sent_bytes)s, %(network_received_bytes)s, %(network_errors)s, %(network_drops)s,
                %(disk_used_percent)s, %(disk_io_time_ms)s, %(disk_ops_read)s, %(disk_ops_write)s,
                %(load_1m)s, %(load_5m)s, %(load_15m)s,
                %(paging_in)s, %(paging_out)s, %(process_count)s, %(inodes_used_percent)s
            )
        """

        if self._pool:
            with self.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.executemany(sql, rows)
                conn.commit()
            return

        # Pool not ready — use ad-hoc connection with whichever token we have
        token = self._cached_user_token or user_token
        if not token:
            raise RuntimeError("Lakebase not ready. Call /api/otel/bootstrap first.")
        conn = psycopg2.connect(
            host=self._host, port=5432, database=self._database,
            user=self._user, password=token,
            sslmode="require", connect_timeout=10,
        )
        try:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
            conn.commit()
        finally:
            conn.close()

    @property
    def is_configured(self) -> bool:
        return bool(self._host and self._user)

    def close(self) -> None:
        if self._pool:
            self._pool.closeall()
            self._pool = None


pool = LakebasePool()
