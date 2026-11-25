# 值班调试台：双 worker 临时 DEBUG / 日志级别管理

值班排查时，让两个**相互独立的 Python worker 进程**（`worker1`、`worker2`，各跑同一个样例模块
`worker_service/sample_module.py`）短时多记日志。一个 FastAPI 调试页可以：

- 修改样例模块的**常规日志级别**（DEBUG/INFO/WARNING/ERROR）；
- 开启限时 **DEBUG**，并可多次**续开**（以新的绝对截止时间为准）；
- DEBUG 期间收到的新常规级别先**挂起**，到期才应用；
- **暂停 / 恢复**任一 worker 接收配置（管理端对该 worker 的拉取返回 503，模拟配置通道中断）；
- 一键（或分别）**触发两边写样例日志**，网页查看每个进程的**实际级别、截止时间、挂起级别、连通状态和日志流**。

## 1. 快速开始（Docker Compose）

```bash
docker compose up --build -d
```

| 入口 | 地址 | 说明 |
| --- | --- | --- |
| 调试页面 | http://localhost:8000/ | 管理 FastAPI + 静态调试页 |
| worker1 直连 | http://localhost:8001/local/state | 实际级别 / 截止时间 / 错误 |
| worker1 日志 | http://localhost:8001/local/logs?limit=200 | 样例模块环形缓冲日志 |
| worker2 直连 | http://localhost:8002/local/state | 同上 |
| worker2 日志 | http://localhost:8002/local/logs?limit=200 | 同上 |

停止：

```bash
docker compose down          # 保留数据卷（截止时间不丢）
docker compose down -v       # 连同持久化卷一起删除
```

三个服务共用同一个镜像（根目录 `Dockerfile`），由 compose 的 `command` / 环境变量区分角色。
管理端配置持久化在命名卷 `admin-data`（容器内 `/data/admin/admin-config.json`），
每个 worker 的“最后收到的级别 + 截止时间”持久化在 `worker1-data` / `worker2-data`。

### 一键场景自检（仅用 Python 标准库）

Compose 启动并暴露上述宿主端口后，在仓库根目录执行：

```bash
python3 scripts/e2e_check.py
# 若端口不同：
ADMIN_URL=http://127.0.0.1:8000 \
WORKER1_URL=http://127.0.0.1:8001 \
WORKER2_URL=http://127.0.0.1:8002 \
python3 scripts/e2e_check.py
```

覆盖：常规级别过滤、DEBUG 期间级别挂起与到期应用、暂停下发后断连到期、重连跳过已过期窗口、
续开跨过原截止时刻、一键两边写日志、暂停相互独立。

进程重启日志续读 / 响应乱序的专项自检（自带临时 admin + 双 worker，跑在独立临时端口上，
不影响常驻服务，仅用标准库）：

```bash
python3 scripts/restart_log_check.py
```

覆盖：worker 重启后凭 `boot_id` 自动切到新进程并补读缓冲（含“新编号已超过旧游标”）、
迟到旧进程响应不改列表/游标、同进程并发乱序响应去重且游标不倒退、另一 worker 独立刷新。
受限环境若禁止 `kill` 系统调用，脚本改用 `scripts/_restart_launcher.py`（标记文件触发
uvicorn 优雅退出后 `execve` 同端口全新进程）模拟 `docker compose restart`，语义等价。

## 2. 推荐手工验收路径

1. 打开 http://localhost:8000/ ，能看到两张 worker 卡片，实际级别均为 `INFO`，日志页每 5 秒有心跳。
2. 某张卡片选 `WARNING` → “下发并更新常规级别”，再点“写样例日志”：
   返回 `emitted=[WARNING, ERROR]`、`suppressed=[DEBUG, INFO]`，日志区也只有 WARNING/ERROR。
3. “DEBUG 时长”填 `10` → “开启 / 重设 DEBUG”：徽章变紫 `DEBUG`，出现倒计时，日志开始滚动 DEBUG 行。
4. DEBUG 中把常规级别改成 `ERROR` 并下发：页面显示“待生效 ERROR”，实际级别仍是 DEBUG。
   10 秒后倒计时结束，实际级别变 ERROR，并能看到“先挂起 / 到期后应用”的 `[level-control]` 日志。
5. **断连到期**：开一个 6 秒 DEBUG → 点“暂停接收配置”，按钮变“恢复接收配置”，通道徽章显示 503。
   **不要点恢复**，等到第 6 秒：worker 靠本地看门狗自行恢复 INFO，日志出现
   “到达最后收到的截止时间……未依赖管理端指令自行结束 DEBUG”。再点恢复，状态保持 INFO，
   并出现“重连后收到的 DEBUG 截止时间……已过期，跳过”。
