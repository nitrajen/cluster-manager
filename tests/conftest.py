"""Shared test fixtures for cluster-manager unit tests."""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from cluster_manager.backend.app import app
from cluster_manager.backend.core import AppConfig, get_config, get_ws


def make_mock_cluster(
    cluster_id="cluster-001",
    cluster_name="Test Cluster",
    state_value="RUNNING",
    num_workers=4,
    autoscale=None,
    spark_version="14.3.x-scala2.12",
    node_type_id="i3.xlarge",
    driver_node_type_id="i3.xlarge",
    cluster_source_value="UI",
    start_time=None,
    last_activity_time=None,
    terminated_time=None,
    termination_reason=None,
    creator_user_name="user@example.com",
    policy_id=None,
    autotermination_minutes=120,
    custom_tags=None,
    default_tags=None,
    spark_conf=None,
    spark_env_vars=None,
    aws_attributes=None,
    data_security_mode=None,
    state_message=None,
):
    """Create a mock ClusterDetails-like object."""
    cluster = MagicMock()
    cluster.cluster_id = cluster_id
    cluster.cluster_name = cluster_name

    # State enum mock
    state_mock = MagicMock()
    state_mock.value = state_value
    cluster.state = state_mock

    cluster.num_workers = num_workers
    cluster.autoscale = autoscale
    cluster.spark_version = spark_version
    cluster.node_type_id = node_type_id
    cluster.driver_node_type_id = driver_node_type_id

    # ClusterSource enum mock
    if cluster_source_value:
        source_mock = MagicMock()
        source_mock.value = cluster_source_value
        cluster.cluster_source = source_mock
    else:
        cluster.cluster_source = None

    cluster.start_time = start_time
    cluster.last_activity_time = last_activity_time
    cluster.terminated_time = terminated_time
    cluster.termination_reason = termination_reason
    cluster.creator_user_name = creator_user_name
    cluster.policy_id = policy_id
    cluster.autotermination_minutes = autotermination_minutes
    cluster.custom_tags = custom_tags or {}
    cluster.default_tags = default_tags or {}
    cluster.spark_conf = spark_conf or {}
    cluster.spark_env_vars = spark_env_vars or {}
    cluster.aws_attributes = aws_attributes
    cluster.azure_attributes = None
    cluster.gcp_attributes = None
    cluster.init_scripts = []
    cluster.cluster_log_conf = None
    cluster.enable_elastic_disk = None
    cluster.disk_spec = None
    cluster.single_user_name = None
    cluster.state_message = state_message

    if data_security_mode:
        dsm_mock = MagicMock()
        dsm_mock.value = data_security_mode
        cluster.data_security_mode = dsm_mock
    else:
        cluster.data_security_mode = None

    return cluster


def make_autoscale(min_workers=2, max_workers=8):
    """Create a mock AutoScale object."""
    autoscale = MagicMock()
    autoscale.min_workers = min_workers
    autoscale.max_workers = max_workers
    return autoscale


@pytest.fixture
def mock_config():
    """AppConfig with test warehouse ID."""
    return AppConfig(sql_warehouse_id="test-warehouse-id")


@pytest.fixture
def mock_ws():
    """Mock WorkspaceClient with empty cluster list by default."""
    ws = MagicMock()
    ws.clusters.list.return_value = iter([])
    ws.clusters.get.return_value = make_mock_cluster()
    ws.clusters.events.return_value = iter([])
    ws.cluster_policies.list.return_value = iter([])
    ws.warehouses.list.return_value = []
    return ws


@pytest.fixture
def client(mock_ws, mock_config):
    """FastAPI TestClient with mocked dependencies."""
    from fastapi.testclient import TestClient

    app.dependency_overrides[get_ws] = lambda: mock_ws
    app.dependency_overrides[get_config] = lambda: mock_config

    with TestClient(app, raise_server_exceptions=False) as tc:
        yield tc

    app.dependency_overrides.clear()
