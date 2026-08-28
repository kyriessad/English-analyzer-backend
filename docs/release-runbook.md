# Release / Upgrade Runbook

适用于 Windows 单机 FastAPI + PostgreSQL + Ollama/Piper + ngrok。所有命令在项目目录执行：

```powershell
cd C:\Users\Administrator\WeChatProjects\English-analyzer-backend
```

## 发布前

1. 确认工作区、目标 commit 和当前正式服务状态；不要把 `.env` 纳入提交。
2. 执行只读检查和测试：

```powershell
.\scripts\preflight-release.ps1
& .\.venv\Scripts\python.exe -m pytest -q tests\test_config.py
```

3. migration 前复用现有备份脚本。若数据库仍在旧 revision，使用显式例外参数；脚本仍会校验归档和 SHA-256：

```powershell
.\scripts\backup-postgresql.ps1 -AllowRevisionMismatch
```

记录输出的 backup path、revision 和 SHA-256。备份失败不得继续。

## 发布

```powershell
.\stop-server.ps1
& .\.venv\Scripts\alembic.exe upgrade head
if ($LASTEXITCODE -ne 0) { throw 'Alembic upgrade failed; do not start new code.' }
.\start-server.ps1
```

`start-server.ps1` 只检查数据库 revision 与代码唯一 head 是否一致，不自动执行 migration；同时执行现有 PostgreSQL、Ollama/Qwen、Piper、FastAPI 和 health 检查。ngrok 默认开启；设置 `NGROK_ENABLED=false` 才使用本机/LAN 模式。

## 验证

确认输出 `SERVER READY`、`CONFIG PREFLIGHT PASS`，并访问 `http://127.0.0.1:8000/health`。随后做最小人工 smoke test：认证链和 Card list；不要伪造正式用户 token。

## 失败与回退

- migration 前失败：停止流程，正式系统不变。
- migration 失败：确认 Alembic 事务已回滚，不启动新代码；修复 migration 后重新发布。
- migration 成功但新代码启动或 smoke test 失败：停止新服务。代码回退不能回退数据库；若 migration 有安全 downgrade，先执行对应 downgrade，再回退代码；否则使用发布前 backup 通过现有 restore 流程恢复隔离确认后再恢复正式库。
- 不可逆数据/DDL、无法证明 downgrade 安全或 restore 结果时，保持服务停止并人工决策，不强行回退。
