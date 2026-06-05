# 上线服务与运维维护文档

> 适用项目：英语学习小程序（前端 `English-study-miniapp` + 后端 `English-analyzer-backend`）
> 生成方式：只读审查代码、配置、README 与 docs 后整理，仅记录可确认信息。
> 安全约定：本文档**不含任何真实密钥 / token / 密码**，敏感项只记录变量名与用途。
> 最近核对：2026-06-05

---

## 1. 文档目的

把小程序正式上线所依赖的全部应用、外部服务、云资源、域名、HTTPS、AI 服务、数据库、环境变量、密钥变量与到期维护项统一记录，便于长期运维、排障与交接。

阅读约定：

- 表格中的「来源确认状态」列取值统一为以下四种：
  - **代码确认**：信息直接来自代码或配置文件（如 `app/core/config.py`、`utils/apiClient.js`）。
  - **docs 确认**：信息来自项目部署记录文档（前端 `docs/current-phase.md`、`docs/release-checklist.md`）。
  - **人工确认**：代码与 docs 无法得出，由运维人工核对后填写。
  - **待人工补充**：尚未确认，需运维补全后再更新状态。
- 每条信息尽量标注「信息来源」，例如 `来源：utils/apiClient.js`。
- 三类对象不要混淆（见各分节）：
  - **外部服务**：微信小程序平台、腾讯云轻量服务器、腾讯机器翻译 TMT、Hunyuan、DNS、域名、HTTPS 证书。
  - **服务器组件**：FastAPI、PostgreSQL、Nginx、systemd、Certbot。
  - **配置 / 密钥变量**：`JWT_SECRET_KEY`、`WECHAT_APPID`、`WECHAT_SECRET`、`DATABASE_URL`、`TENCENT_SECRET_ID/KEY`、`HUNYUAN_API_KEY` 等（属于变量，不是独立服务）。

---

## 2. 服务总览表

| 类别 | 名称 | 用途 | 当前状态 | 来源确认状态 | 到期时间 | 维护入口 | 信息来源 |
|---|---|---|---|---|---|---|---|
| 外部服务 | 微信小程序平台 | 小程序载体、登录（code2session）、request 合法域名、审核发布 | 已配置 | 代码确认 | 不适用 | 微信公众平台 | `utils/apiClient.js`、`app/services/auth_service.py`、`project.config.json` |
| 外部服务 | 腾讯云轻量应用服务器 | 承载后端、Nginx、PostgreSQL | 运行中 | 人工确认 | 2027-06-30 | 腾讯云控制台 / TAT 免密登录 | `docs/release-checklist.md`、前端 `docs/current-phase.md` |
| 外部服务 | 域名 qingyacard.com / api.qingyacard.com | 生产 API 域名 | 已购买、已解析 | 人工确认 | 2027-05-30 | 腾讯云控制台（注册商） | `utils/apiClient.js`、前端 `docs/release-checklist.md` |
| 外部服务 | DNS（DNSPod 免费版，A 记录 api → 49.232.134.229） | 域名解析到服务器 | 已生效 | 人工确认 | 随域名状态；DNS 记录本身无固定到期 | DNSPod 控制台 | 前端 `docs/current-phase.md` |
| 外部服务 | HTTPS 证书（Let's Encrypt 免费证书） | api.qingyacard.com TLS | 已部署 | 人工确认 | 2026-08-29（自动续期验证当前失败，待单独处理） | 服务器 `certbot` | 前端 `docs/release-checklist.md` |
| 外部服务 | 腾讯机器翻译 TMT | 英→中 / 中→英 翻译、例句翻译 fallback | 集成 | 人工确认 | 免费资源包，未开启自动付费；免费资源到期 2026-06-30 | 腾讯云控制台 | `app/providers/tencent_translator.py` |
| 外部服务 | Hunyuan（混元，TokenHub OpenAI 兼容 API） | AI 例句生成 + 例句翻译 | 集成 | 人工确认 | 免费体验额度，未开启自动付费；已用约 3.61%，免费体验到期 2026-08-19 | 腾讯云 / TokenHub 控制台 | `app/services/hunyuan_example.py`、`.env.example` |
| 服务器组件 | FastAPI 后端（English Analyzer Backend） | 卡片 CRUD、英文分析、复习调度、登录 | 运行中 | 代码确认 | 不适用 | systemd `english-backend` | `app/main.py` |
| 服务器组件 | PostgreSQL | 生产数据库 | 运行中 | docs 确认 | 不适用 | TAT 免密登录 / psql | 前端 `docs/current-phase.md`、`app/database.py` |
| 服务器组件 | Nginx | 反向代理 + HTTPS 终止 | 运行中 | docs 确认 | 不适用 | TAT 免密登录 / nginx -t | 前端 `docs/release-checklist.md` |
| 服务器组件 | systemd（english-backend.service） | 守护后端进程、开机自启 | 启用 | docs 确认 | 不适用 | `systemctl` | 前端 `docs/release-checklist.md` |
| 服务器组件 | Certbot | 签发 / 续期 HTTPS 证书 | 已安装 | docs 确认 | 不适用 | TAT 免密登录 / certbot | 前端 `docs/release-checklist.md` |
| 配置/密钥 | 环境变量集（见第 9 节） | 后端运行所需密钥与配置 | 生产 `.env` 已配置 | 代码确认 | 见第 10 节 | 服务器 `/opt/english-backend/.env`（已人工确认） | `.env.example`、`app/core/config.py` |

