"""Tests for optimization router helper functions."""

from unittest.mock import MagicMock

import pytest

from cluster_manager.backend.models import ClusterType
from cluster_manager.backend.routers.optimization import (
    _calculate_efficiency,
    _classify_cluster,
)


class TestClassifyCluster:
    def _make_cluster_with_source(self, source_value):
        cluster = MagicMock()
        if source_value is None:
            cluster.cluster_source = None
        else:
            source = MagicMock()
            source.value = source_value
            cluster.cluster_source = source
        return cluster

    def test_job_source(self):
        assert _classify_cluster(self._make_cluster_with_source("JOB")) == ClusterType.JOB

    def test_sql_source(self):
        assert _classify_cluster(self._make_cluster_with_source("SQL")) == ClusterType.SQL

    def test_pipeline_source(self):
        assert _classify_cluster(self._make_cluster_with_source("PIPELINE")) == ClusterType.PIPELINE

    def test_pipeline_maintenance_source(self):
        assert _classify_cluster(self._make_cluster_with_source("PIPELINE_MAINTENANCE")) == ClusterType.PIPELINE

    def test_models_source(self):
        assert _classify_cluster(self._make_cluster_with_source("MODELS")) == ClusterType.MODELS

    def test_ui_source(self):
        assert _classify_cluster(self._make_cluster_with_source("UI")) == ClusterType.INTERACTIVE

    def test_api_source(self):
        assert _classify_cluster(self._make_cluster_with_source("API")) == ClusterType.INTERACTIVE

    def test_none_source(self):
        assert _classify_cluster(self._make_cluster_with_source(None)) == ClusterType.INTERACTIVE


class TestCalculateEfficiency:
    def test_normal_calculation(self):
        # 50 actual DBU out of (4+1)*10=50 potential
        result = _calculate_efficiency(actual_dbu=50.0, workers=4, uptime_hours=10.0)
        assert result == 100.0

    def test_half_efficiency(self):
        result = _calculate_efficiency(actual_dbu=25.0, workers=4, uptime_hours=10.0)
        assert result == 50.0

    def test_zero_workers(self):
        # potential = (0+1)*10 = 10, efficiency = 5/10 = 50%
        result = _calculate_efficiency(actual_dbu=5.0, workers=0, uptime_hours=10.0)
        assert result == 50.0

    def test_zero_uptime(self):
        result = _calculate_efficiency(actual_dbu=100.0, workers=4, uptime_hours=0.0)
        assert result == 0.0

    def test_caps_at_100(self):
        result = _calculate_efficiency(actual_dbu=200.0, workers=4, uptime_hours=10.0)
        assert result == 100.0

    def test_all_zero(self):
        result = _calculate_efficiency(actual_dbu=0.0, workers=0, uptime_hours=0.0)
        assert result == 0.0
