# 英语学习小程序后端化重构设计文档

> 版本：v1.0  
> 状态：设计草案  
> 适用项目：English-analyzer-backend / 英语学习微信小程序  
> 当前重点：先完成后端化方案设计，不直接改动小程序业务代码。

---

## 0. 文档目的

本文档用于明确英语学习小程序后端化重构的整体方向、迁移顺序、数据库结构、接口设计和风险控制方案。

当前阶段的目标不是一次性重写系统，而是先建立一份清晰、可执行、可回退的工程设计文档，避免后续开发时在以下问题上反复摇摆：

- 卡片数据到底以后存在哪里；
- 复习规则到底由前端还是后端决定；
- 复习记录是否需要后端数据库；
- 微信云数据库和自建 Python 后端如何过渡；
- 弱网场景下如何避免数据丢失和重复提交。

---

## 1. 当前背景

英语学习小程序已经上线，当前核心功能包括：

- 添加英语卡片；
- 编辑英语卡片；
- 删除英语卡片；
- 首页查看卡片；
- 今日复习；
- 今日复习总结；
- 今日复习内容；
- 历史复习内容；
- 英文内容检测与翻译。

目前数据和逻辑主要分散在以下位置：

| 模块 | 当前职责 |
|---|---|
| 小程序本地缓存 | 本地卡片、复习状态、弱网 pending 状态 |
| 微信云数据库 | 云端卡片数据、部分复习相关数据 |
| 微信云函数 | 转发请求、调用 Python 后端、适配小程序旧结构 |
| Python FastAPI 后端 | 英文检测、规则校验、翻译、生成“我的理解” |
| 小程序前端 / recordStorage.js | 复习规则、今日任务生成、本地复习状态维护 |

当前 Python 后端已经承担英文分析能力，但卡片数据、复习记录和复习规则仍然主要由小程序端和微信云数据库维护。

随着复习规则逐渐变复杂，如果继续把核心业务逻辑放在小程序前端，会带来以下问题：

1. 复习规则越来越难维护；
2. 本地缓存、微信云数据库、后端之间的数据边界不清晰；
3. 弱网状态下容易出现 pending、failed、重复同步等问题；
4. 后续做统计分析、个性化复习、相似卡片检测会比较困难；
5. 代码继续堆在小程序端，会增加上线后出错风险。

因此，本项目需要逐步把复习规则、复习记录和卡片主数据迁移到 Python 后端。

---

## 2. 后端化目标

后端化的核心目标不是简单替换微信云数据库，而是让 Python 后端逐步成为长期的数据和规则中心。

### 2.1 核心目标

1. 将复习规则统一迁移到 Python 后端；
2. 将复习记录保存到后端数据库；
3. 将卡片主数据逐步从微信云数据库迁移到后端数据库；
4. 小程序继续保留本地缓存，保证弱网情况下仍能查看、保存和复习；
5. 通过 pending 队列处理弱网状态下的延迟同步；
6. 为后续统计分析、个性化复习推荐、相似卡片检测等能力预留空间。

### 2.2 不做的事情

当前阶段暂时不做：

- 不一次性废弃微信云数据库；
- 不直接重写整个小程序；
- 不马上迁移所有历史数据；
- 不把本地缓存完全删除；
- 不在第一阶段追求完整后端主数据架构。

---

## 3. 总体架构方向

长期目标架构如下：

```text
微信小程序前端
  ↓
本地缓存 + 本地 pending 队列
  ↓
Python FastAPI 后端
  ↓
PostgreSQL 数据库
```

其中：

- 小程序前端主要负责页面展示、用户交互和弱网缓存；
- Python 后端负责复习规则、卡片数据、复习记录、英文分析和统计服务；
- PostgreSQL 作为长期主数据库；
- 微信云数据库在迁移期保留，后续逐步降级为备份或废弃。

---

## 4. 总体迁移原则

本次后端化不做一次性全量迁移，而是分阶段推进。

核心原则如下：

1. 不影响已上线小程序的稳定性；
2. 不直接删除微信云数据库中的旧数据；
3. 不破坏现有本地缓存逻辑；
4. 每一步迁移都要有回退空间；
5. 后端接口先小范围接入，再逐步替换旧逻辑；
6. 先迁复习规则，再迁复习记录，最后迁卡片主数据；
7. 即使后端化，小程序仍然保留本地缓存和 pending 队列；
8. 所有涉及弱网重试的接口都要考虑幂等性。