> 注：README 与 `Dockerfile` 中还描述了一条**微信云托管**部署路径（端口 80 / 8000）。生产实际采用的是**腾讯云轻量服务器 + systemd + Nginx（后端监听 127.0.0.1:8001）**路径（来源：前端 `docs/current-phase.md`、`docs/release-checklist.md`）。云托管路径为备选/历史方案，运维时以服务器路径为准。

---

## 3. 微信小程序平台

| 项目 | 值 / 说明 | 来源确认状态 | 信息来源 |
|---|---|---|---|
| 小程序名称 | 青芽卡片 | 人工确认 | 微信公众平台后台 |
| AppID | 已创建（完整 AppID 不在本文档记录，见微信公众平台后台 / `project.config.json`） | 人工确认 | 微信公众平台后台 |
| AppID 环境变量名（后端用） | `WECHAT_APPID` | 代码确认 | `app/core/config.py`、`.env.example` |
| AppSecret 环境变量名（后端用） | `WECHAT_SECRET`（**敏感，不入文档明文**） | 代码确认 | `app/core/config.py`、`.env.example` |
| 登录流程 | 前端 `wx.login()` 取 `code` → 调后端 `POST /api/auth/wechat-login`（带 `code` + `timezone`）→ 后端用 `appid`+`secret`+`code` 请求微信 `jscode2session` 换 `openid` → 建/查 user → 签发 JWT | 代码确认 | `utils/apiClient.js`、`app/services/auth_service.py` |
| 微信 code2session URL | `https://api.weixin.qq.com/sns/jscode2session` | 代码确认 | `app/services/auth_service.py:18` |
| request 合法域名 | `https://api.qingyacard.com`（只填根域名，不带路径；不使用裸 IP / HTTP） | docs 确认 | 前端 `docs/release-checklist.md` 第二节 |
| 线上版本 | 1.0.0 | 人工确认 | 微信公众平台后台 |
| 线上版本发布时间 | 2026-05-06 15:41:07 | 人工确认 | 微信公众平台后台 |
| 小程序服务状态 | 当前为「暂停服务」；暂停原因：接入后端前由开发者主动暂停旧线上版本；正式重新上线前需恢复服务 | 人工确认 | 微信公众平台后台 |
| 前端生产后端地址 | `https://api.qingyacard.com`（默认 `BACKEND_BASE_URL`） | 代码确认 | `utils/apiClient.js:5` |
| 本地调试覆盖 | `utils/localBackendConfig.js`（可选、**不提交**，存在时覆盖默认地址） | 代码确认 | `utils/apiClient.js:7-14` |
| token / storage key | `backendUserId` / `backendAccessToken` / `backendLoginAt` | 代码确认 | `utils/apiClient.js:16-20` |
| token 携带方式 | 请求头 `Authorization: Bearer <accessToken>`；401 时自动用 `wx.login` 刷新一次后重试 | 代码确认 | `utils/apiClient.js:139-275` |
| 服务类目 | 教育服务 > 在线教育 | 人工确认 | 微信公众平台后台 |
| 隐私保护指引 | 已配置；处理信息类型：用户信息（微信昵称、头像等）；用途：用户登录、识别用户身份、保存和同步学习卡片及复习记录 | 人工确认 | 微信公众平台后台 |
| 审核材料 | 待正式提审前准备 | 待人工补充 | 前端 `docs/release-checklist.md` 第十八、二十四节 |

