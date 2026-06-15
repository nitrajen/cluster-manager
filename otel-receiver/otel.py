"""OTel metrics receiver — accepts OTLP/HTTP JSON from cluster nodes, appends to Delta table."""
from __future__ import annotations

import base64
import gzip
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request

import db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/otel", tags=["otel"])

# ── Auth ──────────────────────────────────────────────────────────────────────

_ALLOWED_SP_IDS: set[str] | None = None


def _allowed_sp_ids() -> set[str]:
    global _ALLOWED_SP_IDS
    if _ALLOWED_SP_IDS is None:
        raw = os.getenv("OTEL_ALLOWED_SP_IDS", "")
        _ALLOWED_SP_IDS = {s.strip() for s in raw.split(",") if s.strip()}
    return _ALLOWED_SP_IDS


def _jwt_sub(token: str) -> str:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return ""
        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
        return json.loads(base64.b64decode(payload)).get("sub", "")
    except Exception:
        return ""


def _validate_token(authorization: str | None) -> bool:
    if os.getenv("OTEL_AUTH_DISABLED", "").lower() == "true":
        return bool(authorization)
    if not authorization or not authorization.startswith("Bearer "):
        return False
    token = authorization[7:]
    if len(token.split(".")) != 3:
        return False
    allowed = _allowed_sp_ids()
    if not allowed:
        return True
    sub = _jwt_sub(token)
    if not sub:
        return False
    return "@" in sub or sub in allowed


# ── Metric parsing ────────────────────────────────────────────────────────────

CPU_STATE_COL = {"user": "cpu_user_percent", "system": "cpu_system_percent", "wait": "cpu_wait_percent"}
NET_DIR_COL = {"transmit": "network_sent_bytes", "receive": "network_received_bytes"}


def _attr(obj: dict, key: str) -> str | None:
    for a in obj.get("attributes", []):
        if a.get("key") == key:
            v = a.get("value", {})
            return v.get("stringValue") or (str(v["intValue"]) if "intValue" in v else None)
    return None


def _resource_attrs(resource: dict) -> dict[str, str]:
    attrs = {}
    for a in resource.get("attributes", []):
        k = a.get("key", "")
        v = a.get("value", {})
        if "stringValue" in v:
            attrs[k] = v["stringValue"]
        elif "intValue" in v:
            attrs[k] = str(v["intValue"])
        elif "boolValue" in v:
            attrs[k] = str(v["boolValue"]).lower()
    return attrs


def _num(dp: dict) -> float | None:
    if "asDouble" in dp:
        return dp["asDouble"]
    if "asInt" in dp:
        return float(dp["asInt"])
    return None


def _ts(time_unix_nano: str | int) -> datetime:
    return datetime.fromtimestamp(int(time_unix_nano) / 1e9, tz=timezone.utc)


