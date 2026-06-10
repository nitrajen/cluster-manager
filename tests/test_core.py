"""Tests for core.py shared utilities."""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from cluster_manager.backend.core import AppConfig, execute_sql, get_warehouse_id


class TestGetWarehouseId:
    def test_returns_config_value_when_set(self):
        ws = MagicMock()
        config = AppConfig(sql_warehouse_id="configured-wh")
        assert get_warehouse_id(ws, config) == "configured-wh"
        ws.warehouses.list.assert_not_called()

    def test_picks_running_serverless_warehouse(self):
        ws = MagicMock()
        config = AppConfig(sql_warehouse_id=None)

        wh_serverless_running = MagicMock()
        wh_serverless_running.id = "serverless-1"
        wh_serverless_running.enable_serverless_compute = True
        wh_serverless_running.warehouse_type = None
        state = MagicMock()
        state.value = "RUNNING"
        wh_serverless_running.state = state

        wh_regular = MagicMock()
        wh_regular.id = "regular-1"
        wh_regular.enable_serverless_compute = False
        wh_regular.warehouse_type = None
        state2 = MagicMock()
        state2.value = "RUNNING"
        wh_regular.state = state2

        ws.warehouses.list.return_value = [wh_regular, wh_serverless_running]
        assert get_warehouse_id(ws, config) == "serverless-1"

    def test_picks_running_regular_when_no_serverless(self):
        ws = MagicMock()
        config = AppConfig(sql_warehouse_id=None)

        wh = MagicMock()
        wh.id = "regular-1"
        wh.enable_serverless_compute = False
        wh.warehouse_type = None
        state = MagicMock()
        state.value = "RUNNING"
        wh.state = state

        ws.warehouses.list.return_value = [wh]
        assert get_warehouse_id(ws, config) == "regular-1"

    def test_picks_stopped_serverless_when_nothing_running(self):
        ws = MagicMock()
        config = AppConfig(sql_warehouse_id=None)

        wh = MagicMock()
        wh.id = "serverless-stopped"
        wh.enable_serverless_compute = True
        wh.warehouse_type = None
        state = MagicMock()
        state.value = "STOPPED"
        wh.state = state

        ws.warehouses.list.return_value = [wh]
        assert get_warehouse_id(ws, config) == "serverless-stopped"

    def test_falls_back_to_first_warehouse(self):
        ws = MagicMock()
        config = AppConfig(sql_warehouse_id=None)

        wh = MagicMock()
        wh.id = "first-wh"
        wh.enable_serverless_compute = False
        wh.warehouse_type = None
        state = MagicMock()
        state.value = "DELETING"
        wh.state = state

        ws.warehouses.list.return_value = [wh]
        assert get_warehouse_id(ws, config) == "first-wh"

    def test_raises_when_no_warehouses(self):
        ws = MagicMock()
        config = AppConfig(sql_warehouse_id=None)
        ws.warehouses.list.return_value = []

        with pytest.raises(HTTPException) as exc_info:
            get_warehouse_id(ws, config)
        assert exc_info.value.status_code == 500
        assert "No SQL warehouse" in exc_info.value.detail

    def test_pro_warehouse_type_treated_as_serverless(self):
        ws = MagicMock()
        config = AppConfig(sql_warehouse_id=None)

        wh = MagicMock()
        wh.id = "pro-wh"
        wh.enable_serverless_compute = False
        wh_type = MagicMock()
        wh_type.value = "PRO"
        wh.warehouse_type = wh_type
        state = MagicMock()
        state.value = "RUNNING"
        wh.state = state

        ws.warehouses.list.return_value = [wh]
        assert get_warehouse_id(ws, config) == "pro-wh"


class TestExecuteSql:
    def _mock_ws_with_response(self, state, data_array=None, columns=None, error=None):
        """Create mock ws where state is the actual StatementState enum value."""
        from databricks.sdk.service.sql import StatementState

        ws = MagicMock()
        response = MagicMock()
        response.status.state = state

        if error:
            response.status.error = MagicMock()
            response.status.error.message = error
        else:
            response.status.error = None

        if data_array is not None:
            response.result = MagicMock()
            response.result.data_array = data_array
            if columns:
                col_mocks = []
                for name in columns:
                    col = MagicMock()
                    col.name = name
                    col_mocks.append(col)
                response.manifest = MagicMock()
                response.manifest.schema.columns = col_mocks
            else:
                response.manifest = None
        else:
            response.result = None

        ws.statement_execution.execute_statement.return_value = response
        return ws

    def test_returns_parsed_dicts_on_success(self):
        from databricks.sdk.service.sql import StatementState

        ws = self._mock_ws_with_response(
            StatementState.SUCCEEDED,
            data_array=[["val1", "val2"], ["val3", "val4"]],
            columns=["col_a", "col_b"],
        )
        result = execute_sql(ws, "wh-1", "SELECT 1")
        assert len(result) == 2
        assert result[0] == {"col_a": "val1", "col_b": "val2"}
        assert result[1] == {"col_a": "val3", "col_b": "val4"}

    def test_returns_empty_list_when_no_results(self):
        from databricks.sdk.service.sql import StatementState

        ws = self._mock_ws_with_response(StatementState.SUCCEEDED)
        ws.statement_execution.execute_statement.return_value.result = None
        result = execute_sql(ws, "wh-1", "SELECT 1")
        assert result == []

    def test_raises_on_sdk_exception(self):
        ws = MagicMock()
        ws.statement_execution.execute_statement.side_effect = Exception("WAREHOUSE timeout")

        with pytest.raises(HTTPException) as exc_info:
            execute_sql(ws, "wh-1", "SELECT 1")
        assert exc_info.value.status_code == 503

    def test_raises_on_failed_state(self):
        from databricks.sdk.service.sql import StatementState

        ws = self._mock_ws_with_response(StatementState.FAILED, error="syntax error")
        with pytest.raises(HTTPException) as exc_info:
            execute_sql(ws, "wh-1", "BAD SQL")
        assert exc_info.value.status_code == 500
        assert "syntax error" in exc_info.value.detail

    def test_raises_on_pending_state(self):
        from databricks.sdk.service.sql import StatementState

        ws = self._mock_ws_with_response(StatementState.PENDING)
        with pytest.raises(HTTPException) as exc_info:
            execute_sql(ws, "wh-1", "SLOW QUERY")
        assert exc_info.value.status_code == 503
