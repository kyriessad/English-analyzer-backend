# Level 7 E2E Testing System 报告

最终证据轮：`20260825T140807Z-21c2d3`。最终结果 **PASS（有 1 个非 P0 真实业务缺陷）**。

原始证据：

- `.e2e-artifacts/20260825T140807Z-21c2d3/result.json`
- `.e2e-artifacts/20260825T140807Z-21c2d3/requests.json`
- `.e2e-artifacts/20260825T140807Z-21c2d3/uvicorn.log`
- `.e2e-artifacts/20260825T140807Z-21c2d3/ollama-proxy.log`

## A. 实际建立了什么

- 新增 `e2e/run_level7_e2e.py`：有序门禁、真实 HTTP 用户、Prometheus 采样、进程/GPU 采样、PostgreSQL 末态校验和 `finally` cleanup。
- 新增 `e2e/lab_app.py`：只在 `APP_ENV=e2e` 且持有随机控制令牌时开放 HTTP hold、DB pool hold、Piper cache fault 控制。正常启动脚本不会导入它。
- 新增 `e2e/ollama_proxy.py`：正常时透明转发到真实 Ollama；故障时只切断 E2E 路径，不停止共享 Ollama。
- 入口：`scripts/run-level7-e2e.ps1`。
- FastAPI：`127.0.0.1:18000`，1 worker，`--limit-concurrency 30`。最终轮 launcher PID 7784，实际 listener PID 是其子进程。
- Ollama E2E transport：`127.0.0.1:18114` -> 真实 `127.0.0.1:11434`。
- PostgreSQL：每轮重建 `english_analyzer_phase1_e2e`，真实 Alembic 升到 `f6a7b8c9d0e1`。
- JWT secret、Piper model hard links、TTS cache、日志、metrics 和测试用户均按 run 隔离。
- 未改正式 `.env`、正式 `english_analyzer`、正式 TTS cache，也未启动/停止 8000。

微信开发者工具已安装，但 CLI Service Port 关闭。本轮没有隐式开启该机器安全设置，因此没有客户端自动化。

## B. 哪些依赖是真的

| 依赖 | 结论 | 证据边界 |
|---|---|---|
| PostgreSQL | REAL | 新 DB、真实 migration、真实 SQLAlchemy transaction、最终 SQL 查询 |
| HTTP | REAL | 独立 Uvicorn listener + httpx 网络请求 |
| Auth | PARTIAL | 测试用户/JWT 是隔离环境外置引导；Bearer 校验、DB 用户读取、logout/token_version 撤销为真实链路 |
| Qwen | REAL | Ollama `/api/generate` 真实调用，`analysisModel=qwen3:8b`、`analysisSource=ollama` |
| Piper | REAL | 真实 ONNX 模型产生 RIFF/WAV |
| 微信客户端 | NOT COVERED | DevTools CLI Service Port 关闭；未 Mock、未冒充完整微信 E2E |

## C. 单用户 E2E

结果 **PASS**。

真实链路：隔离身份 -> `/api/auth/me` -> Create/Get/Patch Card -> Sync update + replay -> streaming AI -> Piper TTS -> Review session -> feedback -> final Card/session/DB -> logout -> 旧 token 401。

- 14 请求：13 个 200；1 个 401 是 logout 后旧 token 的预期拒绝。
- p50/p95/p99：31.355 / 3399.650 / 8174.861 ms；RPS 1.375。
- Streaming：Request ID `5c019fa53e9c4bec947bc0ac43814191`；53 个 NDJSON event；TTFE 26.101 ms；event 到达跨度 9341.532 ms；包含 `start/delta/field/final/done`。
- Qwen：`analysisModel=qwen3:8b`，`analysisSource=ollama`，`exampleSource=ollama`。
- Piper：`audio/wav`，64044 bytes，RIFF，SHA-256 `ec9789ddeffaffbbd8a56cdb966bb661b5146abfc6552d0fef850190427305f2`。
- Card 末态：version 4，note=`single-user-synced`，review_count 1，last_result=`got_it`。
- Review 末态：completed，reviewed=1/total=1。
- client actions：Sync/Review 均 succeeded；quota：AI=1、TTS=1。
- 资源末态：DB checked-out/overflow、AI active/waiting/followers、TTS active/waiting 全为 0。

## D. 多用户隔离

结果 **PASS**。5 个用户各自完成 Card CRUD、Sync/replay、Review、真实 AI、真实 TTS。

攻击结果均为 404，且都有 Request ID：

