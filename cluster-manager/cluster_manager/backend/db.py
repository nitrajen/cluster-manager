"""Lakebase (PostgreSQL) connection management for OTel metrics storage."""

from __future__ import annotations

import os
import subprocess
import json
import time
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.pool

from .core import logger

# Lakebase connection config
LAKEBASE_PROJECT = os.getenv("LAKEBASE_PROJECT", "cluster-metrics")
LAKEBASE_BRANCH = os.getenv("LAKEBASE_BRANCH", "production")
LAKEBASE_ENDPOINT = os.getenv("LAKEBASE_ENDPOINT", "primary")
LAKEBASE_HOST = os.getenv("LAKEBASE_HOST", "")
LAKEBASE_DATABASE = os.getenv("LAKEBASE_DATABASE", "otel_metrics")
LAKEBASE_USER = os.getenv("LAKEBASE_USER", "")
DATABRICKS_PROFILE = os.getenv("DATABRICKS_PROFILE", "FEVM_SERVERLESS_STABLE")

# Token refresh interval (50 minutes, tokens last 60)
TOKEN_REFRESH_INTERVAL = 50 * 60

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS node_metrics (
    id BIGSERIAL PRIMARY KEY,
    cluster_id VARCHAR(64) NOT NULL,
    instance_id VARCHAR(64) NOT NULL,
    is_driver BOOLEAN NOT NULL DEFAULT FALSE,
    node_type VARCHAR(64),
    ts TIMESTAMPTZ NOT NULL,
    cpu_user_percent DOUBLE PRECISION,
    cpu_system_percent DOUBLE PRECISION,
    cpu_wait_percent DOUBLE PRECISION,
    mem_used_percent DOUBLE PRECISION,
    mem_swap_percent DOUBLE PRECISION,
    network_sent_bytes BIGINT,
    network_received_bytes BIGINT,
    disk_used_percent DOUBLE PRECISION,
    load_1m DOUBLE PRECISION,
    load_5m DOUBLE PRECISION,
    load_15m DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_node_metrics_cluster_time
    ON node_metrics (cluster_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_node_metrics_time
    ON node_metrics (ts DESC);
"""


class LakebasePool:
    """Manages a psycopg2 connection pool to Lakebase with auto-refreshing OAuth tokens."""

    def __init__(self):
        self._pool: psycopg2.pool.ThreadedConnectionPool | None = None
        self._token: str | None = None
        self._token_expiry: float = 0
        self._lock = threading.Lock()
        self._host = LAKEBASE_HOST
        self._database = LAKEBASE_DATABASE
        self._user = LAKEBASE_USER

    def _generate_token(self) -> str:
        """Generate OAuth token via Databricks CLI."""
        endpoint_path = (
            f"projects/{LAKEBASE_PROJECT}/branches/{LAKEBASE_BRANCH}"
            f"/endpoints/{LAKEBASE_ENDPOINT}"
        )
        try:
            result = subprocess.run(
                [
                    "databricks", "postgres", "generate-database-credential",
                    endpoint_path,
                    "--profile", DATABRICKS_PROFILE,
                    "--output", "json",
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                raise RuntimeError(f"CLI error: {result.stderr}")
            data = json.loads(result.stdout)
            return data["token"]
        except FileNotFoundError:
            raise RuntimeError(
                "databricks CLI not found. Install: brew install databricks"
            )

    def _ensure_token(self) -> str:
        """Get valid token, refreshing if needed."""
        now = time.time()
        if self._token and now < self._token_expiry:
            return self._token
        with self._lock:
            if self._token and now < self._token_expiry:
                return self._token
            logger.info("Refreshing Lakebase OAuth token")
            self._token = self._generate_token()
            self._token_expiry = now + TOKEN_REFRESH_INTERVAL
            # Recreate pool with new token
            self._recreate_pool()
            return self._token

    def _recreate_pool(self):
        """Recreate connection pool with current token."""
        if self._pool:
            try:
                self._pool.closeall()
            except Exception:
                pass
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            host=self._host,
            port=5432,
            database=self._database,
            user=self._user,
            password=self._token,
            sslmode="require",
        )
        logger.info(f"Lakebase pool created: {self._host}/{self._database}")

    def initialize(self, host: str | None = None, user: str | None = None):
        """Initialize pool. Call during app startup."""
        if host:
            self._host = host
        if user:
            self._user = user
        if not self._host or not self._user:
            logger.warning("Lakebase not configured (missing host or user). Skipping.")
            return
        self._ensure_token()
        logger.info("Lakebase pool initialized successfully")

    def ensure_schema(self):
        """Create tables if they don't exist."""
        if not self._pool:
            logger.warning("Lakebase not initialized, skipping schema creation")
            return
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()
        logger.info("Lakebase schema ensured")

    @contextmanager
    def get_conn(self):
        """Get a connection from the pool. Auto-refreshes token if needed."""
        self._ensure_token()
        if not self._pool:
            raise RuntimeError("Lakebase pool not initialized")
        conn = self._pool.getconn()
        try:
            yield conn
        finally:
            self._pool.putconn(conn)

    def close(self):
        """Close all connections."""
        if self._pool:
            self._pool.closeall()
            self._pool = None

    @property
    def is_configured(self) -> bool:
        return bool(self._host and self._user)


    def purge_old_metrics(self, retention_days: int = 7) -> int:
        """Delete metrics older than retention period. Returns rows deleted."""
        if not self._pool:
            return 0
        sql = "DELETE FROM node_metrics WHERE ts < NOW() - make_interval(days => %s)"
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (retention_days,))
                deleted = cur.rowcount
            conn.commit()
        if deleted > 0:
            logger.info(f"Lakebase retention: purged {deleted} rows older than {retention_days} days")
        return deleted


# Singleton pool instance
pool = LakebasePool()