---

## 5. 分阶段迁移计划

### 5.1 阶段一：后端化复习规则

目标：把“今日应该复习哪些卡片”的判断逻辑迁到后端。

本阶段可以先不迁移卡片主数据。小程序可以把当前本地或微信云数据库中的卡片列表传给后端，由后端计算今日复习任务。

重点验证：

- 后端能否正确筛选今日复习卡片；
- 后端能否正确处理新卡、到期卡、待加强卡；
- 后端能否生成稳定的今日复习任务；
- 后端是否能避免重复生成任务；
- 用户中途退出后，是否能恢复未完成复习会话。

### 5.2 阶段二：后端化复习记录

目标：用户点击“没记住 / 模糊 / 记住了 / 太简单”后，将复习记录保存到后端。

后端需要统一处理：

- review_count；
- again_count；
- hard_count；
- good_count；
- easy_count；
- last_review_result；
- last_reviewed_at；
- next_review_at。

重点验证：

- 复习反馈是否能正确入库；
- 弱网重复提交是否能通过 client_record_id 幂等处理；
- 今日总结是否能从后端生成；
- 历史复习内容是否能从后端查询。

### 5.3 阶段三：后端化卡片主数据

目标：后端数据库成为卡片主数据来源。

后端需要支持：

- 新增卡片；
- 编辑卡片；
- 删除卡片；
- 卡片列表查询；
- 卡片搜索；
- 首页筛选；
- 英文分析状态维护。

重点验证：

- 新增卡片是否进入后端数据库；
- 编辑卡片是否正确同步到后端；
- 删除卡片是否采用软删除；
- 微信云数据库旧卡片是否能安全迁移；
- 是否会出现重复卡片。

### 5.4 阶段四：微信云数据库降级或废弃

目标：后端数据库稳定后，逐步减少对微信云数据库的依赖。

最终状态：

- 后端数据库作为主数据源；
- 小程序本地作为缓存和 pending 队列；
- 微信云数据库作为迁移期备份，后续可逐步停用。

---

## 6. 数据库设计

第一版正式数据库建议使用 PostgreSQL。

本地开发阶段可以使用 SQLite 做原型，但正式部署不建议长期依赖 SQLite。

### 6.1 第一版核心表

第一版核心表包括：

1. users
2. cards
3. review_sessions
4. review_session_items
5. review_records

暂时不强制新增 analysis_jobs 表。英文分析当前已经有独立后端能力，后续需要任务化时再补充。

---

## 7. 表结构设计

### 7.1 users 表

用途：保存用户身份，并绑定微信 openid。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | uuid | 后端用户 ID |
| wx_openid | varchar | 微信 openid，唯一 |
| wx_unionid | varchar nullable | 微信 unionid，可选 |
| nickname | varchar nullable | 昵称，可选 |
| avatar_url | text nullable | 头像，可选 |
| created_at | timestamp | 创建时间 |
| updated_at | timestamp | 更新时间 |
| last_login_at | timestamp | 最近登录时间 |

关键约束：

- `wx_openid` 必须唯一。

---

### 7.2 cards 表

用途：保存英语卡片主数据。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | uuid | 后端卡片 ID |
| user_id | uuid | 所属用户 |
| legacy_cloud_id | varchar nullable | 原微信云数据库 `_id` |
| local_temp_id | varchar nullable | 本地弱网创建时生成的临时 ID |
| content | text | 英文内容 |
| content_normalized | text | 规范化后的英文内容，用于搜索和辅助去重 |
| card_type | varchar | word / phrase / sentence |
| exam_scene | varchar nullable | 考试场景 |
| exam_module | varchar nullable | 考试模块 |
| understanding | text nullable | 我的理解 |
| note | text nullable | 补充备注 |
| translation | text nullable | 翻译 |
| analysis_status | varchar | pending / done / failed |
| analysis_level | varchar | pass / warning / error |
| analysis_messages | jsonb | 英文检测提示 |
| understanding_source | varchar | local / machine / ai / user |
| review_count | int | 累计复习次数 |
| again_count | int | 没记住次数 |
| hard_count | int | 模糊次数 |
| good_count | int | 记住了次数 |
| easy_count | int | 太简单次数 |
| last_review_result | varchar nullable | 最近一次反馈结果 |
| last_reviewed_at | timestamp nullable | 最近复习时间 |
| next_review_at | timestamp nullable | 下次复习时间 |
| status | varchar | active / archived / deleted |
| created_at | timestamp | 创建时间 |
| updated_at | timestamp | 更新时间 |
| deleted_at | timestamp nullable | 删除时间 |