---

## 4. 腾讯云服务器

> 全部来自部署记录文档（前端 `docs/current-phase.md`、`docs/release-checklist.md`）或运维人工核对。代码本身不含服务器信息。

| 项目 | 值 / 说明 | 信息来源 |
|---|---|---|
| 服务器类型 | 腾讯云轻量应用服务器 | 前端 `docs/current-phase.md` |
| 公网 IP | `49.232.134.229`（仅作部署记录，**不作为小程序后端地址**） | 前端 `docs/current-phase.md` |
| 操作系统 | Ubuntu | 前端 `docs/current-phase.md` |
| 已装基础环境 | Python、PostgreSQL、Nginx、Certbot、Git | 前端 `docs/release-checklist.md` 第一节 |
| 后端部署目录 | `/opt/english-backend` | 前端 `docs/current-phase.md` |
| Python 虚拟环境 | `/opt/english-backend/.venv` | 前端 `docs/current-phase.md` |
| 生产 `.env` 路径 | `/opt/english-backend/.env`（已人工确认） | 人工确认（`app/core/config.py:11-12` 读取项目根 `.env`） |
| 代码来源 | Gitee（国内访问 GitHub 不稳定，服务器从 Gitee clone/pull） | 前端 `docs/current-phase.md` |
| 防火墙放行端口 | 80、443 | 前端 `docs/release-checklist.md` 第二节 |
| 当前主要登录方式 | 腾讯云控制台 TAT 免密登录 | 人工确认 |
| SSH | 暂未作为主要方式使用，可作为后续备用方式配置 | 人工确认 |
| 服务器到期 / 续费时间 | 2027-06-30 | 人工确认 |

---

## 5. 后端服务

| 项目 | 值 / 说明 | 来源确认状态 | 信息来源 |
|---|---|---|---|
| 框架 | FastAPI（title `English Analyzer Backend`，version `0.1.0`） | 代码确认 | `app/main.py:14-17` |
| ASGI 服务器 | uvicorn（`uvicorn[standard]`） | 代码确认 | `requirements.txt`、README |
| 启动入口 | `app.main:app` | 代码确认 | `app/main.py`、`Dockerfile` |
| 生产监听端口 | `127.0.0.1:8001`（Nginx 反代到此） | docs 确认 | 前端 `docs/current-phase.md`、`docs/release-checklist.md` |
| 本地开发端口 | 8000（README 示例 `--port 8000`） | 代码确认 | README |
| 健康检查 | `GET /health` → `{"status":"ok"}` | 代码确认 | `app/main.py:25-27` |
| systemd 服务名 | `english-backend`（`english-backend.service`，`Restart=on-failure`） | docs 确认 | 前端 `docs/release-checklist.md` 第一、二十节 |
| 启动命令 | `uvicorn app.main:app --host 127.0.0.1 --port 8001`（由 systemd 托管） | 人工确认 | 运维确认 |
| Nginx | 反向代理 `→ 127.0.0.1:8001`，含 `Host` / `X-Forwarded-For` / `X-Forwarded-Proto` 头；HTTP 自动 301/302 跳 HTTPS | docs 确认 | 前端 `docs/release-checklist.md` 第一节 |
| Nginx 站点配置文件路径 | 待人工补充 | 待人工补充 | |
| systemd unit 文件路径 | `/etc/systemd/system/english-backend.service` | 人工确认 | |

### 主要 API 路由（来源：`app/main.py` + `app/routers/*.py` 的 `APIRouter(prefix=...)`）

| 路由 | 方法 | 说明 | 来源 |
|---|---|---|---|
| `/health` | GET | 健康检查 | `app/main.py:25` |
| `/api/analyze-english` | POST | 英文分析（分类/翻译/例句） | `app/main.py:30` |
| `/api/auth/wechat-login` | POST | 微信登录换 JWT | `app/routers/auth.py:13` |
| `/api/auth/me` | GET | 当前用户信息 | `app/routers/auth.py:18` |
| `/api/cards` | GET/POST/PATCH/DELETE | 卡片 CRUD（含 `/api/cards/stats`） | `app/routers/cards.py:20`、`utils/apiClient.js` |
| `/api/review` | （prefix） | 复习相关 | `app/routers/review.py:35` |
| `/api/reviews` | GET/POST | overview / today / feedback / history / today-reviewed / sessions summary | `app/routers/reviews.py:57` |
| `/api/review-sessions` | POST | 创建复习 session | `app/routers/reviews.py:58` |

