"""Tests for billing router."""

from unittest.mock import patch

import pytest


class TestBillingSummary:
    @patch("cluster_manager.backend.routers.billing.execute_sql")
    def test_returns_summary(self, mock_sql, client):
        mock_sql.return_value = [{
            "total_dbu": "1500.5",
            "period_start": "2024-01-01",
            "period_end": "2024-01-30",
        }]
        resp = client.get("/api/billing/summary?days=30")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_dbu"] == 1500.5
        assert data["estimated_cost_usd"] == 225.07  # round(1500.5 * 0.15, 2)

    @patch("cluster_manager.backend.routers.billing.execute_sql")
    def test_empty_results(self, mock_sql, client):
        mock_sql.return_value = [{"total_dbu": "0", "period_start": None, "period_end": None}]
        resp = client.get("/api/billing/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_dbu"] == 0.0


class TestBillingTrend:
    @patch("cluster_manager.backend.routers.billing.execute_sql")
    def test_returns_daily_points(self, mock_sql, client):
        mock_sql.return_value = [
            {"date": "2024-01-01", "dbu": "100.0"},
            {"date": "2024-01-02", "dbu": "200.0"},
            {"date": "2024-01-03", "dbu": "150.0"},
        ]
        resp = client.get("/api/billing/trend?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        assert data[0]["dbu"] == 100.0
        assert data[1]["estimated_cost_usd"] == 30.0  # 200 * 0.15

    @patch("cluster_manager.backend.routers.billing.execute_sql")
    def test_empty_trend(self, mock_sql, client):
        mock_sql.return_value = []
        resp = client.get("/api/billing/trend")
        assert resp.status_code == 200
        assert resp.json() == []


class TestTopConsumers:
    @patch("cluster_manager.backend.routers.billing.execute_sql")
    def test_returns_consumers_with_percentages(self, mock_sql, client, mock_ws):
        # First call: total, second call: by cluster
        mock_sql.side_effect = [
            [{"total_dbu": "1000.0"}],
            [
                {"cluster_id": "c1", "total_dbu": "600.0"},
                {"cluster_id": "c2", "total_dbu": "400.0"},
            ],
        ]
        mock_ws.clusters.list.return_value = iter([])

        resp = client.get("/api/billing/top-consumers?days=30&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["cluster_id"] == "c1"
        assert data[0]["percentage_of_total"] == 60.0
        assert data[1]["percentage_of_total"] == 40.0

    @patch("cluster_manager.backend.routers.billing.execute_sql")
    def test_zero_total(self, mock_sql, client, mock_ws):
        mock_sql.side_effect = [
            [{"total_dbu": "0"}],
            [],
        ]
        mock_ws.clusters.list.return_value = iter([])

        resp = client.get("/api/billing/top-consumers")
        assert resp.status_code == 200
        assert resp.json() == []


class TestBillingByCluster:
    @patch("cluster_manager.backend.routers.billing.execute_sql")
    def test_returns_usage_by_cluster(self, mock_sql, client, mock_ws):
        mock_sql.return_value = [
            {"cluster_id": "c1", "total_dbu": "500.0", "usage_start": "2024-01-01", "usage_end": "2024-01-30"},
        ]
        mock_ws.clusters.list.return_value = iter([])

        resp = client.get("/api/billing/by-cluster?days=30")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["total_dbu"] == 500.0
        assert data[0]["estimated_cost_usd"] == 75.0