关键约束：

- 同一个用户下，`legacy_cloud_id` 应唯一，用于微信云数据库迁移；
- 同一个用户下，`local_temp_id` 应唯一，用于弱网本地卡片和后端正式卡片合并；
- 删除卡片采用软删除，不直接物理删除。

说明：

- `next_review_at` 用于判断卡片是否到期；
- `analysis_status` 用于首页展示“分析中 / 待重试”等状态；
- `待学习 / 待加强 / 已掌握` 不建议作为固定字段存入 cards 表，应由后端根据复习数据动态计算。

---

### 7.3 review_sessions 表

用途：保存一次复习会话。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | uuid | 会话 ID |
| user_id | uuid | 用户 ID |
| review_date | date | 用户本地日期 |
| timezone | varchar | 用户本地时区，例如 Asia/Tokyo |
| status | varchar | active / completed / abandoned |
| total_count | int | 本轮总卡片数 |
| completed_count | int | 已完成数量 |
| current_index | int | 当前复习位置 |
| created_at | timestamp | 创建时间 |
| updated_at | timestamp | 更新时间 |
| completed_at | timestamp nullable | 完成时间 |

说明：

- 用户当天进入复习页时，后端优先恢复未完成的 active session；
- 如果没有 active session，再生成新的今日复习任务；
- 所有 item 完成后，session 状态改为 completed；
- `timezone` 用于避免 UTC 日期和用户本地日期不一致导致今日任务错乱。

---

### 7.4 review_session_items 表

用途：保存一次复习会话中的每一张卡片。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | uuid | 明细 ID |
| session_id | uuid | 复习会话 ID |
| card_id | uuid | 卡片 ID |
| position | int | 本轮中的位置 |
| status | varchar | pending / done / skipped |
| is_repeat | boolean | 是否为本轮内追加复习项 |
| repeat_count | int | 本轮内重复出现次数 |
| first_result | varchar nullable | 第一次反馈 |
| final_result | varchar nullable | 最终反馈 |
| created_at | timestamp | 创建时间 |
| updated_at | timestamp | 更新时间 |

说明：

- 用于支持“没记住 / 模糊”后在本轮末尾再次出现；
- `status` 表示当前 item 是否完成；
- `is_repeat` 表示该 item 是否是追加出来的复习项；
- `repeat_count` 用于控制同一张卡片在本轮内最多重复出现次数；
- 该表用于生成今日复习总结和今日复习内容。

---

### 7.5 review_records 表

用途：保存用户每一次复习反馈。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | uuid | 记录 ID |
| user_id | uuid | 用户 ID |
| card_id | uuid | 卡片 ID |
| session_id | uuid nullable | 复习会话 ID |
| session_item_id | uuid nullable | 会话明细 ID |
| result | varchar | again / hard / good / easy |
| result_label | varchar | 没记住 / 模糊 / 记住了 / 太简单 |
| reviewed_at | timestamp | 复习时间 |
| review_date | date | 用户本地日期 |
| before_next_review_at | timestamp nullable | 反馈前下次复习时间 |
| after_next_review_at | timestamp nullable | 反馈后下次复习时间 |
| before_review_count | int | 反馈前复习次数 |
| after_review_count | int | 反馈后复习次数 |
| client_record_id | varchar | 前端生成的唯一记录 ID |
| source | varchar | miniapp / retry / migration |
| created_at | timestamp | 创建时间 |

关键约束：

- 同一个用户下，`client_record_id` 必须唯一；
- 同一个 `client_record_id` 重复提交时，后端不能重复插入记录，也不能重复更新卡片统计。

说明：

