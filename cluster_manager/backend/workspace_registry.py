"""Multi-workspace registry for aggregating clusters from client workspaces.

Parses REGISTERED_WORKSPACES env var (JSON array) and provides authenticated
WorkspaceClient instances via M2M OAuth (client_credentials flow).
"""

from __future__ import annotations

import json
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from databricks.sdk import WorkspaceClient

from .core import logger

# Token refresh margin (refresh 5 min before expiry)
TOKEN_MARGIN_SECONDS = 300
# Default token lifetime assumption (1 hour)
DEFAULT_TOKEN_LIFETIME = 3600
# Timeout for workspace API calls during aggregation
WORKSPACE_TIMEOUT_SECONDS = 10


class WorkspaceEntry:
    """A registered workspace with cached M2M OAuth token."""

    def __init__(self, url: str, name: str, client_id: str, client_secret: str, token_endpoint: str):
        self.url = url.rstrip("/")
        self.name = name
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_endpoint = token_endpoint
        self._token: str | None = None
        self._token_expiry: float = 0
        self._lock = threading.Lock()
        self.last_poll_status: str = "not_polled"
        self.last_poll_at: float = 0

    def _fetch_token(self) -> str:
        """Fetch M2M OAuth token via client_credentials grant."""
        resp = requests.post(
            self.token_endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "all-apis",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token", "")
        if not token:
            raise RuntimeError(f"No access_token in response from {self.token_endpoint}")
        return token

    def get_token(self) -> str:
        """Get valid token, refreshing if near expiry."""
        now = time.time()
        if self._token and now < (self._token_expiry - TOKEN_MARGIN_SECONDS):
            return self._token
        with self._lock:
            if self._token and now < (self._token_expiry - TOKEN_MARGIN_SECONDS):
                return self._token
            self._token = self._fetch_token()
            self._token_expiry = now + DEFAULT_TOKEN_LIFETIME
            return self._token

    def get_client(self) -> WorkspaceClient:
        """Get authenticated WorkspaceClient for this workspace."""
        token = self.get_token()
        return WorkspaceClient(host=self.url, token=token, auth_type="pat")

    @property
    def info(self) -> dict:
        """Public info (no secrets)."""
        return {
            "url": self.url,
            "name": self.name,
            "last_poll_status": self.last_poll_status,
            "last_poll_at": self.last_poll_at,
        }


class WorkspaceRegistry:
    """Registry of client workspaces for multi-workspace cluster aggregation."""

    def __init__(self):
        self._entries: list[WorkspaceEntry] = []

    def load_from_env(self):
        """Parse REGISTERED_WORKSPACES JSON array from environment."""
        raw = os.getenv("REGISTERED_WORKSPACES", "").strip()
        if not raw:
            logger.info("No REGISTERED_WORKSPACES configured — single-workspace mode")
            return
        try:
            workspaces = json.loads(raw)
            if not isinstance(workspaces, list):
                raise ValueError("REGISTERED_WORKSPACES must be a JSON array")
            for ws in workspaces:
                entry = WorkspaceEntry(
                    url=ws["url"],
                    name=ws["name"],
                    client_id=ws["client_id"],
                    client_secret=ws["client_secret"],
                    token_endpoint=ws["token_endpoint"],
                )
                self._entries.append(entry)
            logger.info(f"Loaded {len(self._entries)} registered workspace(s): "
                        f"{[e.name for e in self._entries]}")
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error(f"Failed to parse REGISTERED_WORKSPACES: {e}")

    @property
    def entries(self) -> list[WorkspaceEntry]:
        return self._entries

    @property
    def is_multi_workspace(self) -> bool:
        return len(self._entries) > 0

    def get_entry_by_url(self, workspace_url: str) -> WorkspaceEntry | None:
        """Find workspace entry by URL."""
        normalized = workspace_url.rstrip("/")
        for entry in self._entries:
            if entry.url == normalized:
                return entry
        return None

    def get_client(self, workspace_url: str) -> WorkspaceClient | None:
        """Get authenticated client for a specific workspace URL."""
        entry = self.get_entry_by_url(workspace_url)
        if not entry:
            return None
        return entry.get_client()

    def fetch_clusters_from_all(self, local_ws: WorkspaceClient | None = None) -> list[tuple[str, str, any]]:
        """Fetch clusters from all registered workspaces in parallel.

        Returns list of (workspace_name, workspace_url, ClusterDetails).
        Failures for individual workspaces are logged but don't break the response.
        """
        results = []

        def _fetch_one(entry: WorkspaceEntry):
            try:
                client = entry.get_client()
                clusters = list(client.clusters.list())
                entry.last_poll_status = "ok"
                entry.last_poll_at = time.time()
                return [(entry.name, entry.url, c) for c in clusters]
            except Exception as e:
                entry.last_poll_status = f"error: {str(e)[:100]}"
                entry.last_poll_at = time.time()
                logger.warning(f"Failed to fetch clusters from {entry.name} ({entry.url}): {e}")
                return []

        # Also fetch from local FEVM workspace
        def _fetch_local():
            if not local_ws:
                return []
            try:
                clusters = list(local_ws.clusters.list())
                return [(None, None, c) for c in clusters]
            except Exception as e:
                logger.warning(f"Failed to fetch local clusters: {e}")
                return []

        with ThreadPoolExecutor(max_workers=min(len(self._entries) + 1, 8)) as executor:
            futures = []
            futures.append(executor.submit(_fetch_local))
            for entry in self._entries:
                futures.append(executor.submit(_fetch_one, entry))

            for future in as_completed(futures, timeout=WORKSPACE_TIMEOUT_SECONDS + 5):
                try:
                    results.extend(future.result())
                except Exception as e:
                    logger.warning(f"Workspace fetch future failed: {e}")

        return results


# Singleton
registry = WorkspaceRegistry()