6. **续开**：开一个 6 秒 DEBUG，立刻把时长改成 `20` 点“续开此时长”，管理端截止时间变成
   “原截止 +20s”；等过最初的 6 秒，页面仍为 DEBUG、剩余约 16~20 秒。
7. **管理服务重启**：开一个 10 分钟 DEBUG，然后 `docker compose restart admin`
   （或 `docker compose down && docker compose up -d`，**不要加 `-v`**），
   刷新页面：管理端记录的截止时间与重启前完全一致，恢复下发后 worker 仍按该绝对时刻到期。
8. 顶部“📝 两边同时写样例日志”：两张卡片都出现新的 `[sample #… via=web-console]` 日志。

## 3. 设计与关键语义

### 3.1 配置模型（管理端持久化的期望状态）

```json
{
  "regular_level": "INFO",
  "regular_rev": 3,
  "debug_deadline": "2026-11-25T03:35:00.716277Z",
  "debug_started_at": "...",
  "debug_renewals": 1,
  "paused": false
}
```

- 全链路使用带时区的 **UTC ISO8601 绝对截止时刻**，不是“剩余秒数”。因此管理端重启不会重新计时。
- `regular_rev` 在常规级别每次实际变化时 +1，worker 据此区分“新级别”和重复轮询到的旧配置。
- “续开”在管理端把新截止时间计算为 `max(当前未过截止时间, now) + seconds`；
  worker 收到后直接用新值覆盖旧值（以新截止时间为准）。

### 3.2 worker 状态机（`worker_service/state.py`）

worker 每 1 秒（compose 中 `POLL_INTERVAL=1.0`）GET `http://admin:8000/config/{worker_id}`：

- **200 + 配置**：应用配置（见下）。
- **503**：管理端人工暂停下发，响应不含任何配置，按“通道中断”处理。
- **连接异常 / 管理服务宕机重启**：记录错误，但不改变当前级别与截止时间。

收到配置时：

1. 若配置中的截止时间仍在未来 → sample logger 立即设为 DEBUG；
   此时若带了更新的 `regular_rev`，只写入 `pending_regular_*`，**不改动当前 logger 级别**。
2. 若截止时间为空或已过 → DEBUG 结束：若有挂起级别则应用挂起级别，否则恢复最后应用的常规级别。
3. 一个 **0.2 秒本地看门狗**独立检查最后收到的绝对截止时间；即使永远拉不到配置，
   到点也会自行结束 DEBUG、恢复级别并写一条 `[level-control]` 日志。
4. 重连后收到的同一过期截止时间只提示一次“跳过该调试窗口”，不会重新进入 DEBUG。
5. 最后收到的级别 / rev / pending / 截止时间原子写入本地 JSON；worker 进程重启后：
   截止时间仍在未来就继续 DEBUG，已过期则直接按常规级别运行并清理旧值。

### 3.3 可观测性

- sample logger 同时挂：stdout handler（`docker compose logs worker1` 可见）和
  容量 1000 的进程内环形缓冲 handler（`/local/logs` 与网页日志面板使用，支持 `after_id` 增量轮询）。
- 后台心跳每 5 秒写一条 DEBUG 明细、每 15 秒写一条 INFO 进展，开启 DEBUG 后日志面板会自动滚动。
- 写样例接口返回每一级别是 `emitted` 还是 `suppressed`，可直接核对级别是否真的生效。

### 3.4 进程重启后的日志续读（值班重启 worker 排障场景）

环形缓冲是**进程内**状态：worker 进程一重启，缓冲清空、日志 `id` 从 1 重新计数，而调试页
手里还拿着旧进程的续读游标（可能已经很大）。若只用 `after_id` 续读，新进程的日志会被
`id > after_id` 全部过滤掉，面板从此漏掉新进程日志。修复方式：

- 每个 worker 进程启动时生成唯一 **`boot_id`**（`/local/logs`、`/local/state`、`/healthz` 都带回，
  另带 `booted_at` 启动时刻），并写一条 `[process-boot]` 日志进环形缓冲。
- 调试页每个面板记录“当前跟随的 `boot_id`”。收到**陌生 `boot_id`** 即判定进程重启：
  作废旧游标（`lastId=0`）、清空旧列表，按既有 `limit=200` 首读**补读新缓冲里仍可取到的日志**，
  再以新进程自己的 `last_id` 继续增量轮询；即使新缓冲编号已经超过旧游标也能正确接上。
