"""Minimal OTel metrics receiver — Databricks App entry point."""
import logging
import os
from contextlib import asynccontextmanager

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.compute import State
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import db
from otel import router as otel_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _resolve_warehouse(ws: WorkspaceClient) -> str:
    wh_id = os.getenv("SQL_WAREHOUSE_ID", "")
    if wh_id:
        return wh_id
    # Auto-discover: prefer a running serverless warehouse
    warehouses = list(ws.warehouses.list())
    def _serverless(w) -> bool:
        return getattr(w, "enable_serverless_compute", False) or \
               str(getattr(w, "warehouse_type", "")).upper() == "PRO"
    for w in sorted(warehouses, key=lambda w: (not _serverless(w), str(w.state))):
        if w.id:
            logger.info(f"Using warehouse: {w.name} ({w.id})")
            return w.id
    raise RuntimeError("No SQL warehouse found. Set SQL_WAREHOUSE_ID in app.yaml.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ws = WorkspaceClient()
    warehouse_id = _resolve_warehouse(ws)
    try:
        db.ensure_schema(ws, warehouse_id)
    except Exception as e:
        logger.warning(f"Schema init failed (non-fatal, will retry on first write): {e}")
    app.state.ws = ws
    app.state.warehouse_id = warehouse_id
    logger.info(f"Ready — writing to {db.FULL_TABLE} via warehouse {warehouse_id}")
    yield


app = FastAPI(title="OTel Metrics Receiver", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(otel_router)


@app.get("/health")
async def health(request: "Request"):
    return {
        "status": "healthy",
        "table": db.FULL_TABLE,
        "warehouse_id": getattr(request.app.state, "warehouse_id", None),
    }