- `client_record_id` 是弱网重试场景下保证幂等的核心字段；
- 用户点一次反馈，只能生成一条有效复习记录。

---

## 8. API 设计

第一阶段不需要实现所有接口，但需要先明确接口边界。

### 8.1 用户登录

```http
POST /api/auth/wechat-login
```

用途：

- 小程序通过 `wx.login()` 获取 code；
- 后端用 code 换取 openid；
- 后端返回 `user_id` 和 token。

请求示例：

```json
{
  "code": "wechat_login_code"
}
```

返回示例：

```json
{
  "user_id": "uuid",
  "token": "jwt_token",
  "is_new_user": false
}
```

---

### 8.2 获取卡片列表

```http
GET /api/cards
```

查询参数：

```text
filter=all | due | weak | mastered
keyword=xxx
limit=20
offset=0
```

用途：

- 首页展示；
- 搜索；
- 筛选；
- 后续替代微信云数据库查询。

说明：

- `due` 对应前端“待学习”；
- `weak` 对应前端“待加强”；
- `mastered` 对应前端“已掌握”；
- 这些状态建议由后端动态计算，而不是固定存入 cards 表。

---

### 8.3 创建卡片

```http
POST /api/cards
```

用途：

- 新增卡片；
- 保存英文内容；
- 初始化复习状态；
- 创建英文分析任务或触发英文分析流程。

请求示例：

```json
{
  "client_card_id": "local_20260506_xxxxx",
  "content": "look forward to",
  "card_type": "phrase",
  "exam_scene": "考研",
  "exam_module": "阅读",
  "understanding": "期待；盼望",
  "note": "to 是介词，后面接名词或动名词"
}
```

返回示例：

```json
{
  "card": {
    "id": "uuid",
    "local_temp_id": "local_20260506_xxxxx",
    "content": "look forward to",
    "analysis_status": "pending",
    "review_count": 0,
    "created_at": "2026-05-06T12:00:00+09:00"
  }
}
```

---

### 8.4 修改卡片

```http
PATCH /api/cards/{card_id}
```

用途：

- 修改英文内容；
- 修改“我的理解”；
- 修改补充备注；
- 修改卡片标签信息。

说明：

- 如果只修改 `understanding` 或 `note`，不需要重新分析英文；
- 如果修改 `content`，需要重新分析英文，并将 `analysis_status` 置为 `pending`。

---

### 8.5 删除卡片

```http
DELETE /api/cards/{card_id}
```

用途：

- 删除卡片。

说明：

- 后端执行软删除；
- 不直接物理删除数据；
- 设置 `status = deleted` 和 `deleted_at = now()`。

---

### 8.6 获取今日复习任务

```http
GET /api/review/today
```

查询参数：

```text
date=2026-05-06
timezone=Asia/Tokyo
```

用途：

- 后端根据复习规则生成今日任务；
- 如果当天已有未完成 session，则恢复 session；
- 如果没有 session，则创建新的 session。

返回内容应包括：

- session_id；
- review_date；
- total_count；
- completed_count；
- current_index；
- 当前复习卡片列表或下一张卡片。

---

### 8.7 提交复习反馈

```http
POST /api/review/feedback
```

请求示例：

```json
{
  "client_record_id": "review_20260506_card123_xxxxx",
  "session_id": "uuid",
  "session_item_id": "uuid",
  "card_id": "uuid",
  "result": "again",
  "reviewed_at": "2026-05-06T12:30:00+09:00",
  "timezone": "Asia/Tokyo"
}
```

用途：

- 保存复习记录；
- 更新卡片复习统计；
- 计算下一次复习时间；
- 更新当前 session item；
- 判断是否需要把“没记住 / 模糊”的卡片追加到本轮末尾；
- 返回下一张卡片或完成状态。

幂等要求：

- 同一个用户下，同一个 `client_record_id` 重复提交时，不能重复新增记录；
- 不能重复增加 `review_count`；
- 应返回已有处理结果或等价结果。

---

### 8.8 获取复习总结

```http
GET /api/review/sessions/{session_id}/summary
```

用途：

- 今日复习完成页；
- 今日复习内容页。

返回内容应包括：

