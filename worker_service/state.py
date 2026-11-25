"""worker 侧配置状态机。

核心语义（与题目要求一一对应）：

1. 保存“最后收到的常规级别”和“最后收到的 DEBUG 绝对截止时间”。
2. DEBUG 进行中收到的新常规级别放入 pending，到期（本地看门狗或新配置）后才应用。
3. 与管理端断连（暂停下发 / 管理服务宕机）时，靠本地截止时间自行结束 DEBUG，
   恢复到最后收到的常规级别，不需要管理端再发“结束”指令。
4. 重连后若配置里的截止时间已经过去，跳过该 DEBUG，不重新进入 DEBUG。
5. 收到续开（新的、更晚的截止时间）后直接以新截止时间为准。
6. 最后收到的配置持久化到本地文件，worker 进程重启也不丢截止时间。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from common.timeutil import parse_iso, to_iso, utcnow
from worker_service import sample_module

ops_logger = logging.getLogger("worker_service")


def _control(message: str) -> None:
    """把级别切换事件写进样例模块日志（调试页可见）。

    按“不低于当前阈值”的级别发送，保证恢复到 WARNING/ERROR 后这条
    切换说明本身不会被新级别过滤掉。
    """
    threshold = sample_module.logger.level or logging.INFO
    levelno = max(logging.INFO, threshold)
    sample_module.logger.log(levelno, "[level-control] %s", message)


class WorkerState:
    def __init__(self, worker_id: str, data_dir: Path):
        self.worker_id = worker_id
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "worker-state.json"
        self._lock = threading.RLock()

        # ---- 最后收到 / 已应用的配置 ----
        self.regular_level: str = "INFO"
        self.applied_regular_rev: int = 0
        self.pending_regular_level: Optional[str] = None
        self.pending_regular_rev: Optional[int] = None
        self.debug_deadline = None  # datetime | None

        # ---- 轮询 / 连通性 ----
        self.last_config: Optional[Dict[str, Any]] = None
        self.last_config_received_at: Optional[str] = None
        self.last_poll_attempt_at: Optional[str] = None
        self.last_poll_success_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.consecutive_failures: int = 0
        self.delivery_paused: bool = False

        # ---- 去重 / 事件计数 ----
        self.skipped_deadline_raw: Optional[str] = None
        self.local_expiry_count: int = 0
        self.remote_expiry_count: int = 0

        expired_on_load = self._load()
        if expired_on_load:
            # 停机期间截止时间已过：把清空后的状态落盘，避免旧值残留
            self.persist()

    # ================================================================ 持久化
    def _load(self) -> bool:
        """加载本地状态。返回 True 表示持久化的 DEBUG 截止时间在停机期间已过期。"""
        if not self.state_file.exists():
            return False
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            ops_logger.warning("state file unreadable, start defaults: %s", exc)
            return False
        self.regular_level = data.get("regular_level", "INFO")
        self.applied_regular_rev = int(data.get("applied_regular_rev", 0))
        self.pending_regular_level = data.get("pending_regular_level")
        self.pending_regular_rev = data.get("pending_regular_rev")
        self.debug_deadline = parse_iso(data.get("debug_deadline"))
        expired_on_load = False
        # 重启后先按持久化状态对齐 logger
        if self.debug_deadline and self.debug_deadline > utcnow():
            sample_module.set_level("DEBUG")
        else:
            if self.debug_deadline is not None:
                # 停机期间已到期：清掉，按常规级别运行
                self.debug_deadline = None
                expired_on_load = True
            sample_module.set_level(self.regular_level)
        ops_logger.info(
            "loaded persisted state: level=%s rev=%s deadline=%s expired_on_load=%s",
            self.regular_level,
            self.applied_regular_rev,
            to_iso(self.debug_deadline),
            expired_on_load,
        )
        return expired_on_load

    def persist(self) -> None:
        payload = {
            "worker_id": self.worker_id,
            "regular_level": self.regular_level,
            "applied_regular_rev": self.applied_regular_rev,
            "pending_regular_level": self.pending_regular_level,
            "pending_regular_rev": self.pending_regular_rev,
            "debug_deadline": to_iso(self.debug_deadline),
            "saved_at": to_iso(utcnow()),
        }
        # 防御性：数据目录若被外部清理，写入前重建（Docker 卷场景同样安全）
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    # ================================================================ 工具
    def debug_active(self, now=None) -> bool:
        now = now or utcnow()
        return self.debug_deadline is not None and self.debug_deadline > now

    def _apply_regular_now(self, level: str, rev: int) -> bool:
        """立即应用常规级别。返回 sample logger 的实际级别是否发生变化。"""
        old = sample_module.actual_level_name()
        self.regular_level = level
        self.applied_regular_rev = max(self.applied_regular_rev, rev)
        if (
            self.pending_regular_rev is not None
            and rev >= self.pending_regular_rev
        ):
            self.pending_regular_level = None
            self.pending_regular_rev = None
        sample_module.set_level(level)
        changed = old != level
        if changed:
            _control(f"常规日志级别切换: {old} -> {level}")
        return changed

    def _restore_regular_after_debug(self) -> str:
        """DEBUG 结束后决定恢复到哪个常规级别：优先调试期间挂起的新级别。"""
        if self.pending_regular_level is not None:
            level = self.pending_regular_level
            rev = self.pending_regular_rev or self.applied_regular_rev
            self.pending_regular_level = None
            self.pending_regular_rev = None
            self.regular_level = level
            self.applied_regular_rev = max(self.applied_regular_rev, int(rev or 0))
            sample_module.set_level(level)
            return f"调试到期后应用挂起的新级别: {level}"
        sample_module.set_level(self.regular_level)
        return f"恢复最后收到的常规级别: {self.regular_level}"

    # ================================================================ 配置处理
    def apply_config(self, cfg: Dict[str, Any]) -> None:
        with self._lock:
            self.last_config = cfg
            self.last_config_received_at = to_iso(utcnow())

            now = utcnow()
            raw_deadline = cfg.get("debug_deadline")
            new_deadline = parse_iso(raw_deadline)
            future = new_deadline is not None and new_deadline > now

            level = str(cfg.get("regular_level", "INFO")).upper()
            rev = int(cfg.get("regular_rev", 0))
            newer_regular = rev > self.applied_regular_rev

            # 该 rev 是否正是此前在 DEBUG 期间挂起的那个级别
            was_same_pending = (
                newer_regular
                and self.pending_regular_rev is not None
                and self.pending_regular_rev == rev
            )

            # ---- 1) 常规级别：新配置里 DEBUG 仍有效 -> 挂起；否则立即应用
            pending_released_now = False
            if newer_regular and future:
                if self.pending_regular_rev != rev:
                    self.pending_regular_level = level
                    self.pending_regular_rev = rev
                    _control(
                        f"收到新常规级别 {level}(rev={rev})，"
                        "临时 DEBUG 仍在进行，先挂起，到期后再生效"
                    )
            elif newer_regular:
                self._apply_regular_now(level, rev)
                if was_same_pending:
                    # 轮询请求抢在本地看门狗之前发现截止时间已过，由本路径释放挂起级别
                    pending_released_now = True
                    _control(f"DEBUG 窗口已结束，到期后应用挂起的新级别: {level}")

            # ---- 2) DEBUG 截止时间：绝对时刻，新值直接覆盖旧值
            if future:
                old_deadline = self.debug_deadline
                self.debug_deadline = new_deadline
                sample_module.set_level("DEBUG")
                self.skipped_deadline_raw = None
                if old_deadline is None:
                    _control(
                        f"临时 DEBUG 已开启，截止时间 {to_iso(new_deadline)}（UTC）"
                    )
                elif old_deadline != new_deadline:
                    kind = "续开，截止时间更新" if new_deadline > old_deadline else "截止时间被缩短/重设"
                    _control(
                        f"收到{kind}: {to_iso(old_deadline)} -> {to_iso(new_deadline)}（UTC），以新截止时间为准"
                    )
            else:
                was_active = self.debug_active(now)
                had_deadline = self.debug_deadline is not None
                self.debug_deadline = None

                if newer_regular:
                    # 第 1 步已经设置好级别，这里只生成说明，避免覆盖刚下发的新级别
                    note = (
                        f"挂起的新级别 {self.regular_level} 已在到期后应用"
                        if pending_released_now
                        else f"常规级别已更新为 {self.regular_level}"
                    )
                else:
                    note = self._restore_regular_after_debug()

                if raw_deadline:
                    # 配置里带了截止时间但已过期：重连跳过，且同一过期值只提示一次
                    if self.skipped_deadline_raw != raw_deadline:
                        self.skipped_deadline_raw = raw_deadline
                        self.remote_expiry_count += 1
                        if was_active or had_deadline:
                            _control(
                                f"管理端下发的截止时间 {raw_deadline} 已过，按到期处理；{note}"
                            )
                        else:
                            _control(
                                f"重连后收到的 DEBUG 截止时间 {raw_deadline} 已过期，"
                                f"跳过该调试窗口，不进入 DEBUG；{note}"
                            )
                elif was_active or had_deadline:
                    self.remote_expiry_count += 1
                    _control(f"管理端取消了临时 DEBUG；{note}")

            self.persist()

    def expire_locally_if_due(self) -> bool:
        """本地看门狗调用：即使管理端完全不可达，到点也自行恢复。"""
        with self._lock:
            if self.debug_deadline is None:
                return False
            if self.debug_active():
                # 截止时间仍在未来，继续 DEBUG
                return False
            deadline = self.debug_deadline
            self.debug_deadline = None
            note = self._restore_regular_after_debug()
            self.local_expiry_count += 1
            _control(
                f"到达最后收到的截止时间 {to_iso(deadline)}（UTC），"
                f"未依赖管理端指令自行结束 DEBUG；{note}"
            )
            self.persist()
            return True

    # ================================================================ 状态快照
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = utcnow()
            active = self.debug_active(now)
            remaining = (self.debug_deadline - now).total_seconds() if active else 0.0
            return {
                "worker_id": self.worker_id,
                "actual_level": sample_module.actual_level_name(),
                "logger_level": logging.getLevelName(sample_module.logger.level),
                "regular_level": self.regular_level,
                "applied_regular_rev": self.applied_regular_rev,
                "pending_regular_level": self.pending_regular_level,
                "pending_regular_rev": self.pending_regular_rev,
                "debug_active": active,
                "debug_deadline": to_iso(self.debug_deadline),
                "seconds_remaining": max(0.0, round(remaining, 2)),
                "local_expiry_count": self.local_expiry_count,
                "remote_expiry_count": self.remote_expiry_count,
                "last_config": self.last_config,
                "last_config_received_at": self.last_config_received_at,
                "last_poll_attempt_at": self.last_poll_attempt_at,
                "last_poll_success_at": self.last_poll_success_at,
                "last_error": self.last_error,
                "delivery_paused": self.delivery_paused,
                "consecutive_failures": self.consecutive_failures,
                "state_file": str(self.state_file),
                "server_time": to_iso(now),
            }
