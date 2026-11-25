"""管理服务（FastAPI）：调试页面 + 管理 API + 给 worker 拉取的配置接口。"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from admin_service.store import VALID_LEVELS, ConfigStore
from common.timeutil import to_iso, utcnow

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

WORKER_IDS = [
    w.strip()
    for w in os.getenv("WORKER_IDS", "worker1,worker2").split(",")
    if w.strip()
]
DATA_DIR = Path(os.getenv("ADMIN_DATA_DIR", ".runtime/admin-data"))


def worker_base_url(worker_id: str) -> str:
    env_key = "".join(ch.upper() if ch.isalnum() else "_" for ch in worker_id) + "_URL"
    return os.getenv(env_key, f"http://{worker_id}:8000").rstrip("/")


store = ConfigStore(DATA_DIR, WORKER_IDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=2.0))
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(title="日志级别临时调试控制台", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------- 页面
@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok", "time": to_iso(utcnow())}


# ---------------------------------------------------------------- 请求模型
class LevelBody(BaseModel):
    level: str


class DebugBody(BaseModel):
    seconds: float = Field(..., gt=0, le=7 * 24 * 3600)


# ---------------------------------------------------------------- 给 worker 的配置下发接口
@app.get("/config/{worker_id}")
async def worker_pull_config(worker_id: str):
    """worker 轮询此接口获取配置。

    管理端对某个 worker 点“暂停接收配置”后，这里直接返回 503 且不给出任何
    配置内容，模拟配置通道中断；worker 必须依靠上次收到的截止时间自行到期。
    """
    cfg = store.get(worker_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"unknown worker: {worker_id}")
    if cfg.get("paused"):
        return JSONResponse(
            status_code=503,
            content={"detail": "config delivery paused by operator", "paused": True},
        )
    return {
        "worker_id": worker_id,
        "regular_level": cfg["regular_level"],
        "regular_rev": cfg["regular_rev"],
        "debug_deadline": cfg["debug_deadline"],
        "debug_renewals": cfg["debug_renewals"],
        "server_time": to_iso(utcnow()),
    }


# ---------------------------------------------------------------- 管理 API
@app.get("/api/overview")
async def overview() -> Dict[str, Any]:
    cfgs = store.all()

    async def collect(wid: str) -> Dict[str, Any]:
        cfg = cfgs.get(wid)
        live, error = await _fetch_worker(wid, "/local/state")
        return {
            "id": wid,
            "url": worker_base_url(wid),
            "config": cfg,
            "live": live,
            "live_error": error,
        }

    workers = await asyncio.gather(*(collect(wid) for wid in WORKER_IDS))
    return {"server_time": to_iso(utcnow()), "workers": workers}


@app.post("/api/workers/{worker_id}/level")
async def set_level(worker_id: str, body: LevelBody):
    _require_worker(worker_id)
    try:
        cfg = store.set_regular_level(worker_id, body.level)
    except ValueError:
        raise HTTPException(400, f"level must be one of {VALID_LEVELS}")
    return {"ok": True, "config": cfg}


@app.post("/api/workers/{worker_id}/debug/start")
async def debug_start(worker_id: str, body: DebugBody):
    _require_worker(worker_id)
    return {"ok": True, "config": store.start_debug(worker_id, body.seconds)}


@app.post("/api/workers/{worker_id}/debug/renew")
async def debug_renew(worker_id: str, body: DebugBody):
    _require_worker(worker_id)
    return {"ok": True, "config": store.renew_debug(worker_id, body.seconds)}


@app.post("/api/workers/{worker_id}/debug/cancel")
async def debug_cancel(worker_id: str):
    _require_worker(worker_id)
    return {"ok": True, "config": store.cancel_debug(worker_id)}


@app.post("/api/workers/{worker_id}/pause")
async def pause(worker_id: str):
    _require_worker(worker_id)
    return {"ok": True, "config": store.set_paused(worker_id, True)}


@app.post("/api/workers/{worker_id}/resume")
async def resume(worker_id: str):
    _require_worker(worker_id)
    return {"ok": True, "config": store.set_paused(worker_id, False)}


@app.post("/api/workers/{worker_id}/sample-logs")
async def trigger_sample_logs(worker_id: str):
    _require_worker(worker_id)
    data, error = await _post_worker(worker_id, "/local/write-sample")
    if error:
        raise HTTPException(502, f"worker {worker_id} unreachable: {error}")
    return {"ok": True, "result": data}


@app.post("/api/sample-logs/write-all")
async def trigger_all():
    results = await asyncio.gather(
        *(_post_worker(wid, "/local/write-sample") for wid in WORKER_IDS)
    )
    out = {}
    for wid, (data, error) in zip(WORKER_IDS, results):
        out[wid] = {"result": data, "error": error}
    return {"ok": all(e is None for _, e in results), "workers": out}


@app.get("/api/workers/{worker_id}/logs")
async def get_logs(
    worker_id: str, limit: int = 200, after_id: int = 0
) -> Dict[str, Any]:
    _require_worker(worker_id)
    data, error = await _fetch_worker(
        worker_id, "/local/logs", params={"limit": limit, "after_id": after_id}
    )
    if error:
        return {"worker_id": worker_id, "logs": [], "error": error}
    return data


# ---------------------------------------------------------------- 辅助
def _require_worker(worker_id: str) -> None:
    if not store.exists(worker_id):
        raise HTTPException(404, f"unknown worker: {worker_id}")


async def _fetch_worker(
    worker_id: str, path: str, params: Optional[Dict[str, Any]] = None
):
    client: httpx.AsyncClient = app.state.http
    try:
        resp = await client.get(worker_base_url(worker_id) + path, params=params)
        resp.raise_for_status()
        return resp.json(), None
    except (httpx.HTTPError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


async def _post_worker(worker_id: str, path: str):
    client: httpx.AsyncClient = app.state.http
    try:
        resp = await client.post(worker_base_url(worker_id) + path)
        resp.raise_for_status()
        return resp.json(), None
    except (httpx.HTTPError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