- 本轮复习总数；
- 没记住数量；
- 模糊数量；
- 记住了数量；
- 太简单数量；
- 本轮复习过的卡片列表；
- 每张卡片的最终反馈结果。

---

### 8.9 获取历史复习内容

```http
GET /api/review/history
```

查询参数：

```text
start_date=2026-04-30
end_date=2026-05-06
result=again | hard | good | easy
keyword=xxx
limit=20
offset=0
```

用途：

- 历史复习内容页；
- 根据日期、反馈结果、关键词筛选复习记录。

---

### 8.10 过渡接口：预览今日复习任务

```http
POST /api/review/preview-today
```

用途：

在第一阶段，如果后端还没有正式接管 cards 表，可以先让小程序把当前卡片列表传给后端，由后端只负责计算今日任务。

请求示例：

```json
{
  "date": "2026-05-06",
  "timezone": "Asia/Tokyo",
  "cards": [
    {
      "id": "wechat_cloud_id_or_local_id",
      "content": "look forward to",
      "review_count": 3,
      "again_count": 1,
      "hard_count": 1,
      "last_review_result": "hard",
      "last_reviewed_at": "2026-05-05T20:00:00+09:00",
      "next_review_at": "2026-05-06T00:00:00+09:00"
    }
  ]
}
```

返回示例：

```json
{
  "task_card_ids": ["card_id_1", "card_id_2"],
  "reason_map": {
    "card_id_1": "due",
    "card_id_2": "weak"
  }
}
```

说明：

- 这个接口适合第一阶段低风险验证复习规则；
- 它不要求后端马上成为卡片主数据源；
- 等 cards 表接管后，可以逐步减少对该接口的依赖。

---

## 9. 复习规则后端化设计

后端需要统一处理今日复习任务生成逻辑。

### 9.1 第一版任务来源

今日复习任务主要来自：

1. 新卡；
2. 到期卡；
3. 待加强卡。

其中：

- 新卡：`review_count = 0`；
- 到期卡：`next_review_at <= 当前用户本地日期/时间`；
- 待加强卡：最近一次结果为 `again` 或 `hard`，或者历史 `again_count / hard_count` 较高。

### 9.2 首页筛选状态

前端展示的状态与后端计算逻辑对应关系如下：

| 前端文案 | 后端含义 |
|---|---|
| 全部 | 所有 active 卡片 |
| 待学习 | 新卡 + 到期卡 |
| 待加强 | 最近没记住/模糊，或历史薄弱倾向明显 |
| 已掌握 | 已复习过、未到期、近期表现较好，且不属于待加强 |

说明：

- 这些状态不建议固定写入 cards 表；
- 每次查询时由后端根据复习数据动态计算。

### 9.3 本轮内重复规则

第一版规则：

1. 用户点击 `again` 后，该卡片可以追加到本轮末尾；
2. 用户点击 `hard` 后，该卡片也可以追加到本轮末尾；
3. 同一张卡片在本轮内最多重复有限次数，避免死循环；
4. `good` 和 `easy` 不追加到本轮末尾；
5. session 内所有 item 完成后，session 状态改为 `completed`。

### 9.4 中途退出恢复

用户中途退出复习页后，再次进入时：

1. 后端优先查询当天是否存在 `active` session；
2. 如果存在，则恢复该 session；
3. 如果不存在，则生成新的今日复习任务；
4. 如果当天 session 已完成，则返回完成状态或无任务状态。

---

## 10. 弱网与 pending 队列设计

即使后端化，小程序仍然需要保留本地缓存和 pending 队列。

原因：

- 用户可能在弱网情况下添加卡片；
- 用户可能在弱网情况下复习；
- 用户不能因为网络问题丢失学习记录；
- 后端服务短暂不可用时，小程序也不能完全不可用。

### 10.1 新增卡片 pending

弱网时，小程序先生成本地卡片：

```text
local_temp_id = local_xxx
sync_status = pending
analysis_status = pending
```

网络恢复后，小程序同步到后端。

后端返回正式 `card_id` 后，小程序建立映射：

```text
local_temp_id -> backend_card_id
```

这样可以避免同一张本地卡片同步后变成两张卡片。

### 10.2 复习反馈 pending

用户点击反馈时，前端生成：

```text
client_record_id = review_xxx
```

