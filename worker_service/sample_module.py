"""样例业务模块：两个 worker 各跑一份，用来演示日志级别热调整。

模块持有名为 ``sample_module`` 的 logger，调试页调整的“常规日志级别”
和临时 DEBUG 都作用在这个 logger 上。
"""
from __future__ import annotations

import itertools
import logging
import random
import time
from typing import Any, Dict, List

from worker_service.logbuffer import RingBufferHandler

LOGGER_NAME = "sample_module"
logger = logging.getLogger(LOGGER_NAME)

_LEVELS = [
    ("DEBUG", logging.DEBUG, "明细数据: payload={%r}"),
    ("INFO", logging.INFO, "常规进度: 任务执行完成"),
    ("WARNING", logging.WARNING, "资源预警: 队列积压开始升高"),
    ("ERROR", logging.ERROR, "模拟故障: 调用下游失败(样例，非真实错误)"),
]

_seq = itertools.count(1)
_ring: RingBufferHandler | None = None
_stdout: logging.Handler | None = None


def configure(ring_capacity: int = 1000) -> RingBufferHandler:
    """配置 sample_module logger：stdout + 环形缓冲，不向 root 传播以免重复。"""
    global _ring, _stdout
    logger.setLevel(logging.INFO)  # 启动默认常规级别，首次拉到配置后可能被覆盖
    logger.propagate = False

    if _ring is None:
        _ring = RingBufferHandler(capacity=ring_capacity)
        _ring.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
        )
        logger.addHandler(_ring)

    if _stdout is None:
        _stdout = logging.StreamHandler()
        _stdout.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
        )
        logger.addHandler(_stdout)

    return _ring


def ring_handler() -> RingBufferHandler:
    assert _ring is not None, "call configure() first"
    return _ring


def set_level(level_name: str) -> None:
    logger.setLevel(getattr(logging, level_name))


def actual_level_name() -> str:
    return logging.getLevelName(logger.getEffectiveLevel())


def write_sample_batch(origin: str = "manual") -> Dict[str, Any]:
    """写一组覆盖 DEBUG/INFO/WARNING/ERROR 的样例日志。

    返回每一级别在当前实际级别下是“已输出”还是“被过滤”，
    方便接口调用方核对级别是否真的生效。
    """
    seq = next(_seq)
    task_id = f"task-{random.randint(1000, 9999)}"
    emitted: List[str] = []
    suppressed: List[str] = []

    for name, no, template in _LEVELS:
        enabled = logger.isEnabledFor(no)
        if enabled:
            if name == "DEBUG":
                logger.debug(
                    "[sample #%04d via=%s] %s",
                    seq,
                    origin,
                    template % ({"seq": seq, "ts": time.time()},),
                )
            elif name == "INFO":
                logger.info("[sample #%04d via=%s] %s", seq, origin, template)
            elif name == "WARNING":
                logger.warning("[sample #%04d via=%s] %s", seq, origin, template)
            else:
                logger.error("[sample #%04d via=%s] %s", seq, origin, template)
            emitted.append(name)
        else:
            suppressed.append(name)

    return {
        "sequence": seq,
        "task_id": task_id,
        "origin": origin,
        "actual_level": actual_level_name(),
        "emitted": emitted,
        "suppressed": suppressed,
    }


def heartbeat_tick(tick_count: int) -> None:
    """后台心跳：每 5 秒一次 DEBUG 明细，每 3 次补一条 INFO 进展。

    这样开启 DEBUG 后日志页会自动滚动 DEBUG 行；到期恢复 INFO 后
    DEBUG 行立即消失，便于肉眼验证级别切换。
    """
    logger.debug(
        "[heartbeat #%04d] verbose detail: cache_keys=%d, rss_mb=%d",
        tick_count,
        random.randint(20, 200),
        random.randint(40, 120),
    )
    if tick_count % 3 == 0:
        logger.info("[heartbeat #%04d] periodic progress ok", tick_count)