---

## 6. 数据库 PostgreSQL

| 项目 | 值 / 说明 | 来源确认状态 | 信息来源 |
|---|---|---|---|
| 生产数据库类型 | PostgreSQL（驱动 `psycopg2-binary`） | 代码确认 | `requirements.txt`、前端 `docs/current-phase.md` |
| 本地开发默认 | SQLite `sqlite:///./english_analyzer.db` | 代码确认 | `app/database.py:11`、`alembic.ini:89` |
| 连接环境变量 | `DATABASE_URL`（生产形如 `postgresql://english_user:<password>@localhost:5432/english_study`，**密码不入文档**） | 代码确认 | `app/database.py:12`、前端 `docs/current-phase.md` |
| 生产数据库名 | `english_study` | docs 确认 | 前端 `docs/release-checklist.md` |
| 生产数据库用户 | `english_user` | docs 确认 | 前端 `docs/release-checklist.md` |
| 端口 | 5432 | docs 确认 | 前端 `docs/current-phase.md` |
| ORM | SQLAlchemy 2.0 | 代码确认 | `requirements.txt`、`app/database.py` |
| 迁移工具 | Alembic（`script_location = alembic`） | 代码确认 | `alembic.ini`、`alembic/` |
| 迁移命令（生产） | `alembic upgrade heads`（存在多个 head，须用 `heads`），上下文应为 `PostgresqlImpl` | docs 确认 | 前端 `docs/current-phase.md`、`docs/release-checklist.md` |
| 注意 | `alembic.ini` 内 `sqlalchemy.url` 写的是 SQLite，仅供 env.py 兜底；实际连接由 `DATABASE_URL` 决定 | 代码确认 | `alembic.ini:89`、`app/database.py` |

### 数据表（来源：`app/models/*.py` 的 `__tablename__`）

| 表名 | 模型文件 |
|---|---|
| `users` | `app/models/user.py` |
| `cards` | `app/models/card.py` |
| `review_sessions` | `app/models/review.py` |
| `review_session_items` | `app/models/review.py` |
| `review_records` | `app/models/review.py` |
| `review_logs` | `app/models/review.py` |
| `client_actions` | `app/models/review.py` |

### 备份建议

- 生产前至少手动执行一次 `pg_dump`，并把备份放到服务器本机以外位置（来源：前端 `docs/release-checklist.md` 第二十一节）。
- 恢复命令示例：`psql -U english_user -d english_study < backup.sql`。
- **当前状态：尚未建立数据库备份机制**（人工确认）。未发现 `/opt/english-backend/backups` 目录、`*.sql` 备份文件，也未发现自定义备份 cron。
- 自动备份（cron / systemd timer）：**待建立**（需确定频率、保留策略、异地存放位置）。
- 缓存说明：英文分析缓存为进程内存 `cachetools.TTLCache`（`maxsize=5000`，`ttl=30 天`），**服务重启即清空**，无需备份（来源：README、`app/services/cache.py`）。

---

## 7. 域名、DNS、备案、HTTPS

