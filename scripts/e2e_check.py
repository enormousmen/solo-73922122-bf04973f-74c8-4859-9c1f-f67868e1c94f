#!/usr/bin/env python3
"""端到端场景自检：常规级别、调试期挂起、断连到期、重连跳过、续开、双进程写日志。

只依赖 Python 标准库。默认对应 docker compose 暴露到宿主机的端口：
    ADMIN_URL   http://127.0.0.1:8000
    WORKER1_URL http://127.0.0.1:8001
    WORKER2_URL http://127.0.0.1:8002

本地多端口试跑时可用环境变量覆盖。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ADMIN = os.getenv("ADMIN_URL", "http://127.0.0.1:8000").rstrip("/")
W1 = os.getenv("WORKER1_URL", "http://127.0.0.1:8001").rstrip("/")
W2 = os.getenv("WORKER2_URL", "http://127.0.0.1:8002").rstrip("/")

PASS = 0
FAIL = 0


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ PASS  {name}" + (f" — {detail}" if detail else ""), flush=True)
    else:
        FAIL += 1
        print(f"  ❌ FAIL  {name}" + (f" — {detail}" if detail else ""), flush=True)


def request(method: str, url: str, body=None, timeout: float = 8.0):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        return e.code, parsed


def wait_for(desc: str, fn, timeout: float = 12.0, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"等待超时: {desc}; 最后状态={last!r}")


def wait_state(desc: str, predicate, base: str = W1, timeout: float = 12.0):
    """等到 state 满足 predicate，并返回该 state 快照。"""
    def _check():
        s = state(base)
        return s if predicate(s) else None
    return wait_for(desc, _check, timeout=timeout)


def state(base: str):
    code, data = request("GET", f"{base}/local/state")
    assert code == 200, f"state http {code}"
    return data


def admin_overview():
    code, data = request("GET", f"{ADMIN}/api/overview")
    assert code == 200
    return data


def set_level(wid, level):
    return request("POST", f"{ADMIN}/api/workers/{wid}/level", {"level": level})


def start_debug(wid, seconds):
    return request("POST", f"{ADMIN}/api/workers/{wid}/debug/start", {"seconds": seconds})


def renew_debug(wid, seconds):
    return request("POST", f"{ADMIN}/api/workers/{wid}/debug/renew", {"seconds": seconds})


def cancel_debug(wid):
    return request("POST", f"{ADMIN}/api/workers/{wid}/debug/cancel")


def pause(wid):
    return request("POST", f"{ADMIN}/api/workers/{wid}/pause")


def resume(wid):
    return request("POST", f"{ADMIN}/api/workers/{wid}/resume")


def worker_logs(base, limit=1000):
    code, data = request("GET", f"{base}/local/logs?limit={limit}")
    assert code == 200
    return data["logs"]


def has_log(base, substring, since_id=0):
    code, data = request("GET", f"{base}/local/logs?after_id={since_id}&limit=1000")
    return any(substring in r["message"] for r in data.get("logs", []))


def wait_log(base, substring, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if any(substring in r["message"] for r in worker_logs(base)):
            return True
        time.sleep(0.3)
    return False


def reset_worker(wid, base):
    try:
        resume(wid)
        cancel_debug(wid)
        set_level(wid, "INFO")
        time.sleep(1.2)
    except Exception as exc:  # noqa: BLE001
        log(f"cleanup {wid} error: {exc}")


def main() -> int:
    log(f"ADMIN={ADMIN} W1={W1} W2={W2}")

    # ------------------------------------------------ 0. 存活
    log("== 0. 服务存活 ==")
    for name, base in (("admin", ADMIN), ("worker1", W1), ("worker2", W2)):
        try:
            code, _ = request("GET", f"{base}/healthz", timeout=4)
            check(f"{name} /healthz", code == 200, f"HTTP {code}")
        except Exception as exc:  # noqa: BLE001
            check(f"{name} /healthz", False, str(exc))
            log("基础服务不可用，终止。请先 docker compose up 或启动本地进程。")
            return 2

    ov = admin_overview()
    ids = [w["id"] for w in ov["workers"]]
    check("overview 含 worker1/worker2", ids == ["worker1", "worker2"], str(ids))

    try:
        # ---------------------------------------- 1. 常规级别真正过滤日志
        log("== 1. 常规级别下发并生效（WARNING 过滤 INFO/DEBUG） ==")
        set_level("worker1", "WARNING")
        st = wait_state("worker1 actual WARNING", lambda s: s["actual_level"] == "WARNING")
        check("worker1 实际级别变为 WARNING", st["actual_level"] == "WARNING",
              f"rev={st['applied_regular_rev']}")
        code, res = request("POST", f"{W1}/local/write-sample")
        check("写样例返回 emitted/suppressed", code == 200 and "emitted" in res)
        check("WARNING 时输出 WARNING+ERROR",
              set(res["emitted"]) == {"WARNING", "ERROR"}, str(res["emitted"]))
        check("WARNING 时抑制 DEBUG+INFO",
              set(res["suppressed"]) == {"DEBUG", "INFO"}, str(res["suppressed"]))

        set_level("worker1", "INFO")
        wait_state("back to INFO", lambda s: s["actual_level"] == "INFO")

        # ---------------------------------------- 2. DEBUG 期间新常规级别挂起
        log("== 2. DEBUG 期间收到的新常规级别挂起，到期才应用 ==")
        start_debug("worker1", 8)
        st = wait_state("enter DEBUG",
                        lambda s: s["debug_active"] and s["actual_level"] == "DEBUG")
        check("开启后实际级别=DEBUG", st["actual_level"] == "DEBUG", st["debug_deadline"])

        set_level("worker1", "ERROR")
        time.sleep(1.6)  # 等轮询收到
        st = state(W1)
        check("DEBUG 中实际级别仍为 DEBUG", st["actual_level"] == "DEBUG")
        check("新级别 ERROR 处于 pending", st["pending_regular_level"] == "ERROR",
              f"pending={st['pending_regular_level']} rev={st['pending_regular_rev']}")

        t0 = time.monotonic()
        st = wait_state("expire then apply ERROR",
                        lambda s: s["actual_level"] == "ERROR" and not s["debug_active"],
                        timeout=12)
        elapsed = time.monotonic() - t0
        check("到期后应用挂起的 ERROR", st["actual_level"] == "ERROR" and not st["pending_regular_level"],
              f"等待 {elapsed:.1f}s, pending={st['pending_regular_level']}")
        check("日志记录“先挂起/到期生效”", wait_log(W1, "先挂起") and wait_log(W1, "到期后应用"))
        set_level("worker1", "INFO")
        wait_state("reset INFO", lambda s: s["actual_level"] == "INFO")

        # ---------------------------------------- 3. 断连到期（核心）
        log("== 3. 暂停下发（模拟断连）后，worker 按最后截止时间自行到期 ==")
        set_level("worker1", "INFO")
        wait_state("info again", lambda s: s["actual_level"] == "INFO")
        start_debug("worker1", 6)
        st = wait_state("debug on before pause", lambda s: s["debug_active"])
        deadline = datetime.fromisoformat(st["debug_deadline"].replace("Z", "+00:00"))

        code, cfg = pause("worker1")
        check("管理端暂停成功", code == 200)
        time.sleep(1.3)
        code, body = request("GET", f"{ADMIN}/config/worker1")
        check("暂停后 /config/worker1 返回 503 且无配置", code == 503 and isinstance(body, dict) and body.get("paused") is True,
              f"status={code}")

        st = wait_state("local expiry while paused",
                        lambda s: not s["debug_active"] and s["actual_level"] == "INFO",
                        timeout=11)
        # 与绝对截止时间比较（多进程共享宿主时钟）
        now = datetime.now(timezone.utc)
        lateness = (now - deadline).total_seconds()
        check("断连状态下自行结束 DEBUG 并恢复 INFO", True,
              f"相对截止时刻偏差 {lateness:+.2f}s（看门狗 0.2s + 轮询间隔）")
        check("到期动作发生在截止后 3 秒内（本地时钟自治）", 0 <= lateness < 3.0,
              f"lateness={lateness:.2f}s")
        check("本地到期计数增加", st["local_expiry_count"] >= 1, f"count={st['local_expiry_count']}")
        check("样例日志中有自行到期记录", wait_log(W1, "自行结束 DEBUG"))

        # 关键：仍处于暂停时，级别必须已经恢复（上面 wait 已保证），再等 2 秒确认不会反复
        time.sleep(2.0)
        st = state(W1)
        check("暂停期间不会重新进入 DEBUG", (not st["debug_active"]) and st["actual_level"] == "INFO")

        code, _ = resume("worker1")
        check("恢复下发成功", code == 200)
        time.sleep(1.8)
        st = state(W1)
        check("重连后仍保持 INFO（过期 DEBUG 被跳过）",
              st["actual_level"] == "INFO" and not st["debug_active"])
        check("重连跳过过期窗口有日志", wait_log(W1, "已过期") or wait_log(W1, "过期"))

        # ---------------------------------------- 4. 续开
        log("== 4. 续开：新截止时间生效，跨过原截止时刻仍保持 DEBUG ==")
        start_debug("worker1", 6)
        st = wait_state("debug started for renew", lambda s: s["debug_active"])
        d1 = st["debug_deadline"]
        renew_debug("worker1", 20)
        st = wait_state("deadline updated by renew",
                        lambda s: s["debug_deadline"] not in (None, d1) and s["debug_active"])
        d2 = st["debug_deadline"]
        delta = (datetime.fromisoformat(d2.replace("Z", "+00:00"))
                 - datetime.fromisoformat(d1.replace("Z", "+00:00"))).total_seconds()
        check("新截止时间 ≈ 原截止 + 20s", 18.5 <= delta <= 21.5, f"delta={delta:.1f}s d1={d1} d2={d2}")
        check("日志记录续开", wait_log(W1, "续开"))

        log("  …等待跨过最初的 6s 截止时刻（共等 8.5s）")
        time.sleep(8.5)
        st = state(W1)
        check("超过原截止时间后仍为 DEBUG（续开生效）",
              st["debug_active"] and st["actual_level"] == "DEBUG",
              f"remaining={st['seconds_remaining']}s")
        check("新窗口剩余时间合理(>12s)", st["seconds_remaining"] > 12,
              f"remaining={st['seconds_remaining']}")
        cancel_debug("worker1")
        st = wait_state("cancel -> INFO",
                        lambda s: s["actual_level"] == "INFO" and not s["debug_active"], timeout=6)
        check("取消后恢复 INFO", st["actual_level"] == "INFO")

        # ---------------------------------------- 5. 两边写样例日志
        log("== 5. 一键两边写样例日志 + 日志可查 ==")
        code, data = request("POST", f"{ADMIN}/api/sample-logs/write-all")
        check("write-all HTTP 200 且两边成功",
              code == 200 and all(v["error"] is None for v in data["workers"].values()),
              json.dumps({k: v["error"] for k, v in data["workers"].items()}, ensure_ascii=False))
        check("worker1 日志可查到 web-console 样例", wait_log(W1, "via=web-console"))
        check("worker2 日志可查到 web-console 样例", wait_log(W2, "via=web-console"))

        # ---------------------------------------- 6. 暂停相互独立
        log("== 6. 暂停 worker2 不影响 worker1 ==")
        pause("worker2")
        time.sleep(1.2)
        c2, _ = request("GET", f"{ADMIN}/config/worker2")
        c1, cfg1 = request("GET", f"{ADMIN}/config/worker1")
        check("worker2 /config=503", c2 == 503)
        check("worker1 /config=200 且仍有配置", c1 == 200 and cfg1.get("worker_id") == "worker1")
        resume("worker2")
        time.sleep(1.0)

    except Exception as exc:  # noqa: BLE001
        check("场景执行", False, f"{type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        log("== 清理：恢复两边为接收 + INFO ==")
        reset_worker("worker1", W1)
        reset_worker("worker2", W2)

    print(f"\n结果：{PASS} passed, {FAIL} failed", flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
