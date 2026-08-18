# English Analyzer Backend

## Current Status: Local Development Only

As of 2026-06-28 this project is no longer a public online service.

- The Tencent Cloud Lighthouse server has been returned and destroyed.
- The former production FastAPI/Nginx/PostgreSQL/Certbot/systemd stack no longer exists.
- The DNSPod `api.qingyacard.com` A record has been deleted.
- `https://api.qingyacard.com` is invalid and must not be used as the default backend.
- The WeChat request legal domain for `https://api.qingyacard.com` has been deleted or should be treated as unused.
- The Tencent Cloud ICP filing subject has been submitted for cancellation.
- Public security network filing is no longer being handled.
- `qingyacard.com` is retained but currently has no DNS resolution and is not used by this project.
- The Mini Program remains paused and is kept for Windows local development and personal use.

Current local stack:

- WeChat DevTools
- FastAPI at `http://127.0.0.1:8000`
- PostgreSQL at the password-redacted target selected by `DATABASE_URL`
- Argos Translate for English to Simplified Chinese translation
- Ollama local API at `http://127.0.0.1:11434`
- `qwen3:8b` for word/phrase example sentence generation

PostgreSQL is the formal runtime database. MySQL is not planned. SQLite is
retained only inside explicitly isolated tests and must not be used as evidence
that the PostgreSQL schema is current.

Tencent TMT and Hunyuan implementation files are retained as legacy optional providers, but the default local flow does not call them. Rollback requires explicit configuration:

```env
TRANSLATION_PROVIDER=tencent
EXAMPLE_GENERATOR_PROVIDER=hunyuan
ENABLE_TENCENT_TMT=true
ENABLE_HUNYUAN=true
```

Do not put real API keys, AppSecrets, JWT secrets, database passwords, Argos model files, or Ollama model files in Git.

## Local Setup

Use the existing backend virtual environment:

```powershell
cd C:\Users\Administrator\WeChatProjects\English-analyzer-backend
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\scripts\setup-local-ai.ps1
```

If `ollama` is not installed, install it from the official Windows installer or rerun the official PowerShell installer:

```powershell
irm https://ollama.com/install.ps1 | iex
```

Then confirm:

```powershell
ollama --version
ollama pull qwen3:8b
ollama list
Invoke-RestMethod http://127.0.0.1:11434/api/tags
```

Start the backend:

```powershell
.\scripts\start-local-backend.ps1
```

The start script first runs a password-free database preflight. It prints the
dialect, host, port, database, schema, current user, configuration source and
Alembic revision. Startup stops if the target is not the approved PostgreSQL
database or is not at the required revision.

Equivalent manual command:

```powershell
.\.venv\Scripts\python.exe scripts\check_database_target.py
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

In WeChat DevTools, open the Mini Program project, ensure requests go to `http://127.0.0.1:8000`, and enable: "不校验合法域名、web-view、TLS 版本以及 HTTPS 证书".

WeChat login is still real. Local login requires valid `WECHAT_APPID`, `WECHAT_SECRET`, and `JWT_SECRET_KEY` in `.env`. No test login backdoor is provided.

## Local AI Flow

`POST /api/analyze-english` currently uses:

```text
Input text
-> validator
-> cache, discarding stale word/phrase cache entries with empty exampleSentence
-> Argos Translate for base translation
-> understanding helper
-> word/phrase only: Ollama qwen3:8b
-> validate exampleSentence with existing exact/inflection/continuous phrase rules
-> use Qwen exampleTranslation, or Argos fallback when empty
-> do not cache word/phrase results without exampleSentence
```

Sentence and paragraph inputs receive base translation only and do not generate extra examples.

Common faults:

- `ollama` command not found: install Ollama for Windows, then reopen PowerShell.
- Ollama API connection refused: start Ollama or run `ollama serve`; keep it bound to `127.0.0.1`.
- `qwen3:8b` missing: run `ollama pull qwen3:8b`.
- Argos en->zh model missing: run `.\.venv\Scripts\python.exe tools\install_argos_en_zh.py`.
- First Qwen request is slow: the model is loading; later calls should be faster because requests use `keep_alive`.
- Frontend still requests production: remove or update ignored `utils/localBackendConfig.js` and confirm `utils/apiClient.js`.
- Analyze request timeout: only the Mini Program analyze endpoint timeout is 60s; other API timeouts are unchanged.
- WeChat login fails: check `WECHAT_APPID`, `WECHAT_SECRET`, and `JWT_SECRET_KEY`; do not commit them.
- Database preflight rejects an accidental SQLite target outside explicit test mode.
- Confirm no Tencent calls: default `.env.example` has `ENABLE_TENCENT_TMT=false`, `ENABLE_HUNYUAN=false`; tests patch and assert legacy providers are not called.

## Level 1 observability

This backend exposes minimal single-node observability without Grafana, Jaeger,
Loki, Redis or Docker:

- Logs: stdout JSON lines. Each request has `request_id`; a valid incoming
  `X-Request-ID` is preserved, otherwise the server generates one.
- Metrics: open `http://127.0.0.1:8000/metrics` for Prometheus text metrics.
  Core series include `http_request_duration_seconds`,
  `http_requests_total`, `ai_requests_total`, `ai_cache_events_total`,
  `tts_cache_events_total`, `db_operations_total` and
  `component_operations_total`.
- Traces: OpenTelemetry spans are printed to stdout by the console exporter
  when `TRACING_ENABLED=true` and `opentelemetry-api` /
  `opentelemetry-sdk` are installed. Spans include `request_id` so they can be
  matched with logs and metrics labels.