| 项目 | 当前值 / 说明 | 来源确认状态 | 信息来源 |
|---|---|---|---|
| 主域名 | `qingyacard.com`（已购买） | docs 确认 | 前端 `docs/release-checklist.md` 第二节 |
| 域名注册商 | 腾讯云 | 人工确认 | |
| 域名到期时间 | 2027-05-30 | 人工确认 | |
| 生产 API 域名 | `api.qingyacard.com` | 代码确认 | `utils/apiClient.js:5` |
| DNS 服务商 | DNSPod 免费版 | 人工确认 | |
| DNS 解析记录 | `api.qingyacard.com` A 记录 → `49.232.134.229` | 人工确认 | 前端 `docs/current-phase.md` |
| DNS 到期 | 随域名状态；DNS 记录本身无固定到期 | 人工确认 | |
| 协议 | 强制 HTTPS（HTTP 自动跳转）；不使用裸 IP / HTTP | docs 确认 | 前端 `docs/release-checklist.md` 第一、二节 |
| 证书类型 | Let's Encrypt 免费证书 | 人工确认 | |
| 证书域名 | `api.qingyacard.com` | 人工确认 | |
| 证书文件路径 | `/etc/letsencrypt/live/api.qingyacard.com/fullchain.pem`、`/etc/letsencrypt/live/api.qingyacard.com/privkey.pem` | 人工确认 | |
| 证书有效期至 | 2026-08-29 | 人工确认 | |
| 自动续期 | `certbot renew --dry-run` 当前失败；原因尚未最终定位，自动续期验证未通过，后续需单独处理 | 人工确认 | |
| ICP 备案状态 | `qingyacard.com` ICP 备案已提交，当前处于审核流程中，尚未最终通过 | 人工确认 | |

---

## 8. AI 服务：Hunyuan 与腾讯机器翻译

### 调用链路（来源：README、`app/services/analyzer.py`、`app/services/hunyuan_example.py`、`app/services/translator.py`、`app/providers/tencent_translator.py`）

1. 前端 / 云函数 → `POST /api/analyze-english`。
2. 后端 `analyze_text` 做分类（word / phrase / sentence / unknown）。
3. 翻译：调用腾讯 TMT（`translate_to_zh`）；失败时返回 `translation:null` + warning，不报 500。
4. 例句（仅 word / phrase）：先调 **Hunyuan**（strict→loose 两次校验），失败再走 **TMT 例句模板** fallback。
5. 所有 AI 失败均静默降级，不阻断保存。

### Hunyuan（混元）

| 项目 | 值 / 说明 | 来源确认状态 | 信息来源 |
|---|---|---|---|
| 接入方式 | TokenHub Hunyuan，OpenAI 兼容 `chat/completions` | 代码确认 | `app/services/hunyuan_example.py` |
| API key 变量名 | `HUNYUAN_API_KEY`（**敏感**，请求头 `Authorization: Bearer <key>`） | 代码确认 | `app/core/config.py:20`、`.env.example` |
| Base URL 变量名 | `HUNYUAN_BASE_URL`（默认 `https://api.hunyuan.cloud.tencent.com/v1`，非敏感） | 代码确认 | `app/core/config.py:21`、`.env.example` |
| 模型变量名 / 当前值 | `HUNYUAN_MODEL`，默认 `hunyuan-role-latest`（非敏感） | 代码确认 | `app/core/config.py:22`、`.env.example` |
| 超时 | 15s | 代码确认 | `app/services/hunyuan_example.py:215` |
| 额度 / 计费 / 到期 | 免费体验额度，当前未开启自动付费；已使用约 3.61%；免费体验到期 2026-08-19 | 人工确认 | |

### 腾讯机器翻译 TMT

| 项目 | 值 / 说明 | 来源确认状态 | 信息来源 |
|---|---|---|---|
| SDK | `tencentcloud-sdk-python`（`tmt.v20180321`） | 代码确认 | `requirements.txt`、`app/providers/tencent_translator.py` |
| 接口端点 | `tmt.tencentcloudapi.com`，`TextTranslate` | 代码确认 | `app/providers/tencent_translator.py:47` |
| SecretId 变量名 | `TENCENT_SECRET_ID`（**敏感**） | 代码确认 | `app/core/config.py:17`、`.env.example` |
| SecretKey 变量名 | `TENCENT_SECRET_KEY`（**敏感**） | 代码确认 | `app/core/config.py:18`、`.env.example` |
| 区域变量名 / 默认 | `TENCENT_TMT_REGION`，默认 `ap-guangzhou`（非敏感） | 代码确认 | `app/core/config.py:19`、`.env.example` |
| 未配置时行为 | 不报 500，返回 warning「翻译暂时不可用，已先保存英文内容」 | 代码确认 | README、`app/providers/tencent_translator.py:39-40` |
| 额度 / 计费 / 到期 | 免费资源包，当前未开启自动付费；免费资源到期 2026-06-30 | 人工确认 | |

---

## 9. 环境变量与密钥变量