- 已被换掉的**旧进程迟到响应**（`boot_id` 对不上、且是历史见过的 boot）一律判 `stale` 丢弃，
  不改列表、不动游标、不改当前跟随进程。
- 同一进程内每个日志请求带单调递增序号 `reqSeq`：自动轮询与“写样例后刷新”并发、乱序返回时，
  只应用序号更新的响应；合并时再按日志 `id` 去重，且游标 `lastId` **只增不减**——
  每条日志只显示一次，续读位置绝不倒退。请求不排队、不加全局锁，写样例后的刷新**不必等慢轮询**。
- 两个 worker 的面板状态各自独立；一个重启/刷新，另一个照常轮询。
- 沿用日志接口既有查询方式（首读 `limit=200`、增量 `after_id + limit=1000`）、面板排序
  （最新在上）与显示条数（保留最近 400 条、渲染前 200 行），均未改变。

## 4. HTTP API 摘要

管理端（http://localhost:8000）：

```text
GET  /                                  调试页面
GET  /healthz
GET  /api/overview                      两个 worker 的期望配置 + 实时状态
POST /api/workers/{id}/level            {"level":"WARNING"}
POST /api/workers/{id}/debug/start      {"seconds":30}
POST /api/workers/{id}/debug/renew      {"seconds":120}
POST /api/workers/{id}/debug/cancel
POST /api/workers/{id}/pause
POST /api/workers/{id}/resume
POST /api/workers/{id}/sample-logs
POST /api/sample-logs/write-all
GET  /api/workers/{id}/logs?limit=200[&after_id=N]
GET  /config/{id}                       worker 轮询用；paused 时 503
```

worker 本地（compose 网络内 `:8000`，宿主机 `8001/8002`）：

```text
GET  /healthz                                  含 worker_id、boot_id
GET  /local/state                              状态快照 + boot_id、booted_at
GET  /local/logs?limit=200&after_id=0&level=DEBUG,INFO   响应含 boot_id、booted_at
POST /local/write-sample
```

> `boot_id` 是每次进程启动新生成的身份标识；调试页用它识别 worker 重启并续读新缓冲。

## 5. 已实现功能（对照需求）

- [x] FastAPI 调试页 + 两个独立 Python worker 进程，各跑样例模块。
- [x] 网页修改模块常规日志级别，worker 秒级生效并真实过滤日志（返回 emitted/suppressed）。
- [x] 网页开启临时 DEBUG（自定义秒数 + 10s/30s/60s/5m/30m 快捷项）。
- [x] 网页“续开”，下发新的绝对截止时间，worker 以新时间为准。
- [x] DEBUG 期间新常规级别挂起，到期（本地看门狗或下一次配置）才应用，UI 显示 pending。
- [x] 断连自治：收不到配置也按最后收到的截止时间结束 DEBUG、恢复最后收到的常规级别。
- [x] 重连跳过已过期的 DEBUG 窗口，不反复进入。
- [x] 管理端配置 / 原截止时间 / 暂停状态 JSON 持久化到命名卷，新进程重启后原样沿用。
- [x] worker 本地也持久化最后收到的级别与截止时间，覆盖“worker 重启 / 管理端彻底不可达”。
- [x] 网页按 worker 暂停 / 恢复配置接收（管理端 `/config` 返回 503），两个 worker 相互独立。
- [x] 网页分别或一键两边写样例日志；查看实际级别、倒计时、本地/管理端两份截止时间、
      最近成功轮询、连续失败、错误信息与彩色日志流（自动刷新、级别过滤、清空显示）。
- [x] Dockerfile + docker-compose.yml（admin/worker1/worker2 + 3 个命名卷 + 健康检查）。
- [x] 零第三方依赖的端到端自检脚本 `scripts/e2e_check.py`。
- [x] **worker 进程重启后的日志续读**：每进程 `boot_id` 身份，调试页检测到陌生 boot 自动切到
      新进程、作废旧游标并补读新缓冲（即使新编号已超过旧游标）；旧进程迟到响应判 stale 丢弃，
      不改列表/游标；同进程并发乱序响应按 id 去重、游标只增不减；写样例刷新不等慢轮询；
      另一 worker 独立刷新。专项自检 `scripts/restart_log_check.py`（22 项）。

## 6. 实测结果（2026-11-25，UTC）