弱网时，先保存在本地 pending 队列。

网络恢复后提交到：

```http
POST /api/review/feedback
```

后端根据 `client_record_id` 保证幂等，避免重复记录。

---

## 11. 微信云数据库迁移策略

迁移时不能直接删除微信云数据库数据。

### 11.1 迁移步骤

1. 先只读微信云数据库；
2. 批量导入后端数据库；
3. 每条卡片保存 `legacy_cloud_id`；
4. 使用 `user_id + legacy_cloud_id` 避免重复导入；
5. 小程序灰度切换到后端数据；
6. 确认稳定后，微信云数据库降级为备份；
7. 最后再决定是否停用微信云数据库。

### 11.2 迁移检查项

迁移时必须检查：

- 卡片数量是否一致；
- 删除状态是否一致；
- 复习次数是否一致；
- 最近复习状态是否一致；
- `next_review_at` 是否一致；
- 是否出现重复卡片；
- 首页筛选结果是否基本一致；
- 今日复习任务是否没有明显异常。

---

## 12. 风险清单与应对策略

### 12.1 数据重复

风险：

- 本地卡片、微信云卡片、后端卡片同时存在；
- 同一张卡片被同步多次。

应对：

- 使用 `legacy_cloud_id`；
- 使用 `local_temp_id`；
- 使用内容规范化辅助去重；
- 后端接口保证幂等。

### 12.2 复习记录重复

风险：

- 弱网重试导致同一次反馈提交多次。

应对：

- 前端生成 `client_record_id`；
- 后端对 `user_id + client_record_id` 加唯一约束；
- 重复请求返回已有结果，不重复更新统计。

### 12.3 今日复习状态错乱

风险：

- 用户退出后再次进入复习页，任务重新生成；
- 已完成卡片再次出现；
- 没记住/模糊重复过多。

应对：

- 使用 `review_sessions`；
- 使用 `review_session_items`；
- active session 优先恢复；
- 限制本轮内 `repeat_count`。

### 12.4 弱网不可用

风险：

- 用户无法保存卡片；
- 用户无法提交复习反馈；
- 用户无法继续复习。

应对：

- 保留本地缓存；
- 保留 pending 队列；
- 后端恢复后自动同步。

### 12.5 一次性迁移风险过高

风险：

- 改动范围太大；
- 上线小程序出现严重问题。

应对：

- 分阶段迁移；
- 每阶段只替换一小块逻辑；
- 每阶段保留回退方案。

---

## 13. 第一阶段开发任务

第一阶段只做“复习规则后端化原型”，暂时不全量迁移卡片数据。

### 13.1 第一阶段目标

第一阶段要验证的是：

- 后端能否正确生成今日复习任务；
- 后端能否正确处理复习反馈；
- 后端能否维护复习 session；
- 后端能否避免重复任务和重复反馈。

### 13.2 第一阶段建议实现的接口

优先实现：

```http
POST /api/review/preview-today
GET /api/review/today
POST /api/review/feedback
GET /api/review/sessions/{session_id}/summary
```

其中，`POST /api/review/preview-today` 可以作为过渡接口，先让小程序把当前卡片列表传给后端，由后端只负责返回今日任务排序结果。

### 13.3 第一阶段建议新增的后端模块

建议在 Python 后端中逐步新增：

```text
app/models/          数据库模型
app/schemas/         Pydantic 请求/响应结构
app/services/review_service.py
app/routers/review.py
```

第一阶段可以先实现 review 相关 service，不急着重构英文分析接口。

### 13.4 当前暂不改动的内容

第一阶段暂不改动：

- 不重写小程序首页；
- 不重写添加卡片页；
- 不删除微信云数据库；
- 不迁移全部历史卡片；
- 不一次性替换 recordStorage.js 中所有逻辑。

---

## 14. 下一步

当前文档确认后，下一步再进入数据库和代码设计。

建议顺序：

1. 确认本设计文档；
2. 补充更具体的数据库 schema；
3. 设计 SQLAlchemy models；
4. 设计 Alembic 迁移脚本；
5. 先实现 review service；
6. 再实现 review router；
7. 最后小范围接入小程序复习页。

在这份文档确认之前，不建议直接修改小程序业务代码。