> 来源：`.env.example` 与 `app/core/config.py`（后端）、`cloudfunctions/analyzeEnglish/index.js`（云函数）。
> **敏感变量只记录变量名与用途，真实值一律不入文档。** 生产值存放在服务器 `.env`（路径 `/opt/english-backend/.env`，已人工确认；已被 `.gitignore` 忽略，未进仓库）。

| 变量名 | 用途 | 是否敏感 | 是否允许明文记录 | 建议存放位置 | 信息来源 |
|---|---|---|---|---|---|
| `DATABASE_URL` | 数据库连接串（生产 PostgreSQL，含密码） | 是 | 否 | 服务器 `.env` | `.env.example`、`app/database.py:12` |
| `JWT_SECRET_KEY` | 签发 / 校验登录 JWT 的密钥 | 是 | 否 | 服务器 `.env` | `.env.example`、`app/core/config.py:25` |
| `JWT_ALGORITHM` | JWT 算法，默认 `HS256` | 否 | 是 | 服务器 `.env` / 默认值 | `app/core/config.py:26` |
| `JWT_EXPIRE_DAYS` | JWT 有效期天数，默认 `30` | 否 | 是 | 服务器 `.env` / 默认值 | `app/core/config.py:27` |
| `WECHAT_APPID` | 微信小程序 AppID（公开标识） | 否（公开） | 是 | 服务器 `.env` | `.env.example`、`app/core/config.py:23` |
| `WECHAT_SECRET` | 微信小程序 AppSecret | 是 | 否 | 服务器 `.env` | `.env.example`、`app/core/config.py:24` |
| `HUNYUAN_API_KEY` | Hunyuan 例句生成 API 密钥 | 是 | 否 | 服务器 `.env` | `.env.example`、`app/core/config.py:20` |
| `HUNYUAN_BASE_URL` | Hunyuan API base URL（默认值见第 8 节） | 否 | 是 | 服务器 `.env` / 默认值 | `app/core/config.py:21` |
| `HUNYUAN_MODEL` | Hunyuan 模型名，默认 `hunyuan-role-latest` | 否 | 是 | 服务器 `.env` / 默认值 | `app/core/config.py:22` |
| `TENCENT_SECRET_ID` | 腾讯云访问密钥 ID（TMT 用） | 是 | 否 | 服务器 `.env` | `.env.example`、`app/core/config.py:17` |
| `TENCENT_SECRET_KEY` | 腾讯云访问密钥 Key（TMT 用） | 是 | 否 | 服务器 `.env` | `.env.example`、`app/core/config.py:18` |
| `TENCENT_TMT_REGION` | TMT 区域，默认 `ap-guangzhou` | 否 | 是 | 服务器 `.env` / 默认值 | `app/core/config.py:19` |
| `PYTHON_ANALYZER_URL` | 云函数 `analyzeEnglish` 转发到的后端分析地址 | 否（地址，非密钥） | 是 | 微信云函数环境变量 | `cloudfunctions/analyzeEnglish/index.js:204` |

> 说明：当前生产前端**直连** `https://api.qingyacard.com`（`utils/apiClient.js`）作为 AI 分析主链路。云函数 `analyzeEnglish` 仍保留，但**生产 `.env` 未配置 `PYTHON_ANALYZER_URL`**（人工确认），因此云函数当前**不具备实际后端兜底能力**，仅作为历史降级路径保留，后续可评估删除。

---

## 10. 到期时间与人工补充清单

| 项目 | 当前值 | 到期时间 | 是否自动续费 | 维护入口 | 备注 |
|---|---|---|---|---|---|
| 域名 `qingyacard.com` | 已购买（注册商：腾讯云） | 2027-05-30 | 否（未开启自动续费） | 腾讯云控制台 | 过期将导致 API 域名不可用 |
| 腾讯云轻量服务器 | 运行中（IP 49.232.134.229） | 2027-06-30 | 否（未开启自动续费） | 腾讯云控制台 / TAT 免密登录 | 过期将停机 |
| HTTPS 证书（api.qingyacard.com，Let's Encrypt） | 已部署 | 2026-08-29 | 否（`certbot renew --dry-run` 当前失败） | 服务器 `certbot certificates` | 自动续期验证未通过，原因待定位，需单独处理 |
| ICP 备案（qingyacard.com） | 已提交，审核流程中 | 尚未最终通过 | — | 云服务商备案系统 | 当前未通过，重新上线前关注 |
| Hunyuan 账户额度 | 免费体验额度，已使用约 3.61% | 2026-08-19（免费体验到期） | 否（未开启自动付费） | 腾讯云 / TokenHub 控制台 | 到期需评估是否开通付费 |
| 腾讯云 TMT 额度 | 免费资源包 | 2026-06-30（免费资源到期） | 否（未开启自动付费） | 腾讯云控制台 | 到期需评估是否开通付费 |
| `JWT_SECRET_KEY` 轮换 | 已配置 | 无固定到期 | — | 服务器 `.env` | 轮换会使所有现有 token 失效，需用户重新登录 |
| 数据库备份 | 尚未建立数据库备份机制 | — | — | 服务器 / 对象存储 | 未发现 backups 目录 / *.sql / 备份 cron，需尽快建立 |
| 负责人 / 值班人 | 待人工补充 | — | — | — | 建议填写主负责人与备份联系人 |

