"""管理端配置存储：JSON 文件持久化。

关键要求：管理服务重启后必须沿用原来的 DEBUG 截止时间。
因此存储的是绝对截止时刻 debug_deadline(ISO8601 UTC)，
而不是“还剩多少秒”，重启也不会重新计时。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.timeutil import to_iso, utcnow, parse_iso

VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DEFAULT_LEVEL = "INFO"


def _new_worker_config() -> Dict[str, Any]:
    now = to_iso(utcnow())
    return {
        "regular_level": DEFAULT_LEVEL,
        # 常规级别版本号：每次修改 +1，worker 据此区分“新级别”和重复下发的旧配置
        "regular_rev": 1,
        "debug_deadline": None,
        "debug_started_at": None,
        "debug_renewals": 0,
        "paused": False,
        "created_at": now,
        "updated_at": now,
    }


class ConfigStore:
    def __init__(self, path: Path, worker_ids: List[str]):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.file = self.path / "admin-config.json"
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {"workers": {}}
        self._worker_ids = list(worker_ids)
        self._load()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        with self._lock:
            if self.file.exists():
                try:
                    loaded = json.loads(self.file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict) and isinstance(loaded.get("workers"), dict):
                        self._data = loaded
                except (json.JSONDecodeError, OSError):
                    # 文件损坏时不覆盖现场，另存后重建，避免丢掉原截止时间
                    backup = self.file.with_suffix(".corrupt.json")
                    try:
                        self.file.rename(backup)
                    except OSError:
                        pass
                    self._data = {"workers": {}}
            changed = False
            for wid in self._worker_ids:
                if wid not in self._data["workers"] or not isinstance(
                    self._data["workers"][wid], dict
                ):
                    self._data["workers"][wid] = _new_worker_config()
                    changed = True
                else:
                    # 补齐后续版本可能新增的字段
                    base = _new_worker_config()
                    for k, v in base.items():
                        self._data["workers"][wid].setdefault(k, v)
            if changed or not self.file.exists():
                self._flush_locked()

    def _flush_locked(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_name(self.file.name + ".tmp")
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.file)

    def _mutate(self, worker_id: str) -> Dict[str, Any]:
        cfg = self._data["workers"][worker_id]
        cfg["updated_at"] = to_iso(utcnow())
        self._flush_locked()
        return dict(cfg)

    # ---------- 查询 ----------
    def worker_ids(self) -> List[str]:
        return list(self._worker_ids)

    def exists(self, worker_id: str) -> bool:
        return worker_id in self._data["workers"]

    def get(self, worker_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cfg = self._data["workers"].get(worker_id)
            return dict(cfg) if cfg else None

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {wid: dict(cfg) for wid, cfg in self._data["workers"].items()}

    # ---------- 修改 ----------
    def set_regular_level(self, worker_id: str, level: str) -> Dict[str, Any]:
        level = level.upper()
        if level not in VALID_LEVELS:
            raise ValueError(f"invalid level: {level}")
        with self._lock:
            cfg = self._data["workers"][worker_id]
            if cfg.get("regular_level") != level:
                cfg["regular_level"] = level
                cfg["regular_rev"] = int(cfg.get("regular_rev", 1)) + 1
            return self._mutate(worker_id)

    def start_debug(self, worker_id: str, seconds: float) -> Dict[str, Any]:
        if seconds <= 0:
            raise ValueError("seconds must be > 0")
        with self._lock:
            now = utcnow()
            cfg = self._data["workers"][worker_id]
            cfg["debug_deadline"] = to_iso(now + _delta(seconds))
            cfg["debug_started_at"] = to_iso(now)
            cfg["debug_renewals"] = 0
            return self._mutate(worker_id)

    def renew_debug(self, worker_id: str, seconds: float) -> Dict[str, Any]:
        """续开：若当前截止时间仍在未来，则在原截止时间上顺延；否则重新开始。

        worker 收到新的绝对截止时间后直接以新时间为准（覆盖旧值）。
        """
        if seconds <= 0:
            raise ValueError("seconds must be > 0")
        with self._lock:
            now = utcnow()
            cfg = self._data["workers"][worker_id]
            old_deadline = parse_iso(cfg.get("debug_deadline"))
            if old_deadline is not None and old_deadline > now:
                base = old_deadline
                cfg["debug_renewals"] = int(cfg.get("debug_renewals", 0)) + 1
            else:
                base = now
                cfg["debug_started_at"] = to_iso(now)
                cfg["debug_renewals"] = 0
            cfg["debug_deadline"] = to_iso(base + _delta(seconds))
            return self._mutate(worker_id)

    def cancel_debug(self, worker_id: str) -> Dict[str, Any]:
        with self._lock:
            cfg = self._data["workers"][worker_id]
            cfg["debug_deadline"] = None
            cfg["debug_started_at"] = None
            return self._mutate(worker_id)

    def set_paused(self, worker_id: str, paused: bool) -> Dict[str, Any]:
        with self._lock:
            cfg = self._data["workers"][worker_id]
            cfg["paused"] = bool(paused)
            return self._mutate(worker_id)


def _delta(seconds: float):
    from datetime import timedelta

    return timedelta(seconds=float(seconds))
