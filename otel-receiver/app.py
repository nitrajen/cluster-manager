"""Minimal OTel metrics receiver — Databricks App entry point."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from databricks.sdk import WorkspaceClient
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

import db
from otel import router as otel_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _resolve_warehouse(ws: WorkspaceClient) -> str:
    wh_id = os.getenv("SQL_WAREHOUSE_ID", "")
    if wh_id:
        return wh_id
    warehouses = list(ws.warehouses.list())
    def _serverless(w) -> bool:
        return getattr(w, "enable_serverless_compute", False) or \
               str(getattr(w, "warehouse_type", "")).upper() == "PRO"
    for w in sorted(warehouses, key=lambda w: (not _serverless(w), str(w.state))):
        if w.id:
            logger.info(f"Auto-selected warehouse: {w.name} ({w.id})")
            return w.id
    raise RuntimeError("No SQL warehouse found. Set SQL_WAREHOUSE_ID in app.yaml.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ws = WorkspaceClient()
    warehouse_id = _resolve_warehouse(ws)

    try:
        db.ensure_schema(ws, warehouse_id)
    except Exception as e:
        logger.warning(f"Schema init failed (will retry on first flush): {e}")

    db.buffer.configure(ws, warehouse_id)

    # Background task: flush buffer to Delta on a fixed interval
    flush_task = asyncio.create_task(db.buffer.flush_loop())

    app.state.ws = ws
    app.state.warehouse_id = warehouse_id
    logger.info(f"Ready — buffering to {db.FULL_TABLE}, flushing every {db.FLUSH_INTERVAL}s")

    yield

    # Flush remaining rows before shutdown
    flush_task.cancel()
    remaining = db.buffer.drain()
    if remaining:
        logger.info(f"Shutdown flush: writing {len(remaining)} buffered rows")
        try:
            db._write(remaining, ws, warehouse_id)
        except Exception as e:
            logger.error(f"Shutdown flush failed — {len(remaining)} rows dropped: {e}")


app = FastAPI(title="OTel Metrics Receiver", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(otel_router)


@app.get("/health")
async def health(request: Request):
    return {
        "status": "healthy",
        "table": db.FULL_TABLE,
        "warehouse_id": getattr(request.app.state, "warehouse_id", None),
        "buffered_rows": len(db.buffer._rows),
        "flush_interval_seconds": db.FLUSH_INTERVAL,
    }