---

## 11. 上线后定期检查清单

### 每周检查

- [ ] `curl https://api.qingyacard.com/health` 返回 `{"status":"ok"}`
- [ ] `sudo systemctl status english-backend` 为 `active (running)`
- [ ] `sudo journalctl -u english-backend --since "1 week ago" --no-pager | grep -i error` 无关键 ERROR
- [ ] 添加卡片 → AI 分析 / 翻译 / 例句正常（抽测 1 个单词、1 个短语）
- [ ] 磁盘剩余空间充足（`df -h`），日志未撑爆磁盘

### 每月检查

- [ ] `sudo certbot certificates` 确认证书有效期 > 30 天且自动续期正常
- [ ] `pg_dump` 备份成功并验证可恢复；确认备份已存到本机以外位置
- [ ] 检查 Hunyuan / 腾讯云 TMT 账户额度与账单
- [ ] 确认域名、服务器距到期 > 30 天（见第 10 节）
- [ ] 复查 `git status` 无 `.env` / `localBackendConfig.js` / 本地配置被误提交
- [ ] 服务器系统安全更新（`sudo apt update && sudo apt upgrade`，按变更窗口）

### 到期前提醒（建议提前 30 天）

- [ ] 域名续费（2027-05-30 到期）
- [ ] 服务器续费（2027-06-30 到期）
- [ ] HTTPS 证书续期确认（证书 2026-08-29 到期；`certbot renew --dry-run` 当前失败，需先修复自动续期）
- [ ] AI 服务额度评估（TMT 免费资源 2026-06-30 到期、Hunyuan 免费体验 2026-08-19 到期，均未开启自动付费）

---

## 12. 常用检查命令

### 后端 / 服务器（来源：前端 `docs/release-checklist.md`、`docs/current-phase.md`）

```bash
# 健康检查
curl https://api.qingyacard.com/health
curl http://127.0.0.1:8001/health        # 服务器本机

# 后端服务（systemd）
sudo systemctl status english-backend
sudo systemctl restart english-backend
sudo journalctl -u english-backend -n 80 --no-pager

# Nginx
sudo nginx -t
sudo systemctl reload nginx
curl -I https://api.qingyacard.com/health   # 期望 200
curl -I http://api.qingyacard.com/health    # 期望 301/302 跳 HTTPS

# 数据库 / 迁移
alembic upgrade heads
pg_dump -U english_user english_study > backup.sql
psql -U english_user -d english_study < backup.sql   # 恢复

# HTTPS 证书
sudo certbot certificates
sudo certbot renew --dry-run
```

> 生产后端不要手动长期运行 `uvicorn`，应由 systemd 托管。

