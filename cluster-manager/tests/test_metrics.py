"""Tests for metrics router."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from databricks.sdk.service.compute import State

from .conftest import make_autoscale, make_mock_cluster


class TestMetricsSummary:
    def test_counts_clusters_by_state(self, client, mock_ws):
        clusters = [
            make_mock_cluster(cluster_id="c1", state_value="RUNNING", num_workers=4),
            make_mock_cluster(cluster_id="c2", state_value="RUNNING", num_workers=2),
            make_mock_cluster(cluster_id="c3", state_value="TERMINATED"),
            make_mock_cluster(cluster_id="c4", state_value="PENDING"),
        ]
        # Set real State enums for the comparison logic in metrics.py
        clusters[0].state = State.RUNNING
        clusters[1].state = State.RUNNING
        clusters[2].state = State.TERMINATED
        clusters[3].state = State.PENDING
        # Autoscale must be None for num_workers to be used
        for c in clusters:
            c.autoscale = None

        mock_ws.clusters.list.return_value = clusters

        resp = client.get("/api/metrics/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_clusters"] == 4
        assert data["running_clusters"] == 2
        assert data["terminated_clusters"] == 1
        assert data["pending_clusters"] == 1
        assert data["total_running_workers"] == 6

    def test_handles_autoscale_workers(self, client, mock_ws):
        cluster = make_mock_cluster(cluster_id="c1", state_value="RUNNING", num_workers=0)
        cluster.state = State.RUNNING
        cluster.autoscale = make_autoscale(min_workers=2, max_workers=10)
        mock_ws.clusters.list.return_value = [cluster]

        resp = client.get("/api/metrics/summary")
        data = resp.json()
        # (2+10)//2 = 6
        assert data["total_running_workers"] == 6

    def test_empty_workspace(self, client, mock_ws):
        mock_ws.clusters.list.return_value = []
        resp = client.get("/api/metrics/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_clusters"] == 0
        assert data["estimated_hourly_dbu"] == 0.0


class TestIdleClusters:
    def test_detects_idle_cluster(self, client, mock_ws):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        # Last activity 2 hours ago
        last_activity_ms = now_ms - (2 * 60 * 60 * 1000)

        cluster = make_mock_cluster(
            cluster_id="idle-1",
            cluster_name="Idle Cluster",
            state_value="RUNNING",
            num_workers=4,
            start_time=now_ms - (3 * 60 * 60 * 1000),
            last_activity_time=last_activity_ms,
            autotermination_minutes=0,
        )
        cluster.state = State.RUNNING
        cluster.autoscale = None
        mock_ws.clusters.list.return_value = [cluster]

        resp = client.get("/api/metrics/idle-clusters")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["cluster_id"] == "idle-1"
        assert data[0]["idle_duration_minutes"] >= 119

    def test_ignores_terminated_clusters(self, client, mock_ws):
        cluster = make_mock_cluster(state_value="TERMINATED")
        cluster.state = State.TERMINATED
        mock_ws.clusters.list.return_value = [cluster]

        resp = client.get("/api/metrics/idle-clusters")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_ignores_recently_active_clusters(self, client, mock_ws):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        # Active 5 minutes ago (below 30 min threshold)
        cluster = make_mock_cluster(
            state_value="RUNNING",
            start_time=now_ms - (60 * 60 * 1000),
            last_activity_time=now_ms - (5 * 60 * 1000),
        )
        cluster.state = State.RUNNING
        cluster.autoscale = None
        mock_ws.clusters.list.return_value = [cluster]

        resp = client.get("/api/metrics/idle-clusters")
        assert resp.json() == []


class TestRecommendations:
    def test_no_auto_termination(self, client, mock_ws):
        cluster = make_mock_cluster(
            cluster_id="c1",
            cluster_name="No AutoTerm",
            state_value="RUNNING",
            autotermination_minutes=0,
        )
        cluster.state = State.RUNNING
        cluster.autoscale = None
        mock_ws.clusters.list.return_value = [cluster]

        resp = client.get("/api/metrics/recommendations")
        data = resp.json()
        assert any("auto-termination" in r["issue"].lower() for r in data)

    def test_large_fixed_cluster(self, client, mock_ws):
        cluster = make_mock_cluster(
            cluster_id="c1",
            cluster_name="Big Fixed",
            state_value="TERMINATED",
            num_workers=15,
        )
        cluster.state = State.TERMINATED
        cluster.autoscale = None
        mock_ws.clusters.list.return_value = [cluster]

        resp = client.get("/api/metrics/recommendations")
        data = resp.json()
        assert any("autoscaling" in r["recommendation"].lower() for r in data)

    def test_no_recommendations_for_healthy_cluster(self, client, mock_ws):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        cluster = make_mock_cluster(
            state_value="RUNNING",
            num_workers=4,
            autotermination_minutes=60,
            start_time=now_ms - (30 * 60 * 1000),  # started 30 min ago
            spark_version="14.3.x-scala2.12",
        )
        cluster.state = State.RUNNING
        cluster.autoscale = None
        mock_ws.clusters.list.return_value = [cluster]

        resp = client.get("/api/metrics/recommendations")
        assert resp.json() == []
