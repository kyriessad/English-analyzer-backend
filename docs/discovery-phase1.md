# 发现素材与今日一句（Phase 1）

## 数据边界

公共素材保存在 `public_material_packs` 和 `public_material_items`，不是用户学习数据，也不会自动创建 Card。用户级素材状态仅保存 `known`；“想记住”只把内容带入现有添加卡片页。用户确认保存后，仍由原 Card API 完成英文合法性校验、持久化、AI 分析、TTS 和 FSRS。

`in_library` 不保存冗余关系，而是按当前用户有效 Card 的规范化英文实时判断。这样从素材页、今日一句或手动输入保存同一内容时，结果一致。

## 内容

- CET4、CET6、考研、IELTS、TOEFL：从固定版本 ECDICT 的 `tag` 字段导入，每包默认 500 条。
- 日常表达、职场英语、旅行英语、自然口语、常见短语、实用短句：版本化编辑内容。
- 今日一句：365 条版本化、预生成内容；请求时不调用 AI。按 `Asia/Shanghai` 日期确定当日条目，同一天对所有用户一致。

导入是幂等的，稳定 UUID 由内容版本、包和英文内容生成；重复执行会更新当前版本并隐藏已移除条目，不会创建 Card。

```powershell
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe scripts\seed_discovery_content.py --audit-content --word-limit 500
```

ECDICT 运行库必须包含 `word`、`phonetic`、`translation`、`tag`、`frq`、`bnc`、`pos`。可用 `scripts\setup-ecdict.ps1` 重建。

## API

所有接口都需要现有 Bearer JWT：

- `GET /api/discovery/packs`：素材包和当前用户剩余数量。
- `GET /api/discovery/items`：按包分页浏览，可搜索；返回 `known`、`in_library`。
- `PUT /api/discovery/items/{item_id}/state`：幂等设置 `known`。
- `GET /api/discovery/today-quote`：返回固定时区日期和当日已审核句子。

## 小程序流程

首页先显示独立“今日一句”，再显示发现入口和原卡片库。发现页提供包筛选、搜索、分页、“认识”和“想记住”。“想记住”通过短期、一次性预填状态打开原添加页；预填可修改，保存仍走原 Card 链路。今日一句来源固定为“今日一句”，普通素材来源为“发现素材 · <包名>”。

## 发布与验收

`scripts\release-upgrade.ps1` 在数据库迁移验证后执行幂等素材导入。正式发布前运行：

```powershell
.\scripts\run-three-layer-tests.ps1 -Tier Release
```

Layer 3 证据必须来自真实微信开发者工具、`wx.login` 和 `wx.request`；直调后端或伪造身份不能替代。
