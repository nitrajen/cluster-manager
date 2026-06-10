"""Tests for clusters router."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from databricks.sdk.service.compute import State

from cluster_manager.backend.models import ClusterState
from cluster_manager.backend.routers.clusters import (
    _calculate_uptime_minutes,
    _estimate_dbu_per_hour,
    _format_termination_reason,
    _ms_to_datetime,
    _state_to_enum,
)

from .conftest import make_autoscale, make_mock_cluster


# --- Helper function tests ---


class TestStateToEnum:
    def test_running(self):
        assert _state_to_enum(State.RUNNING) == ClusterState.RUNNING

    def test_terminated(self):
        assert _state_to_enum(State.TERMINATED) == ClusterState.TERMINATED

    def test_pending(self):
        assert _state_to_enum(State.PENDING) == ClusterState.PENDING

    def test_none(self):
        assert _state_to_enum(None) == ClusterState.UNKNOWN

    def test_all_states_mapped(self):
        for state in [State.RUNNING, State.TERMINATED, State.PENDING,
                      State.RESTARTING, State.RESIZING, State.TERMINATING, State.ERROR]:
            result = _state_to_enum(state)
            assert result != ClusterState.UNKNOWN


class TestMsToDatetime:
    def test_valid_timestamp(self):
        result = _ms_to_datetime(1700000000000)
        assert isinstance(result, datetime)
        assert result.tzinfo == timezone.utc

    def test_none(self):
        assert _ms_to_datetime(None) is None

    def test_epoch(self):
        result = _ms_to_datetime(0)
        assert result == datetime(1970, 1, 1, tzinfo=timezone.utc)


class TestCalculateUptimeMinutes:
    def test_running_cluster(self):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = now_ms - (60 * 60 * 1000)  # 1 hour ago
        cluster = make_mock_cluster(state_value="RUNNING", start_time=start_ms)
        cluster.state = State.RUNNING
        result = _calculate_uptime_minutes(cluster)
        assert 58 <= result <= 62  # ~60 min with tolerance

    def test_terminated_cluster(self):
        cluster = make_mock_cluster(state_value="TERMINATED", start_time=1700000000000)
        cluster.state = State.TERMINATED
        assert _calculate_uptime_minutes(cluster) == 0

    def test_no_start_time(self):
        cluster = make_mock_cluster(state_value="RUNNING", start_time=None)
        cluster.state = State.RUNNING
        assert _calculate_uptime_minutes(cluster) == 0


class TestEstimateDBUPerHour:
    def test_running_fixed_workers(self):
        cluster = make_mock_cluster(state_value="RUNNING", num_workers=4)
        cluster.state = State.RUNNING
        cluster.autoscale = None
        result = _estimate_dbu_per_hour(cluster)
        assert result == 5.0  # 4 workers + 1 driver

    def test_running_autoscale(self):
        cluster = make_mock_cluster(state_value="RUNNING", num_workers=0)
        cluster.state = State.RUNNING
        cluster.autoscale = make_autoscale(min_workers=2, max_workers=8)
        result = _estimate_dbu_per_hour(cluster)
        assert result == 6.0  # avg(2,8)=5 + 1 driver

    def test_terminated(self):
        cluster = make_mock_cluster(state_value="TERMINATED")
        cluster.state = State.TERMINATED
        assert _estimate_dbu_per_hour(cluster) == 0.0


class TestFormatTerminationReason:
    def test_none(self):
        assert _format_termination_reason(None) is None

    def test_with_code_and_type(self):
        reason = MagicMock()
        reason.code = MagicMock()
        reason.code.value = "INACTIVITY"
        reason.type = MagicMock()
        reason.type.value = "SUCCESS"
        reason.parameters = {"inactivity_duration_min": "120"}
        result = _format_termination_reason(reason)
        assert "INACTIVITY" in result
        assert "SUCCESS" in result
        assert "inactivity_duration_min=120" in result

    def test_with_no_parameters(self):
        reason = MagicMock()
        reason.code = MagicMock()
        reason.code.value = "USER_REQUEST"
        reason.type = None
        reason.parameters = None
        result = _format_termination_reason(reason)
        assert result == "USER_REQUEST"


# --- Endpoint tests ---


class TestListClustersEndpoint:
    def test_returns_empty_list(self, client, mock_ws):
        mock_ws.clusters.list.return_value = iter([])
        resp = client.get("/api/clusters")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_clusters(self, client, mock_ws):
        clusters = [
            make_mock_cluster(cluster_id="c1", cluster_name="Alpha", state_value="RUNNING"),
            make_mock_cluster(cluster_id="c2", cluster_name="Beta", state_value="TERMINATED"),
        ]
        mock_ws.clusters.list.return_value = iter(clusters)
        resp = client.get("/api/clusters")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # Running comes first
        assert data[0]["cluster_id"] == "c1"
        assert data[0]["state"] == "RUNNING"

    def test_filters_by_state(self, client, mock_ws):
        clusters = [
            make_mock_cluster(cluster_id="c1", state_value="RUNNING"),
            make_mock_cluster(cluster_id="c2", state_value="TERMINATED"),
        ]
        mock_ws.clusters.list.return_value = iter(clusters)
        resp = client.get("/api/clusters?state=TERMINATED")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["state"] == "TERMINATED"

    def test_respects_limit(self, client, mock_ws):
        clusters = [make_mock_cluster(cluster_id=f"c{i}", cluster_name=f"C{i}") for i in range(10)]
        mock_ws.clusters.list.return_value = iter(clusters)
        resp = client.get("/api/clusters?limit=3")
        assert resp.status_code == 200
        assert len(resp.json()) == 3


class TestGetClusterEndpoint:
    def test_returns_detail(self, client, mock_ws):
        cluster = make_mock_cluster(cluster_id="abc-123", cluster_name="My Cluster")
        mock_ws.clusters.get.return_value = cluster
        resp = client.get("/api/clusters/abc-123")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cluster_id"] == "abc-123"
        assert data["cluster_name"] == "My Cluster"

    def test_returns_404_on_not_found(self, client, mock_ws):
        mock_ws.clusters.get.side_effect = Exception("RESOURCE_DOES_NOT_EXIST")
        resp = client.get("/api/clusters/nonexistent")
        assert resp.status_code == 404


class TestClusterMetricsEndpoint:
    @patch("cluster_manager.backend.routers.clusters.execute_sql")
    def test_returns_time_series(self, mock_exec_sql, client, mock_ws, mock_config):
        mock_exec_sql.return_value = [
            {
                "start_time": "2024-01-01T12:00:00Z",
                "instance_id": "i-001",
                "driver": "true",
                "cpu_user_percent": "45.5",
                "cpu_system_percent": "10.2",
                "cpu_wait_percent": "1.0",
                "mem_used_percent": "72.3",
                "network_sent_bytes": "1024",
                "network_received_bytes": "2048",
                "node_type": "i3.xlarge",
            },
            {
                "start_time": "2024-01-01T12:00:00Z",
                "instance_id": "i-002",
                "driver": "false",
                "cpu_user_percent": "60.0",
                "cpu_system_percent": "5.0",
                "cpu_wait_percent": "2.0",
                "mem_used_percent": "80.0",
                "network_sent_bytes": "512",
                "network_received_bytes": "1024",
                "node_type": "i3.xlarge",
            },
        ]
        resp = client.get("/api/clusters/cluster-001/metrics?minutes=60")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cluster_id"] == "cluster-001"
        assert len(data["time_series"]) == 1
        # CPU averaged across 2 nodes
        point = data["time_series"][0]
        assert point["cpu_user_percent"] == pytest.approx(52.75, rel=0.01)
        assert point["cpu_system_percent"] == pytest.approx(7.6, rel=0.01)
        # 2 current nodes
        assert len(data["current_nodes"]) == 2
        # Driver first
        assert data["current_nodes"][0]["is_driver"] is True

    @patch("cluster_manager.backend.routers.clusters.execute_sql")
    def test_returns_empty_when_no_data(self, mock_exec_sql, client, mock_ws, mock_config):
        mock_exec_sql.return_value = []
        resp = client.get("/api/clusters/cluster-001/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["time_series"] == []
        assert data["current_nodes"] == []


class TestStartClusterEndpoint:
    def test_starts_terminated_cluster(self, client, mock_ws):
        cluster = make_mock_cluster(state_value="TERMINATED")
        cluster.state = State.TERMINATED
        mock_ws.clusters.get.return_value = cluster
        resp = client.post("/api/clusters/c1/start")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "initiated" in data["message"]
        mock_ws.clusters.start.assert_called_once_with("c1")

    def test_already_running(self, client, mock_ws):
        cluster = make_mock_cluster(state_value="RUNNING")
        cluster.state = State.RUNNING
        mock_ws.clusters.get.return_value = cluster
        resp = client.post("/api/clusters/c1/start")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "already running" in data["message"]
        mock_ws.clusters.start.assert_not_called()

    def test_wrong_state(self, client, mock_ws):
        cluster = make_mock_cluster(state_value="PENDING")
        cluster.state = State.PENDING
        mock_ws.clusters.get.return_value = cluster
        resp = client.post("/api/clusters/c1/start")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False


class TestStopClusterEndpoint:
    def test_stops_running_cluster(self, client, mock_ws):
        cluster = make_mock_cluster(state_value="RUNNING")
        cluster.state = State.RUNNING
        mock_ws.clusters.get.return_value = cluster
        resp = client.post("/api/clusters/c1/stop")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        mock_ws.clusters.delete.assert_called_once_with("c1")

    def test_already_terminated(self, client, mock_ws):
        cluster = make_mock_cluster(state_value="TERMINATED")
        cluster.state = State.TERMINATED
        mock_ws.clusters.get.return_value = cluster
        resp = client.post("/api/clusters/c1/stop")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "already stopped" in data["message"]
        mock_ws.clusters.delete.assert_not_called()