**关于执行环境的如实说明**：本次编码环境中没有 docker CLI / daemon（`docker: command not found`，
无 `/var/run/docker.sock`），因此**未能在本机字面执行 `docker compose up --build`**。
作为替代，使用 Python 3.11 venv 按 `docker-compose.yml` 中完全相同的 uvicorn 入口
（`admin_service.main:app` / `worker_service.main:app`）和环境变量，在 `127.0.0.1`
的 8500/8501/8502 端口实跑了三个进程，并把管理端数据目录、worker 数据目录指到临时目录，
对真实 HTTP 接口和真实网页 JS 做了验证。`requirements.txt` 中的版本已在该 venv 实际安装运行；
compose YAML 已通过解析与不变量检查（服务、端口、卷、Dockerfile `COPY common` 均一致）。

### 6.1 端到端场景自检：`33 passed, 0 failed`

最终一轮完整输出存档于试跑环境，关键结果：

- 常规级别 WARNING：写样例返回 `emitted=['WARNING','ERROR']`、`suppressed=['DEBUG','INFO']`。
- DEBUG 中下发 ERROR：实际级别保持 DEBUG，`pending=ERROR rev=4`；8 秒窗口到期后 6.0s 内
  实际级别变 ERROR、pending 清空，且有“先挂起 / 到期后应用”日志。
- **断连到期（暂停下发，不恢复）**：暂停后 `GET /config/worker1` 返回 `503 {"paused":true}`；
  worker 在截止时刻后 **+0.09s**（看门狗周期 0.2s）自行恢复 INFO，`local_expiry_count≥1`，
  暂停期间再观察 2 秒不会重新进入 DEBUG；恢复后保持 INFO，并记录“已过期，跳过”。
- **续开**：6s 窗口续开 20s，worker 截止时间变化量实测 **delta=20.0s**；
  等待 8.5s（已超过最初 6s 截止）后仍为 DEBUG，`remaining=16.49s`。
- 一键两边写样例：两个 worker 均无错误，日志都能查到 `via=web-console`。
- 暂停 worker2 时：`/config/worker2=503`，而 `/config/worker1=200`，互不影响。

### 6.2 网页前端 jsdom 冒烟：`15 passed, 0 failed`

环境中没有无头浏览器，因此安装 jsdom 加载真实 `index.html` + `app.js`，
把 fetch 指向真实运行的管理服务，验证：两张卡片渲染、实际级别徽章、通道状态、
日志面板渲染 40+ 行、点“写样例日志”出现序号与 emitted/suppressed、样例日志进入面板、
网页下发 WARNING 后徽章跟随、暂停/恢复按钮文案与 503 徽章、DEBUG 倒计时显示“1 分 59 秒”、
全程无 JS 运行时错误 / 未处理 Promise 异常。

### 6.3 管理服务重启沿用原截止时间：PASS

写入 `debug_deadline=2026-11-25T03:47:42.545187Z` 后，用**同一数据目录启动一个全新 admin
uvicorn 进程**（等价于容器重启后新进程读命名卷），`/api/overview` 读出的截止时间逐字符相同；
另一轮还验证了常规级别与 `paused=true` 同样被恢复。

### 6.4 管理端彻底不可达的自治：PASS（比暂停 503 更强）

给一个全新 worker 预置“最后收到的”状态文件（WARNING rev42 + 5 秒后的截止时间），
并把 `ADMIN_URL` 指向无监听的端口。启动后：

- 立即按持久化状态进入 DEBUG；
- 5 秒后自行恢复 WARNING，`local_expiry_count=1`；
- `last_poll_success_at=None`（从未成功收到过配置），`last_error=ConnectError`；
- 状态文件被改写为 `debug_deadline: null`。

另外验证：预置 2020 年过期截止时间启动时，worker 直接以 ERROR 常规级别运行并清理旧截止时间。

### 6.5 其他实测交互

- “暂停期间把级别改成 ERROR，2.2 秒内 worker 仍为 INFO；恢复后秒级变 ERROR”：PASS。
- `GET /`、`/static/styles.css`、`/static/app.js`、`/healthz` 均为 200 且 Content-Type 正确。

### 6.6 进程重启续读与响应乱序（本次新增，全部 PASS）

测试在 Python 3.11 venv 中对**真实 uvicorn 进程**进行（非 mock 业务逻辑），分三层：