| 攻击 | Request ID |
|---|---|
| A read B card | `47db05e2f2794379bc634c6bf659fcd1` |
| A patch B card | `74946bbd76b64e4eaba07929a210813a` |
| A delete B card | `b0f386a9ced4430baade439bb19b8e0f` |
| A sync B card / replay B action id | `54ee6ba56a8146c0adafaf56f8dae2a8` |
| A submit B review resource ids | `d784ddf9cc874feabb749bc4b625e310` |

受害者 PostgreSQL 快照逐字段不变；跨用户 Review item/log 查询为 0；无 500。

## E. 负载实验

业务模型：所有用户 Auth + Create/Read/Update；每 2 个用户一个 Sync；每 3 个用户一个 Review；AI/TTS 低频；think time 0.02–0.18 秒。每个用户都有独立 UUID/JWT/Card。

| 用户 | 请求 | 成功/失败 | HTTP | p50/p95/p99 ms | RPS | CPU/RSS peak | GPU/VRAM peak |
|---:|---:|---:|---|---|---:|---|---|
| 5 | 27 | 27/0 | 200=27 | 55.171/191.396/9931.915 | 1.948 | 63.5% one-core / 706.8 MiB | 97% / 6281 MiB |
| 10 | 54 | 54/0 | 200=54 | 105.173/233.508/2135.250 | 10.885 | 116.8% one-core / 728.8 MiB | 99% / 6279 MiB |
| 30 | 159 | 156/3 | 200=156, 503=3 | 304.442/463.353/3287.057 | 12.694 | 100.3% one-core / 723.6 MiB | 99% / 6281 MiB |
| 100 | 464 | 388/76 | 200=388, 503=76 | 863.352/3919.413/3984.992 | 16.878 | 60.1% one-core / 719.2 MiB | 97% / 6285 MiB |

说明：CPU 是实际 FastAPI listener 的 CPU time/elapsed（单核百分比，可超过 100%）；GPU/VRAM 是整张 GPU，不是进程归因。

30-user 的 3 个 503 均无 Request ID、正文 `Service Unavailable`，在 Uvicorn admission 层快速拒绝。100-user 的 76 个 503 分为：

- 73 个 Uvicorn admission 503：无 Request ID，未进入业务中间件。
- 2 个 `DB_POOL_TIMEOUT`：有 Request ID，约 3 秒受控失败。
- 1 个 AI Queue Full：有 Request ID，`ai_queue_full_reject_total +1`。

资源峰值：30/100-user 都达到 DB checked-out=15、overflow=10；AI 始终 active<=1、waiting<=2；TTS active<=1、waiting<=1（混合负载观测值）。每级恢复探针均 200，末态资源 gauge 均归零，无 500。

## F. Burst

### HTTP

100 个同时进入的 E2E HTTP hold：29 个 200、71 个 Uvicorn 503；恢复 `/health` 200。hold 路由不触碰 DB，所以 71 个 admission reject 没有继续占 DB/AI/TTS。

### AI

- 8 个语义独立请求：3 个 200、5 个 Queue Full 503；max active=1、max waiting=2、`ai_queue_full_reject_total +5`。
- 1 owner + 5 个真正同用户/同 payload/同 idempotency key follower：4 个 200、2 个 follower-full 503；max followers=3、`ai_inflight_follower_reject_total +2`。
- 最终 active/waiting/followers 均为 0。

### TTS

8 个独立 cache-miss TTS：3 个有效 WAV 200、5 个 Queue Full 503；max active=1、max waiting=2、`tts_queue_full_reject_total +5`；最终均归零。

## G. 故障实验

### Ollama

只关闭 E2E -> Ollama transport；共享 Ollama 没有被停止。

- AI HTTP=200，但业务结果明确 `ok=false, level=failed, errors=[分析服务暂时不可用，请稍后重试]`。
- Card/Review/TTS 同时请求均 200。
- transport 恢复后，新 AI Request ID `ad344c945ed0499bb3c55710450cdb4b` 返回真实 streaming Qwen 200（99 events）。
- 资源 gauge 归零。

### Piper

只移动本轮 isolated model hard links，并清空 E2E 进程 voice cache；正式 model 文件未变。

- TTS 返回 503。
- Auth/Review/AI 均 200。
- hard links 恢复后，新 TTS 返回 200 有效 WAV。
- 资源 gauge 归零。

### DB pool

- 15 个受控 E2E DB hold 占满 pool 5+10。
- 第 16 个 DB-backed 请求 3.034 秒后返回 503 `DB_POOL_TIMEOUT`；counter +1。
- holders 释放后新 `/api/auth/me` 200。
- checked-out/overflow 最终归零，无 idle-in-transaction。

## H. 所有异常

### H1. 真实业务缺陷：async AI Queue Full/Timeout 中文乱码