def parse_payload(payload: dict) -> list[dict]:
    rows: dict[tuple, dict] = {}

    for rm in payload.get("resourceMetrics", []):
        ra = _resource_attrs(rm.get("resource", {}))
        cluster_id = ra.get("cluster_id", ra.get("host.name", "unknown"))
        instance_id = ra.get("instance_id", ra.get("host.id", "unknown"))
        is_driver = ra.get("is_driver", "false") == "true"
        node_type = ra.get("node_type", "")

        for sm in rm.get("scopeMetrics", []):
            for metric in sm.get("metrics", []):
                name = metric.get("name", "")
                dps = []
                if "gauge" in metric:
                    dps = metric["gauge"].get("dataPoints", [])
                elif "sum" in metric:
                    dps = metric["sum"].get("dataPoints", [])

                for dp in dps:
                    val = _num(dp)
                    if val is None:
                        continue
                    ts = _ts(dp.get("timeUnixNano", dp.get("startTimeUnixNano", "0"))).replace(microsecond=0)
                    key = (cluster_id, instance_id, ts)
                    if key not in rows:
                        rows[key] = dict(
                            cluster_id=cluster_id, instance_id=instance_id,
                            is_driver=is_driver, node_type=node_type, ts=ts,
                            cpu_user_percent=None, cpu_system_percent=None, cpu_wait_percent=None,
                            mem_used_percent=None, mem_swap_percent=None, mem_available_bytes=None,
                            network_sent_bytes=None, network_received_bytes=None,
                            network_errors=None, network_drops=None,
                            disk_used_percent=None, disk_io_time_ms=None,
                            disk_ops_read=None, disk_ops_write=None,
                            load_1m=None, load_5m=None, load_15m=None,
                            paging_in=None, paging_out=None,
                            process_count=None, inodes_used_percent=None,
                        )
                    r = rows[key]

                    if name == "system.cpu.utilization":
                        state = _attr(dp, "state")
                        if state in CPU_STATE_COL:
                            pct = val * 100 if val <= 1.0 else val
                            r[f"_cpu_{state}_sum"] = r.get(f"_cpu_{state}_sum", 0) + pct
                            r[f"_cpu_{state}_cnt"] = r.get(f"_cpu_{state}_cnt", 0) + 1
                    elif name == "system.memory.utilization":
                        r["mem_used_percent"] = val * 100 if val <= 1.0 else val
                    elif name == "system.memory.usage":
                        r[f"_mem_{_attr(dp, 'state')}"] = val
                    elif name == "system.paging.utilization":
                        r["mem_swap_percent"] = val * 100 if val <= 1.0 else val
                    elif name in ("system.disk.utilization", "system.filesystem.utilization"):
                        if _attr(dp, "type") not in ("devfs", "tmpfs", "autofs"):
                            pct = val * 100 if val <= 1.0 else val
                            if r["disk_used_percent"] is None or pct > r["disk_used_percent"]:
                                r["disk_used_percent"] = pct
                    elif name == "system.network.io":
                        col = NET_DIR_COL.get(_attr(dp, "direction"))
                        if col:
                            r[col] = int(val)
                    elif name == "system.network.errors":
                        r["network_errors"] = (r["network_errors"] or 0) + int(val)
                    elif name == "system.network.dropped":
                        r["network_drops"] = (r["network_drops"] or 0) + int(val)
                    elif name == "system.disk.io_time":
                        r["disk_io_time_ms"] = (r["disk_io_time_ms"] or 0) + val
                    elif name == "system.disk.operations":
                        d = _attr(dp, "direction")
                        if d == "read":
                            r["disk_ops_read"] = (r["disk_ops_read"] or 0) + int(val)
                        elif d == "write":
                            r["disk_ops_write"] = (r["disk_ops_write"] or 0) + int(val)
                    elif name == "system.cpu.load_average.1m":
                        r["load_1m"] = val
                    elif name == "system.cpu.load_average.5m":
                        r["load_5m"] = val
                    elif name == "system.cpu.load_average.15m":
                        r["load_15m"] = val
                    elif name == "system.paging.operations":
                        d = _attr(dp, "direction")
                        if d == "page_in":
                            r["paging_in"] = (r["paging_in"] or 0) + int(val)
                        elif d == "page_out":
                            r["paging_out"] = (r["paging_out"] or 0) + int(val)
                    elif name == "system.processes.count":
                        if _attr(dp, "status") in (None, "running"):
                            r["process_count"] = (r["process_count"] or 0) + int(val)
                    elif name == "system.filesystem.inodes.usage":
                        state = _attr(dp, "state")
                        r[f"_inodes_{state}"] = r.get(f"_inodes_{state}", 0) + val

    for r in rows.values():
        for state, col in CPU_STATE_COL.items():
            s, c = f"_cpu_{state}_sum", f"_cpu_{state}_cnt"
            if s in r and r.get(c):
                r[col] = r[s] / r[c]
            r.pop(s, None)
            r.pop(c, None)

        mu = r.pop("_mem_used", 0) or 0
        mf = r.pop("_mem_free", 0) or 0
        mc = r.pop("_mem_cached", 0) or 0
        mb = r.pop("_mem_buffered", 0) or 0
        for k in [k for k in r if k.startswith("_mem_")]:
            r.pop(k)
        total = mu + mf + mc + mb
        if total > 0 and not r["mem_used_percent"]:
            r["mem_used_percent"] = (mu / total) * 100
        if mf + mc + mb > 0:
            r["mem_available_bytes"] = int(mf + mc + mb)

        iu = r.pop("_inodes_used", 0) or 0
        if_ = r.pop("_inodes_free", 0) or 0
        if iu + if_ > 0:
            r["inodes_used_percent"] = iu / (iu + if_) * 100

    return list(rows.values())


# ── Route ─────────────────────────────────────────────────────────────────────

@router.post("/v1/metrics")
async def receive_metrics(
    request: Request,
    authorization: str | None = Header(default=None),
    x_forwarded_access_token: str | None = Header(default=None, alias="X-Forwarded-Access-Token"),
):
    """Receive OTLP/HTTP JSON metrics from cluster node OTel Collectors."""
    effective_auth = authorization or (f"Bearer {x_forwarded_access_token}" if x_forwarded_access_token else None)
    if not _validate_token(effective_auth):
        sub = _jwt_sub(effective_auth[7:]) if effective_auth and effective_auth.startswith("Bearer ") else ""
        allowed = _allowed_sp_ids()
        if sub and "@" not in sub and allowed and sub not in allowed:
            raise HTTPException(status_code=403, detail=f"SP {sub} not in OTEL_ALLOWED_SP_IDS")
        raise HTTPException(status_code=401, detail="Invalid or missing authorization")

    try:
        body = await request.body()
        if "gzip" in request.headers.get("content-encoding", ""):
            body = gzip.decompress(body)
        payload = json.loads(body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid payload: {e}")

    try:
        rows = parse_payload(payload)
        if rows:
            db.buffer.add(rows)  # returns immediately — flushed to Delta by background task
            logger.debug(f"OTel: buffered {len(rows)} rows")
    except Exception as e:
        logger.error(f"OTel buffer error: {e}")
        raise HTTPException(status_code=500, detail=str(e)[:300])

    return {}