1. **专项自检 `scripts/restart_log_check.py`：22 passed, 0 failed**（仅标准库）。
   脚本在临时目录、独立端口起真实 admin + 双 worker；用 `scripts/_restart_launcher.py` 触发
   worker1 旧 uvicorn 优雅退出并 `execve` 同端口全新进程（等价 `docker compose restart`，
   新 `boot_id`、空环形缓冲），并令新缓冲通过 `WORKER_LOG_START_ID=500` 从更大编号开始，
   实测：旧游标 `lastId=10` 时重启，`after_id=600` 对新缓冲返回空但带新 `boot_id`（修复前的
   漏日志卡死点）；页面状态机凭陌生 `boot_id` 判 `switched`，补读到新缓冲 `id=500…` 的
   `[process-boot]` 行，游标更新为新进程 `last_id`；伪造的旧 boot 迟到响应判 `stale`，
   游标与列表不变；同进程两个并发请求乱序应用后无重复 id、游标不倒退；worker2 boot 未变、
   独立刷新。
2. **前端 jsdom 竞态套件（内存假后端，加载真实 `index.html` + `app.js`）：33 passed, 0 failed**。
   覆盖：重启后自动切换 + 补读（含新编号 90/200/500 超过旧游标的多组取值）、旧进程在途请求
   重启后才返回时不改列表/游标、慢轮询挂起时写样例刷新立即上屏（不等慢轮询）、乱序返回去重且
   游标不倒退、二次重启与历史 boot 迟到判 stale、两 worker 面板互不影响。
3. **真实后端 jsdom 冒烟（fetch 指向真实运行的 8500/8501/8502）：11 passed, 0 failed**。
   真实页面渲染两张卡片、面板显示真实 boot 短号与 100+ 行日志、点“写样例日志”新行真实进入面板、
   连续两个自动轮询周期无重复行、全程无未处理 JS 错误 / Promise 异常。

回归确认：改动后原 `scripts/e2e_check.py` 仍为 **33 passed, 0 failed**；全部 Python 文件
`py_compile` 通过；`app.js` 通过 `node --check`；compose/Dockerfile 静态不变量校验通过。

## 7. 未完成项 / 已知限制

1. **未在本环境真正执行 `docker compose up --build`**（无 Docker）。Docker 镜像构建、
   compose 网络内 DNS（`http://admin:8000`、`http://worker1:8000`）未在本机实跑；
   已用相同入口/环境的本地多进程 + compose 静态校验最大程度替代，首次在有 Docker 的机器上
   建议先跑一次 `docker compose up --build`、`scripts/e2e_check.py` 与
   `scripts/restart_log_check.py`，并在调试页按下面的“手工验收重启续读”走一遍。
2. **未做真实浏览器视觉/点击验证与截图**；前端经 jsdom 对接真实后端验证，未做像素级 UI 走查。
3. 调试页/API **没有鉴权、登录与 HTTPS**，定位是内网值班临时工具；端口 8000/8001/8002
   直接暴露在宿主网卡上，如需跨网段使用请放到反向代理/VPN 后并加鉴权。
4. “暂停接收配置”是**管理端应用层不下发配置（503）**来模拟断连，并未调用
   `docker network disconnect`；真实网络中断 / 管理进程宕机场景由 6.4 的 dead-ADMIN_URL 测试覆盖。
5. 网页日志来自各进程内最近 1000 条环形缓冲；长期归档依赖 stdout 被 Docker
   日志驱动收集，未接 ELK/Loki 等中心日志。
6. 目前只内置两个固定 worker（`WORKER_IDS` / compose 服务）；新增进程需要同时加服务定义与
   `WORKER{n}_URL` 环境变量，尚未做动态注册。
7. 本环境 seccomp 禁止 `kill` 系统调用，无法字面演示 `docker compose restart worker1`；
   重启改用 `_restart_launcher.py` 的“uvicorn 优雅退出 + `execve` 同端口新进程”等价模拟，
   真实 Docker 环境里直接 `docker compose restart worker1` 即可触发同样的 boot 切换路径。
   `WORKER_LOG_START_ID` 仅为测试钩子（默认不设，缓冲仍从 id=1 开始），compose 不使用它。

### 调试页手工验收重启续读（有 Docker 的环境）

```bash
docker compose up --build -d
# 打开 http://localhost:8000/ ，等 worker1 面板出现若干日志、meta 显示 boot 短号
docker compose restart worker1
# 页面 1~2 秒内：顶部提示“检测到进程重启…已切到新进程并补读缓冲日志”，
# 面板 meta 出现“进程已切换 旧boot→新boot（缓冲重新计数）”，新 [process-boot] 行在列，
# 之后心跳/写样例日志继续滚动；worker2 面板不受影响。
```
