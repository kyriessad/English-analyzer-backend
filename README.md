# English Analyzer Backend

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
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
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
