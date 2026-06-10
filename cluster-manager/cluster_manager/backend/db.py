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
        """Generate OAuth token via Databricks CLI or SDK.

        Tries CLI first (local dev), falls back to SDK (deployed app).
        """
        endpoint_path = (
            f"projects/{LAKEBASE_PROJECT}/branches/{LAKEBASE_BRANCH}"
            f"/endpoints/{LAKEBASE_ENDPOINT}"
        )

        # Try CLI first (works for local dev with profiles)
        try:
            cmd = ["databricks", "postgres", "generate-database-credential",
                   endpoint_path, "--output", "json"]
            if DATABRICKS_PROFILE:
                cmd.extend(["--profile", DATABRICKS_PROFILE])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                return data["token"]
            logger.warning(f"CLI token generation failed: {result.stderr[:100]}")
        except FileNotFoundError:
            logger.info("databricks CLI not found, trying SDK approach")

        # Fallback: use Databricks SDK (for deployed apps with SP auth)
        try:
            from databricks.sdk import WorkspaceClient
            ws = WorkspaceClient(
                profile=DATABRICKS_PROFILE if DATABRICKS_PROFILE else None
            )
            resp = ws.api_client.do(
                "POST",
                "/api/2.0/postgres/credentials",
                body={"endpoint": endpoint_path},
            )
            if isinstance(resp, dict):
                return resp.get("token", resp.get("password", ""))
            raise RuntimeError(f"Unexpected response: {resp}")
        except Exception as sdk_err:
            logger.warning(f"SDK token generation failed: {sdk_err}")

        # Last resort: use static LAKEBASE_TOKEN env var if set
        static_token = os.getenv("LAKEBASE_TOKEN", "")
        if static_token:
            logger.info("Using static LAKEBASE_TOKEN from environment")
            return static_token

        raise RuntimeError(
            f"Cannot generate Lakebase token (CLI, SDK, and env all failed)"
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
        try:
            self._pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                host=self._host,
                port=5432,
                database=self._database,
                user=self._user,
                password=self._token,
                sslmode="require",
                connect_timeout=10,
            )
            logger.info(f"Lakebase pool created: {self._host}/{self._database}")
        except Exception as e:
            logger.error(f"Lakebase pool creation failed: {e}")
            self._pool = None
            raise

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
        try:
            self._ensure_token()
        except Exception as e:
            raise RuntimeError(f"Lakebase token refresh failed: {e}")
        # If pool is None (previous connection attempt failed), force retry
        if not self._pool:
            self._token_expiry = 0  # force re-generation
            try:
                self._ensure_token()
            except Exception as e:
                raise RuntimeError(f"Lakebase pool retry failed: {e}")
        if not self._pool:
            raise RuntimeError("Lakebase pool unavailable")
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
