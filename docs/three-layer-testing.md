# 三层测试体系

Layer 1 是冻结产品契约，也是后两层的唯一 oracle。不得为通过集成或 E2E 测试而放宽它。规则清单在 `tests/fixtures/rule_ids.json`，逐层映射在 `tests/contract_traceability.json`，映射完整性由 `tests/test_contract_traceability.py` 自动检查。

## 一键入口

在后端目录执行：

```powershell
# Tier A：每日，Frozen Layer 1 + 小程序确定性测试
.\scripts\run-three-layer-tests.ps1 -Tier Daily

# Tier B：推送前，再加完整的本地回归
.\scripts\run-three-layer-tests.ps1 -Tier PrePush

# 单独运行完整 Layer 2 或 Layer 3
.\scripts\run-three-layer-tests.ps1 -Tier Layer2
.\scripts\run-three-layer-tests.ps1 -Tier Layer3

# Tier C：发布门禁，Layer 1 + 完整 Layer 2 + 固定真实微信旅程
.\scripts\run-three-layer-tests.ps1 -Tier Release

# Tier D：查看容量、灾难和真机专项入口
.\scripts\run-three-layer-tests.ps1 -Tier Manual
```

Tier C 需要当前 `.env` 中的正式开发依赖配置可用，但所有 PostgreSQL 写入只进入脚本创建的隔离测试库。脚本不会执行生产库迁移，也不会修改 `.env`。

## Layer 1：冻结契约

固定语料和 Rule ID 覆盖规范化、硬错误、分类、ECDICT/SymSpell、Harper 决策、capability、卡片、可靠性和复习规则。核心命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_frozen_contract_layer1.py tests\test_frozen_api_card_layer1.py tests\test_frozen_lexical_tts_layer1.py
```

## Layer 2：真实服务与隔离 PostgreSQL

Layer 2 由三部分组成：

```powershell
# 全量 pytest，使用新建、迁移到 head、完成后删除的 PostgreSQL 测试库
.\scripts\run-postgresql-tests.ps1

# 真实 ECDICT、SymSpell、Harper 和 Piper；自动管理 Harper 并生成 JUnit artifact
.\scripts\run-layer2-real.ps1

# 单/多用户、5/10/30/100 负载、HTTP/AI/TTS 容量、Ollama/Piper/DB 故障恢复
.\scripts\run-level7-e2e.ps1 -Through all
```

Level 7 每次在 `.e2e-artifacts/<run-id>/` 保存环境摘要、场景结果、HTTP/request ID、后端日志、指标、数据库最终审计和失败诊断。最终数据库审计要求卡片、幂等、用户隔离、复习状态、V1 answer/question/options snapshot 全部一致。

`test_reviews_phase2_api.py` 及 `test_postgresql_integration.py` 中三条旧四等级 feedback 用例不属于当前 release gate：Review V1 已明确替换该产品语义，不能为让旧测试通过而重新引入 `forgot/shaky/got_it/fluent`。V1 由 `test_review_v1_api.py`、`test_review_v1_postgresql.py` 和 Level 7 最终数据库审计覆盖；这些不是 SKIP，而是从正式选择集中移除的已退役契约。

Harper 启停还必须通过正式脚本验证：

```powershell
$env:NGROK_ENABLED = 'false'
.\start-server.ps1
Invoke-RestMethod http://127.0.0.1:8082/health
.\stop-server.ps1
```

`start-server.ps1` 不运行 migration；`stop-server.ps1` 只停止状态文件中记录并通过身份校验的进程。

## Layer 3：真实微信开发者工具

```powershell
.\scripts\run-level7-wechat-e2e.ps1
```

runner 使用运行时副本，不改源小程序；自动发现开发者工具 service port，并通过 `miniprogram-automator` 执行真实 `wx.login`、JWT、今日一句保存、发现素材认识/保存、添加页预填、Card/FSRS 复习、校验、卡片编辑/同步重放、AI streaming、取消后重生、TTS、401 恢复和退出登录。Harper SYSTEM_WARNING 使用明确标注的受控进程停止/恢复，不声称是真实断网。

成功 artifact 还包含 `layer3-journeys.json`。可独立校验其证据契约：

```powershell
$env:RUN_LAYER3 = '1'
$env:LAYER3_ARTIFACT_PATH = '<artifact目录>\layer3-journeys.json'
Set-Location ..\English-study-miniapp
node --test tests\layer3-journeys.test.js
```

卡片 delete/tombstone 的真实 UI 专项使用：

```powershell
.\scripts\run-level7-wechat-crud-e2e.ps1
```

如果只能稳定获得一个真实微信身份，Layer 3 多账号标记为环境限制；A/B 隔离仍由 Layer 2 的真实 PostgreSQL 并发测试作为 release oracle。

## Tier D：Manual / Capacity / Disaster

以下项目不伪装成自动化 PASS：

- iOS/Android 实体扬声器是否真正出声；开发者工具只证明 WAV、下载、播放 API 和状态。
- 真实手机网络切换、系统音量、静音模式和蓝牙路由。
- 正式数据库耗尽、进程级灾难、备份/恢复演练；必须按 release runbook 在明确隔离目标中执行。
- 两个真实微信账号的端到端隔离；仅在账号条件稳定时执行。

高并发和服务故障的安全自动化部分已经纳入 `run-level7-e2e.ps1 -Through all`。
