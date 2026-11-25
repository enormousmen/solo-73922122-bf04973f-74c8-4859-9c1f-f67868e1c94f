#!/usr/bin/env python3
"""进程重启日志续读 + 响应乱序自测（仅用 Python 标准库）。

针对题目里调试页日志续读的几个不变量，对真实 uvicorn 进程做端到端验证：

  A. worker 进程重启后，页面凭 boot_id 自动切到新进程；即使新缓冲日志编号已超过
     旧游标，也能用 limit=200 补读新缓冲里仍可取到的日志，并以新进程 last_id 续读；
  B. 切换后，迟到的旧进程响应不能改动列表或游标；
  C. 同一进程的自动轮询与“写样例后刷新”乱序返回时，每条日志只显示一次、游标不倒退；
  D. 一个 worker 重启/刷新，另一个 worker 照常独立刷新。

受限环境可能禁止 kill 系统调用，因此 worker1 由 scripts/_restart_launcher.py 包裹：
标记文件出现后旧 uvicorn 优雅退出并 execve 成同端口的全新进程，等价 compose restart。
脚本在临时目录、独立端口上起自己的 admin + 两个 worker，不影响常驻服务。

用法：python3 scripts/restart_log_check.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = str(Path(__file__).resolve().parent / "_restart_launcher.py")

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ PASS  {name}" + (f" — {detail}" if detail else ""), flush=True)
    else:
        FAIL += 1
        print(f"  ❌ FAIL  {name}" + (f" — {detail}" if detail else ""), flush=True)


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(method: str, url: str, timeout: float = 5.0):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get(url: str):
    return http("GET", url)


def post(url: str):
    return http("POST", url)


def wait_health(port: int, timeout: float = 25.0, want_boot=None):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            last = get(f"http://127.0.0.1:{port}/healthz")
            if want_boot is None or last.get("boot_id") == want_boot:
                return last
        except Exception:
            pass
        time.sleep(0.25)
    raise AssertionError(f"端口 {port} 未就绪（want_boot={want_boot}），last={last}")


def wait_port_down(port: int, timeout: float = 15.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            get(f"http://127.0.0.1:{port}/healthz")
        except Exception:
            return True
        time.sleep(0.2)
    return False


class PageLogState:
    """与 admin_service/static/app.js 的 handleBootChange/applyLogResponse 同构。

    保持同样的语义：boot 切换即重置游标并补读；旧 boot 迟到响应判 stale 丢弃；
    同进程按 id 去重合并，last_id 只增不减。
    """

    def __init__(self) -> None:
        self.boot_id = None
        self.known: list[str] = []
        self.last_id = 0
        self.ids: list[int] = []

    def apply(self, data: dict) -> str:
        bid = data.get("boot_id")
        if not bid:
            return "error-envelope"
        if self.boot_id is None:
            self.boot_id = bid
            self.known.append(bid)
            action = "adopted"
        elif bid == self.boot_id:
            action = "same"
        elif bid in self.known:
            return "stale"
        else:
            self.boot_id = bid
            self.known.append(bid)
            self.last_id = 0
            self.ids = []
            action = "switched"
        for r in data.get("logs", []):
            if r["id"] not in self.ids:
                self.ids.append(r["id"])
        new_last = data.get("last_id", 0) or 0
        if new_last > self.last_id:
            self.last_id = new_last
        return action


class ProcMgr:
    def __init__(self, logdir: Path) -> None:
        self.logdir = logdir

    def spawn(self, name: str, argv: list[str], env: dict) -> subprocess.Popen:
        out = open(self.logdir / f"{name}.out", "ab")
        err = open(self.logdir / f"{name}.err", "ab")
        return subprocess.Popen(argv, cwd=str(ROOT), env=dict(os.environ, **env),
                                stdout=out, stderr=err)


def main() -> int:
    py = sys.executable
    tmp = Path(tempfile.mkdtemp(prefix="restart-log-check-"))
    logdir = tmp / "logs"
    for d in ("admin", "worker1", "worker2", "logs"):
        (tmp / d).mkdir(parents=True, exist_ok=True)

    p_admin, p_w1, p_w2 = free_port(), free_port(), free_port()
    admin = f"http://127.0.0.1:{p_admin}"
    w1 = f"http://127.0.0.1:{p_w1}"
    flag = tmp / "restart.flag"
    mgr = ProcMgr(logdir)

    def logs(wid: str, limit: int = 200, after_id=None):
        q = f"limit={limit}" if after_id is None else f"after_id={after_id}&limit=1000"
        return get(f"{admin}/api/workers/{wid}/logs?{q}")

    procs = []
    try:
        procs.append(mgr.spawn("admin", [py, "-m", "uvicorn", "admin_service.main:app",
                                         "--host", "127.0.0.1", "--port", str(p_admin),
                                         "--no-access-log", "--log-level", "warning"], {
            "PYTHONPATH": str(ROOT),
            "ADMIN_DATA_DIR": str(tmp / "admin"),
            "WORKER_IDS": "worker1,worker2",
            "WORKER1_URL": w1,
            "WORKER2_URL": f"http://127.0.0.1:{p_w2}",
        }))
        wait_health(p_admin)

        common_w1 = {
            "PYTHONPATH": str(ROOT),
            "WORKER_ID": "worker1",
            "ADMIN_URL": admin,
            "WORKER_DATA_DIR": str(tmp / "worker1"),
            "POLL_INTERVAL": "1.0",
            "REEXEC_CFG": json.dumps({
                "module": "worker_service.main:app", "port": p_w1,
                "flag": str(flag), "new_env": {"WORKER_LOG_START_ID": "500"},
            }),
        }
        procs.append(mgr.spawn("worker1", [py, LAUNCHER], common_w1))
        procs.append(mgr.spawn("worker2", [py, "-m", "uvicorn", "worker_service.main:app",
                                           "--host", "127.0.0.1", "--port", str(p_w2),
                                           "--no-access-log", "--log-level", "warning"], {
            "PYTHONPATH": str(ROOT),
            "WORKER_ID": "worker2",
            "ADMIN_URL": admin,
            "WORKER_DATA_DIR": str(tmp / "worker2"),
            "POLL_INTERVAL": "1.0",
        }))
        boot_before = wait_health(p_w1)["boot_id"]
        w2_boot_before = wait_health(p_w2)["boot_id"]

        # ------------------------------------------------ A. 重启续读
        print("== A. 进程重启：boot 切换 + 新编号超过旧游标 + 自动补读 ==", flush=True)
        page = PageLogState()
        first = logs("worker1", limit=200)
        check("首读带 boot_id", bool(first.get("boot_id")), first["boot_id"][:12])
        check("首读被采纳", page.apply(first) == "adopted" and page.boot_id == boot_before)
        for _ in range(3):
            post(f"{admin}/api/workers/worker1/sample-logs")
        time.sleep(0.6)
        page.apply(logs("worker1", after_id=page.last_id, limit=1000))
        old_cursor = page.last_id
        check("重启前游标已推进", old_cursor > first["last_id"], f"lastId={old_cursor}")

        flag.write_text("restart", encoding="utf-8")
        check("旧进程退场、端口短暂释放", wait_port_down(p_w1))
        boot_after = wait_health(p_w1, timeout=25)["boot_id"]
        check("同端口由全新进程接管，boot_id 改变", boot_after != boot_before,
              f"{boot_before[:8]} -> {boot_after[:8]}")

        overshoot = logs("worker1", after_id=600, limit=1000)
        check("旧游标(600)越过新缓冲 -> 空 logs 但带新 boot_id（修复前的漏日志卡死点）",
              overshoot.get("boot_id") == boot_after and overshoot["logs"] == [],
              f"n={len(overshoot['logs'])}")
        same_id_space = logs("worker1", after_id=old_cursor, limit=1000)
        check("新编号(>=500)已超过旧游标(%d)，响应来自新 boot" % old_cursor,
              same_id_space.get("boot_id") == boot_after
              and all(r["id"] >= 500 for r in same_id_space["logs"]),
              f"ids={[r['id'] for r in same_id_space['logs']][:3]}")

        ov = get(f"{admin}/api/overview")
        live1 = next(w for w in ov["workers"] if w["id"] == "worker1")["live"]
        action = page.apply({"boot_id": live1["boot_id"], "booted_at": live1.get("booted_at"),
                             "logs": [], "last_id": 0})
        check("概览先发现陌生 boot -> switched", action == "switched", action)
        backfill = logs("worker1", limit=200)
        check("补读响应属于新进程", page.apply(backfill) in ("adopted", "same"))
        ids = [r["id"] for r in backfill["logs"]]
        check("补读到新进程缓冲日志，编号从 500 起", bool(ids) and min(ids) >= 500,
              f"{min(ids)}..{max(ids)} 共 {len(ids)} 条")
        check("补读含 [process-boot] 启动行",
              any("[process-boot]" in r["message"] for r in backfill["logs"]))
        check("切换后游标=新进程 last_id", page.last_id == backfill["last_id"],
              f"{page.last_id} == {backfill['last_id']}")

        # ------------------------------------------------ B. 迟到旧响应
        print("== B. 切换后迟到的旧进程响应不改列表/游标 ==", flush=True)
        cur, saved = page.last_id, list(page.ids)
        stale = page.apply({"worker_id": "worker1", "boot_id": boot_before,
                            "logs": [{"id": old_cursor + 1, "level": "INFO",
                                      "message": "late old", "ts": "", "logger_ts": ""}],
                            "last_id": old_cursor + 1})
        check("旧 boot 迟到响应判 stale", stale == "stale", stale)
        check("stale 后游标不变", page.last_id == cur)
        check("stale 后列表不变", page.ids == saved)

        # ------------------------------------------------ C. 同进程乱序去重
        print("== C. 同进程并发/乱序响应：去重且游标不倒退 ==", flush=True)
        post(f"{admin}/api/workers/worker1/sample-logs")
        time.sleep(0.5)
        base_cursor = page.last_id
        # 两个并发请求：一个用旧游标(模拟慢的自动轮询)，一个用最新游标(写样例后刷新)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            fut_old = ex.submit(lambda: logs("worker1", after_id=base_cursor - 1, limit=1000))
            fut_new = ex.submit(lambda: logs("worker1", after_id=base_cursor, limit=1000))
            resp_new = fut_new.result()
            resp_old = fut_old.result()  # 故意先拿“新”的，再让“旧”的晚到
        # 乱序应用：先新后旧
        a_new = page.apply(resp_new)
        cursor_after_new = page.last_id
        a_old = page.apply(resp_old)
        check("两个响应都属于当前 boot", a_new == "same" and a_old == "same")
        check("乱序应用后无重复 id", len(page.ids) == len(set(page.ids)),
              f"{len(page.ids)} 条，去重后 {len(set(page.ids))}")
        check("乱序应用后续读位置不倒退", page.last_id >= cursor_after_new,
              f"{cursor_after_new} -> {page.last_id}")
        check("游标只增不减（不低于 base）", page.last_id >= base_cursor)

        # ------------------------------------------------ D. 另一进程独立
        print("== D. worker2 全程独立刷新 ==", flush=True)
        w2 = PageLogState()
        w2.apply(logs("worker2", limit=200))
        post(f"{admin}/api/workers/worker2/sample-logs")
        time.sleep(0.6)
        w2.apply(logs("worker2", after_id=w2.last_id, limit=1000))
        check("worker2 boot 从未变化", w2.boot_id == w2_boot_before)
        check("worker2 游标正常前进", w2.last_id >= 1)
        check("worker1 仍跟随新 boot", page.boot_id == boot_after)

        print(f"\n结果：{PASS} passed, {FAIL} failed", flush=True)
        return 1 if FAIL else 0
    finally:
        # 受限环境可能无法 terminate；进程绑定临时端口，退出后由运行环境回收。
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
