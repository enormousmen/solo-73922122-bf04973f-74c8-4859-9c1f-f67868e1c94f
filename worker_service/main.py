"""worker 服务：轮询管理端配置 + 本地到期看门狗 + 样例日志/状态 API。"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Query

from common.timeutil import to_iso, utcnow
from worker_service import sample_module
from worker_service.state import WorkerState

# ---------------------------------------------------------------- 环境配置
WORKER_ID = os.getenv("WORKER_ID", "worker1")
ADMIN_URL = os.getenv("ADMIN_URL", "http://admin:8000").rstrip("/")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1.0"))
DATA_DIR = Path(os.getenv("WORKER_DATA_DIR", f".runtime/{WORKER_ID}-data"))
HEARTBEAT_INTERVAL = float(os.getenv("HEARTBEAT_INTERVAL", "5.0"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
# 轮询很频繁，屏蔽 httpx/httpcore 的逐请求 INFO，避免淹没样例日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
ops_logger = logging.getLogger("worker_service")

ring = sample_module.configure()
state = WorkerState(WORKER_ID, DATA_DIR)

# ---------------------------------------------------------------- 进程身份
# 每次进程启动生成全新 boot_id；环形缓冲是进程内状态，重启后日志 id 从 1 重新计数，
# 调试页据此识别“换进程”并把续读游标重置到新缓冲，避免 after_id 越过新缓冲而漏日志。
BOOT_ID = os.getenv("WORKER_BOOT_ID") or uuid.uuid4().hex
BOOTED_AT = to_iso(utcnow())


async def poll_config_loop(app: FastAPI) -> None:
    """周期性拉取配置。任何失败都不影响本地看门狗按最后截止时间到期。"""
    client: httpx.AsyncClient = app.state.http
    url = f"{ADMIN_URL}/config/{WORKER_ID}"
    while True:
        state.last_poll_attempt_at = to_iso(utcnow())
        try:
            resp = await client.get(url, timeout=httpx.Timeout(4.0, connect=2.0))
            if resp.status_code == 503:
                # 管理端人工暂停下发：收不到任何配置内容，按“通道中断”处理
                first = state.consecutive_failures == 0
                state.consecutive_failures += 1
                state.delivery_paused = True
                state.last_error = "管理端已暂停向该进程下发配置 (HTTP 503)"
                if first:
                    ops_logger.warning("%s: config delivery paused by admin", WORKER_ID)
            else:
                resp.raise_for_status()
                cfg = resp.json()
                recovered = state.consecutive_failures > 0 or state.delivery_paused
                state.apply_config(cfg)
                state.consecutive_failures = 0
                state.delivery_paused = False
                state.last_error = None
                state.last_poll_success_at = to_iso(utcnow())
                if recovered:
                    ops_logger.info("%s: config channel restored", WORKER_ID)
        except Exception as exc:  # noqa: BLE001 - 轮询循环必须永不退出
            first = state.consecutive_failures == 0
            state.consecutive_failures += 1
            state.delivery_paused = False
            state.last_error = f"{type(exc).__name__}: {exc}"
            if first:
                ops_logger.warning(
                    "%s: config poll failed (%s); keep last deadline %s",
                    WORKER_ID,
                    state.last_error,
                    to_iso(state.debug_deadline),
                )
        await asyncio.sleep(POLL_INTERVAL)


async def deadline_watchdog_loop() -> None:
    """每 0.2 秒检查本地截止时间：到点自行结束 DEBUG。"""
    while True:
        try:
            state.expire_locally_if_due()
        except Exception as exc:  # noqa: BLE001
            ops_logger.exception("watchdog error: %s", exc)
        await asyncio.sleep(0.2)


async def heartbeat_loop() -> None:
    tick = 0
    # 启动后稍等，避免和启动日志挤在一起
    await asyncio.sleep(2.0)
    while True:
        tick += 1
        try:
            sample_module.heartbeat_tick(tick)
        except Exception as exc:  # noqa: BLE001
            ops_logger.exception("heartbeat error: %s", exc)
        await asyncio.sleep(HEARTBEAT_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient()
    tasks = [
        asyncio.create_task(poll_config_loop(app), name="poll-config"),
        asyncio.create_task(deadline_watchdog_loop(), name="deadline-watchdog"),
        asyncio.create_task(heartbeat_loop(), name="heartbeat"),
    ]
    ops_logger.info(
        "worker %s started: admin=%s poll=%.1fs data=%s actual_level=%s deadline=%s boot=%s",
        WORKER_ID,
        ADMIN_URL,
        POLL_INTERVAL,
        DATA_DIR,
        sample_module.actual_level_name(),
        to_iso(state.debug_deadline),
        BOOT_ID,
    )
    # 写进样例日志环形缓冲，调试页面板上能直接看到“新进程已启动、缓冲重新计数”
    sample_module.logger.info(
        "[process-boot] worker %s 新进程已启动 boot_id=%s，日志环形缓冲重新计数",
        WORKER_ID,
        BOOT_ID,
    )
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await app.state.http.aclose()


app = FastAPI(title=f"worker {WORKER_ID}", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "worker_id": WORKER_ID, "boot_id": BOOT_ID}


@app.get("/local/state")
async def get_state():
    snap = state.snapshot()
    # 进程身份：调试页用它识别进程重启（状态接口和日志接口各带一份，任一先到都能发现）
    snap["boot_id"] = BOOT_ID
    snap["booted_at"] = BOOTED_AT
    return snap


@app.get("/local/logs")
async def get_logs(
    limit: int = Query(200, ge=1, le=1000),
    after_id: int = Query(0, ge=0),
    level: Optional[str] = Query(None),
):
    levels = [lv.strip() for lv in level.split(",")] if level else None
    return {
        "worker_id": WORKER_ID,
        "boot_id": BOOT_ID,
        "booted_at": BOOTED_AT,
        **ring.query(limit=limit, after_id=after_id, levels=levels),
    }


@app.post("/local/write-sample")
async def write_sample():
    result = sample_module.write_sample_batch(origin="web-console")
    result["worker_id"] = WORKER_ID
    return result