### 本地后端开发（来源：README）

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
python -m pytest -q
python -m py_compile app/routers/reviews.py
```

### 前端（来源：根 `CLAUDE.md`、前端 `CLAUDE.md`）

```bash
node --check <changed-js-file>
git diff --check
git status --short
# 功能验收：微信开发者工具人工编译 + 真机扫码
```

---

## 13. 信息来源与待确认项

### 已从代码 / 配置 / docs 确认的服务与组件

- 后端框架与入口、健康检查、API 路由（`app/main.py`、`app/routers/*.py`）
- 配置 / 密钥变量名与用途（`.env.example`、`app/core/config.py`）
- 数据库类型、连接变量、表结构、迁移工具（`app/database.py`、`app/models/*.py`、`alembic.ini`）
- 微信登录流程与 code2session（`app/services/auth_service.py`、`utils/apiClient.js`）
- 生产域名、token storage key、前端后端地址（`utils/apiClient.js`、`project.config.json`）
- AI 调用链路、Hunyuan / TMT 接入与变量（`app/services/hunyuan_example.py`、`app/providers/tencent_translator.py`、README）
- 服务器、PostgreSQL、Nginx、systemd、Certbot、域名、DNS、HTTPS 部署事实（前端 `docs/current-phase.md`、`docs/release-checklist.md`）

### 已由人工确认的项

- 当前主要登录方式：腾讯云控制台 TAT 免密登录
- 域名注册商：腾讯云；域名到期：2027-05-30；服务器到期：2027-06-30
- DNS：DNSPod 免费版；`api.qingyacard.com` A 记录 → `49.232.134.229`
- HTTPS：Let's Encrypt 免费证书，证书域名 `api.qingyacard.com`，有效期至 2026-08-29；证书文件 `/etc/letsencrypt/live/api.qingyacard.com/{fullchain,privkey}.pem`
- HTTPS 自动续期：`certbot renew --dry-run` 当前失败，原因尚未最终定位，需单独处理（**未确认与 ICP 备案相关**）
- ICP 备案：`qingyacard.com` 已提交，审核流程中，尚未最终通过
- 微信小程序：名称「青芽卡片」，AppID 已创建（完整值不记录），request 合法域名 `https://api.qingyacard.com`，当前为「暂停服务」（接入后端前主动暂停旧版本，重新上线前需恢复）
- AI：Hunyuan 免费体验额度已用约 3.61%、2026-08-19 到期；TMT 免费资源 2026-06-30 到期；两者均未开启自动付费
- 后端启动：`uvicorn app.main:app --host 127.0.0.1 --port 8001`，systemd `english-backend.service`，unit 文件 `/etc/systemd/system/english-backend.service`，部署目录 `/opt/english-backend`，生产 `.env` 路径 `/opt/english-backend/.env`
- 域名 / 服务器：均未开启自动续费
- 微信平台：服务类目「教育服务 > 在线教育」；隐私保护指引已配置（处理用户信息——微信昵称、头像等，用于登录、识别身份、保存同步学习卡片及复习记录）
- 微信小程序线上版本：1.0.0，发布时间 2026-05-06 15:41:07，当前「暂停服务」（接入后端前主动暂停旧线上版本，重新上线前需恢复）
- 云函数：生产 `.env` 未配置 `PYTHON_ANALYZER_URL`，`analyzeEnglish` 当前无实际后端兜底能力，仅作历史降级路径保留，后续可评估删除
- 数据库备份：尚未建立备份机制

### 待人工补充清单

- SSH 备用接入方式与密钥归属（当前主要使用 TAT 免密登录，SSH 暂未作为主要方式）
- HTTPS `certbot renew --dry-run` 失败的根因定位与修复
- ICP 备案审核最终结果
- 数据库备份机制的建立（频率 / 保留 / 异地存放）
- Nginx 站点配置文件路径
- 微信审核材料（待正式提审前准备）
- 运维负责人 / 值班人

### 安全核查结果（仓库密钥泄露）

> 核查方式：`git ls-files` + `git log --all -- .env` 检查两个仓库的跟踪文件与历史。

- 后端仓库：`.env` 已被 `.gitignore` 忽略，**未被 git 跟踪、历史中无记录**；仓库内仅有 `.env.example`，其中全部为占位符（如 `your_..._here`、`change_me_to_a_long_random_secret`），不含真实值。
- 代码中无硬编码密钥，所有密钥经 `app/core/config.py` 从环境变量读取。
- 前端仓库：`.env` / `utils/localBackendConfig.js` 均未被跟踪。
- **未发现真实密钥被提交到仓库。** 暂无密钥泄露风险。
- 低优先级提醒（非密钥泄露）：前端 `project.private.config.json` 已被 git 跟踪（项目规范建议不提交），但其内容仅为微信开发者工具本地设置，**不含任何密钥**；可按需从跟踪中移除以符合规范。

> 维护本文档时：补全「待人工补充」项后请把对应行的「来源确认状态 / 到期时间」更新为实际值，并保持不写入任何真实密钥的原则。
</content>