这是微信英语学习小程序后续用于英文内容检测、腾讯云机器翻译 TMT 翻译和“我的理解”生成的 Python FastAPI 后端。当前版本是最小可运行框架，后续可由微信云函数转发调用。

## 进入后端目录

```powershell
cd C:\Users\Administrator\WeChatProjects\English-analyzer-backend
```

## 创建虚拟环境

```powershell
python -m venv .venv
```

## 激活虚拟环境

```powershell
.venv\Scripts\activate
```

## 安装依赖

```powershell
pip install -r requirements.txt
```

## 配置环境变量

复制 `.env.example` 为 `.env`，然后填写腾讯云机器翻译 TMT 配置。

```powershell
copy .env.example .env
```

`.env` 示例：

```env
TENCENT_SECRET_ID=your_tencent_secret_id_here
TENCENT_SECRET_KEY=your_tencent_secret_key_here
TENCENT_TMT_REGION=ap-guangzhou
```

`TENCENT_SECRET_ID` 和 `TENCENT_SECRET_KEY` 来自腾讯云访问密钥，`TENCENT_TMT_REGION` 默认使用 `ap-guangzhou`。如果暂时没有配置密钥，服务仍然可以启动；调用分析接口时会返回翻译不可用的 warning，不会返回 500。

## 缓存说明

当前缓存使用 `cachetools.TTLCache`，属于进程内存缓存，服务重启后会清空：

- `maxsize=5000`
- `ttl=30 天`
- 服务重启、代码重载、重新部署后缓存会清空
- `ttl=30 天` 只在服务进程持续运行时有效
- 后续可替换为 SQLite、Redis 或 diskcache

## 启动服务

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

## 健康检查

浏览器访问：

```text
http://127.0.0.1:8000/health
```

或使用 curl：

```powershell
curl.exe http://127.0.0.1:8000/health
```

预期返回：

```json
{
  "status": "ok"
}
```

## 测试英文分析接口

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/analyze-english `
  -H "Content-Type: application/json" `
  -d "{\"text\":\"look forward to\",\"cardType\":\"auto\",\"targetLang\":\"zh\"}"
```

返回结构统一为：

```json
{
  "ok": true,
  "level": "pass",
  "category": "phrase",
  "normalizedText": "look forward to",
  "translation": "期待；盼望",
  "understanding": "这个短语大致表示“期待；盼望”。复习时可以重点看它在句子中的搭配方式。",
  "warnings": [],
  "errors": [],
  "provider": "tencent",
  "cacheHit": false
}
```

如果没有配置腾讯云密钥，接口仍会返回统一结构，例如：

```json
{
  "ok": true,
  "level": "pass",
  "category": "phrase",
  "normalizedText": "look forward to",
  "translation": null,
  "understanding": "你可以在这里写下自己对这个内容的理解。",
  "warnings": [
    "翻译暂时不可用，已先保存英文内容。"
  ],
  "errors": [],
  "provider": "tencent",
  "cacheHit": false
}
```

## 建议测试用例

可在后端目录执行下面的快速检查：

```powershell
cd C:\Users\Administrator\WeChatProjects\English-analyzer-backend

.venv\Scripts\activate

python -c "from app.services.analyzer import analyze_text; cases=['autum','ChatGPT','iPhone',\"women's\",'API','Go away.','I love you.','look forward to','你好','@@@@@']; [print(text, '=>', analyze_text(text)['level'], analyze_text(text)['category'], analyze_text(text)['errors']) for text in cases]"
```

预期重点：

- `autum` -> `warning`
- `ChatGPT` -> 不进行拼写检查，不 `error`
- `iPhone` -> 不进行拼写检查，不 `error`
- `women's` -> 不进行拼写检查，不 `error`
- `API` -> 不进行拼写检查，不 `error`
- `Go away.` -> `sentence`
- `I love you.` -> `sentence`
- `look forward to` -> `phrase`
- `你好` -> `error`
- `@@@@@` -> `error`

## 批量接口测试脚本

脚本会调用 `/api/analyze-english`，并输出：

- `test_results/analyze_english_results.csv`
- `test_results/analyze_english_results.md`

本地测试：

```powershell
python tests/test_samples.py
```

测试云托管：

```powershell
$env:ANALYZER_API_URL="https://你的云托管地址/api/analyze-english"
python tests/test_samples.py
```

## 微信云托管部署说明

上传代码包时选择 `English-analyzer-backend` 目录作为服务代码目录。

云托管服务端口填写：

```text
80
```

云托管环境变量需要配置：

```env
TENCENT_SECRET_ID=your_tencent_secret_id_here
TENCENT_SECRET_KEY=your_tencent_secret_key_here
TENCENT_TMT_REGION=ap-guangzhou
```

部署完成后先测试健康检查：

```text
https://你的云托管服务域名/health
```

预期返回：

```json
{
  "status": "ok"
}
```

再测试英文分析接口：

```powershell
curl.exe -X POST https://你的云托管服务域名/api/analyze-english `
  -H "Content-Type: application/json" `
  -d "{\"text\":\"look forward to\",\"cardType\":\"auto\",\"targetLang\":\"zh\"}"
```

接口应返回统一结构，并在腾讯云密钥配置正确时返回中文翻译。

## 后续接入方式

微信小程序前端不直接调用该服务。建议后续由现有微信云函数作为转发层调用这个 FastAPI 后端，再把统一响应结构返回给小程序端。