- 现象：真实 streaming AI Queue Full 503 的 `detail` 是乱码；capacity、status、metrics 和资源释放本身正确。
- Request IDs：`f9a7aa6fafac47bf861a3a39ca5c6658`、`96e82bcfbdee4e4fbeafcd840c650240`、`2e8475e4ffcf4fa5b3c4044ecb08476e`、`891fa6b98c604b73afe80e511861fd3e`、`4fa1a51376984415a2f65c277f5f5b9b`。
- 首个请求 trace：`fbcc527db4feaf6be3203084f2bcd69c`。
- 根因：`app/services/security.py` 的 sync 路径文本正确，但 async 路径 Queue Full（line 294）和 Queue Timeout（line 333）字符串在源文件中已是 mojibake。
- 分类：A，真实业务 bug；非 P0（无越权、500、半事务或资源泄漏），但错误合同/用户提示损坏。
- 本轮按要求只报告，没有修改业务代码。

### H2. E2E 工具问题与回归

| Run | 问题 | 分类 | 处理/回归 |
|---|---|---|---|
| `20260825T134124Z-4f4c5b` | SQLAlchemy URL 直接传给 psycopg | B | 单独转换 psycopg DSN；cleanup 成功；单用户重跑 PASS |
| `20260825T134258Z-867163` | Windows launcher/listener 双 PID 被误判 | B | 验证实际 listener 命令行与父子 PID；cleanup 成功；单用户重跑 PASS |
| `20260825T134708Z-ba5f35` | CPU/RAM 采了 launcher | B | 改采 listener；5-user 重跑得到有效数据 |
| `20260825T135724Z-d13f86` | Ollama 故障只按 HTTP 判定，忽略业务失败载荷 | B | 支持 HTTP failure 或 `ok=false/level=failed`；完整回归 PASS |

没有隐藏 flaky。Uvicorn/FastAPI 和 proxy 在 Windows 受控 terminate 后 exit code 是 1，但 `stopped=true`、端口均释放；这不是遗留进程。所有失败轮也完成了 E2E DB drop。

## I. 数据库最终验证

最终 shutdown 前：

- users=171，cards=136，review_sessions=44，review_session_items=44，review_logs=43，client_actions=106，resource_usage rows=43。
- Card：active=136。
- Review：completed sessions=43，active=1；done items=43，pending=1。唯一 active/pending 是负载中 session 创建成功、后续 feedback 被 admission 拒绝后的可恢复状态，不是半事务。
- Client action：succeeded=105，failed=1，processing=0。failed 是越权 Sync 攻击的明确失败记录。
- 重复 client action=0，重复 review log=0，重复 local card id=0。
- review item/log 跨用户=0；completed progress mismatch=0；active overcomplete=0；reviewed item 缺 log=0；log 对应未 review item=0。
- quota 汇总：AI count=20/19 users，TTS count=25/24 users；单用户精确为 AI=1、TTS=1。总体过载下逐请求 quota 归因仍未单独形式化证明。
- shutdown 前只有 5 个 app-pool `idle` connection；没有 `idle in transaction`。shutdown 后 listener/proxy 端口为空，E2E DB 已 drop，cache/model links 已删除。
- 未发现半事务、重复数据、跨用户污染、负数 quota、processing action 泄漏或永久资源泄漏。

## J. 测试体系可信度结论

现在可以对以下能力有较高信心：

- 当前代码在真实 Uvicorn/HTTP/PostgreSQL/Qwen/Piper 下能完成核心业务链。
- Card/Sync/Review 的单用户末态正确，5 用户资源隔离可靠。
- 5/10 用户正常流量稳定；30/100 用户过载时主要受控 503，而不是 500 或系统拖死。
- Uvicorn、AI running/waiting/follower、TTS running/waiting、DB pool timeout 均真实达到设计上限并恢复。
- Ollama、Piper、DB pool 故障被限制在对应能力，恢复后新请求成功。

仍未被证明：

- 真实微信客户端的 `wx.login -> code2session`、设备网络、域名校验、代理、`onChunkReceived` 和 UI 状态；Auth 因此只能标为 PARTIAL。
- 真实微信客户端的自动化需要：显式开启 DevTools Service Port、安装/固定 `miniprogram-automator`、提供指向 18000 的 E2E config，并在已登录开发者工具中验证一次性 `wx.login` code。
- 多 Uvicorn worker 行为（本轮固定 1 worker；进程内 semaphore/idempotency/rate limit 会随 worker 数倍增）。
- 逐个过载请求的 quota 归因、长时间 soak、进程崩溃/机器重启、真实 PostgreSQL server 故障。
- async AI Queue Full/Timeout 中文乱码尚未修复。
