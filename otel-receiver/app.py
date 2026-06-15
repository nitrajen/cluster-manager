"""Minimal OTel metrics receiver — Databricks App entry point."""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db import pool
from otel import router as otel_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    host = os.getenv("LAKEBASE_HOST", "")
    user = os.getenv("LAKEBASE_USER", "")
    try:
        pool.initialize(host=host, user=user)
        pool.ensure_schema()
    except Exception as e:
        logger.warning(f"Lakebase init failed (non-fatal — call /api/otel/bootstrap): {e}")
    yield
    pool.close()


app = FastAPI(title="OTel Metrics Receiver", lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(otel_router)


@app.get("/health")
async def health():
    return {"status": "healthy", "lakebase": pool.is_configured}
