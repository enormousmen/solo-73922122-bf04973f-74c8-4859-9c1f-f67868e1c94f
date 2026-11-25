"""内存环形日志处理器：保存 sample_module 最近的日志，供调试页查看。"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from common.timeutil import to_iso, utcnow


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 1000):
        super().__init__()
        self._records: deque[Dict[str, Any]] = deque(maxlen=capacity)
        self._next_id = 1
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 日志处理器绝不能抛异常影响业务
            message = str(record.msg)
        entry = {
            "id": 0,  # 在锁内赋值
            "ts": to_iso(utcnow()),
            "logger_ts": to_iso(datetime.fromtimestamp(record.created, tz=timezone.utc)),
            "level": record.levelname,
            "levelno": record.levelno,
            "logger": record.name,
            "message": message,
        }
        with self._lock:
            entry["id"] = self._next_id
            self._next_id += 1
            self._records.append(entry)

    def query(
        self,
        limit: int = 200,
        after_id: int = 0,
        levels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        level_set = {lv.upper() for lv in levels} if levels else None
        with self._lock:
            items = list(self._records)
            last_id = self._next_id - 1
        filtered = [
            r
            for r in items
            if r["id"] > after_id and (level_set is None or r["level"] in level_set)
        ]
        if limit and len(filtered) > limit:
            filtered = filtered[-limit:]
        return {"logs": filtered, "last_id": last_id, "buffered": len(items)}
