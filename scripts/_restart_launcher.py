#!/usr/bin/env python3
"""自测用进程重启引导器（生产 / docker compose 不使用本文件）。

在无权限发送信号的受限环境里，用它包裹一个 uvicorn worker：
  - 正常启动 worker_service.main:app；
  - 监视一个标记文件，出现后让 uvicorn 优雅退出（server.should_exit=True，无需信号）；
  - 旧进程退出后 os.execve 一个全新解释器，监听同一端口，得到全新 boot_id 与空环形缓冲，
    与 `docker compose restart worker` 的“新进程接管同一端口”等价。

新进程额外环境变量（如 WORKER_LOG_START_ID）由 REEXEC_NEW_ENV 以 JSON 传入。
仅被 scripts/restart_log_check.py 调用。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import uvicorn


def main() -> None:
    cfg = json.loads(os.environ["REEXEC_CFG"])
    server = uvicorn.Server(
        uvicorn.Config(
            cfg["module"],
            host=cfg.get("host", "127.0.0.1"),
            port=int(cfg["port"]),
            log_level=cfg.get("log_level", "warning"),
            access_log=False,
        )
    )

    def watch_flag() -> None:
        flag = cfg["flag"]
        while not os.path.exists(flag):
            time.sleep(0.1)
        # 让 uvicorn 主事件循环自行优雅收尾（lifespan 会停掉轮询/看门狗/心跳）
        server.should_exit = True

    threading.Thread(target=watch_flag, daemon=True).start()
    server.run()

    # 旧 server 已退出：带新环境 execve 成全新解释器，同端口接管
    new_env = dict(os.environ)
    new_env.pop("REEXEC_CFG", None)
    new_env.update(cfg.get("new_env", {}))
    argv = [
        sys.executable, "-m", "uvicorn", cfg["module"],
        "--host", cfg.get("host", "127.0.0.1"),
        "--port", str(int(cfg["port"])),
        "--no-access-log",
    ]
    if cfg.get("log_level"):
        argv += ["--log-level", cfg["log_level"]]
    os.execve(sys.executable, argv, new_env)


if __name__ == "__main__":
    main()
