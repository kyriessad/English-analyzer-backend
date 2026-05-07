# 英语学习小程序后端主导重构 Phase 0 架构设计文档

## 1. 文档目标

本阶段目标不是立刻修改代码，而是先确定后端主导重构的整体架构、数据库模型、API 契约和关键边界规则。

本次重构的核心目标是：

> 将英语学习小程序从“前端本地 Storage + 微信云数据库 + 云函数”的混合架构，重构为“微信小程序客户端 + FastAPI 后端 + PostgreSQL 数据库”的标准后端主导架构。

Phase 0 的产出包括：

1. 系统事实来源原则。
2. Auth 登录鉴权流程。
3. 数据库表结构草案。
4. API 契约草案。
5. 复习队列与 Session 机制。
6. 时区处理规则。
7. Token 过期与静默刷新规则。
8. 本地缓存与 Action Queue 的边界。
9. 后续 Phase 1–5 的执行顺序。

---

## 2. 总体架构原则

### 2.1 唯一事实来源

重构后，系统的唯一事实来源为：

```text
PostgreSQL 数据库
```

也就是说，所有正式数据均以后端数据库为准，包括：

- 用户信息；
- 卡片数据；
- 卡片熟练度；
- 下次复习时间；
- 复习记录；
- 今日复习队列；
- 历史复习统计。

小程序本地 Storage 不再作为事实来源，只作为：

```text
缓存 + Token 存储 + 弱网动作队列
```

### 2.2 前后端职责边界

| 模块 | 职责 |
|---|---|
| 微信小程序 | 页面展示、用户输入、调用 API、本地缓存、弱网动作缓存 |
| FastAPI 后端 | 登录鉴权、卡片 CRUD、英文分析、复习规则、Session 调度、数据写入 |
| PostgreSQL | 存储所有正式数据 |
| 微信云数据库 | 退出主链路，未来仅作为可选历史迁移来源 |
| 微信云函数 | 最终退出主链路，后续不再作为核心服务层 |

### 2.3 前端不再承担的职责

重构后，前端不再负责：

```text
1. 计算今天复习哪些卡片；
2. 计算 next_review_at；
3. 判断 mastery_level 如何变化；
4. 判断 mastery_state 是待学习、待加强还是已掌握；
5. 处理本轮“没记住”的重现逻辑；
6. 合并本地 Storage 与微信云数据库；
7. 处理 updatedAt 冲突；
8. 处理 pending_delete 墓碑同步。
```

这些逻辑全部迁移到 FastAPI 后端。

---

## 3. Auth 登录鉴权设计

### 3.1 Auth 的作用

Auth 用来解决两个问题：

```text
1. 当前请求是谁发来的？
2. 这个用户是否有权限访问这些数据？
```

在新架构下，FastAPI 后端必须通过登录鉴权机制识别用户。

### 3.2 登录流程

登录流程如下：

```text
小程序调用 wx.login()
↓
获取临时 code
↓
POST /api/auth/wechat-login
↓
FastAPI 调微信接口，用 code 换取 openid
↓
后端根据 openid 查询或创建 users 记录
↓
后端生成 JWT access_token
↓
小程序保存 access_token
↓
后续请求携带 Authorization: Bearer <token>
```

### 3.3 Auth API

#### POST /api/auth/wechat-login

用途：微信登录，换取后端 token。

请求示例：

```json
{
  "code": "wx_login_code",
  "timezone": "Asia/Shanghai",
  "device_id": "optional_device_id"
}
```

返回示例：

```json
{
  "access_token": "jwt_token",
  "token_type": "bearer",
  "user": {
    "id": "user_uuid",
    "timezone": "Asia/Shanghai"
  }
}
```

说明：

- `code` 来自 `wx.login()`。
- `timezone` 由前端传入，用于后端判断用户本地日期。
- 后端不向前端暴露微信 `openid`。

#### GET /api/auth/me

用途：获取当前登录用户信息。

请求 Header：

```http
Authorization: Bearer <access_token>
```

返回示例：

```json
{
  "id": "user_uuid",
  "timezone": "Asia/Shanghai",
  "created_at": "2026-05-07T10:00:00Z"
}
```

---

## 4. Token 过期与 401 静默刷新

### 4.1 问题

JWT Token 有有效期。用户如果几天后重新打开小程序，旧 Token 可能已经过期。此时后端会返回：

```http
401 Unauthorized
```

如果直接让用户重新登录，体验较差。

### 4.2 处理规则

前端 `apiClient.js` 需要封装请求拦截逻辑：

```text
普通 API 请求
↓
如果返回 401
↓
前端自动调用 wx.login()
↓
重新请求 POST /api/auth/wechat-login
↓
拿到新 access_token
↓
自动重放刚才失败的请求
```

### 4.3 并发刷新锁

如果多个请求同时返回 401，只允许触发一次 token 刷新。

规则：

```text
1. 第一个 401 请求触发 refreshPromise；
2. 其他 401 请求等待同一个 refreshPromise；
3. refresh 成功后，所有失败请求重放；
4. refresh 失败后，清理本地 token，并提示用户重新进入小程序。
```

---

## 5. 时区设计

### 5.1 核心原则

数据库中不存本地日期作为核心调度依据，而是存 UTC 时间戳。

推荐字段：

```text
cards.next_review_at TIMESTAMPTZ
cards.last_reviewed_at TIMESTAMPTZ
```

后端在查询“今天要复习什么”时，根据用户时区动态计算本地日期边界。

### 5.2 查询今日任务的时区逻辑

后端处理 `GET /api/reviews/today` 时：

```text
1. 从 JWT 中解析 user_id；
2. 获取用户 timezone；
3. 计算用户本地今天 00:00；
4. 计算用户本地明天 00:00；
5. 将两个时间转换为 UTC；
6. 查询 next_review_at < 用户本地明天 00:00 对应的 UTC 时间。
```

### 5.3 Timezone 来源优先级

```text
1. 请求 Header: X-Timezone
2. users.timezone
3. 默认 Asia/Shanghai
```

请求示例：

```http
X-Timezone: Asia/Shanghai
```

---

## 6. 数据库表结构草案

### 6.1 users 表

用途：保存用户身份和默认设置。

建议字段：

```text
id UUID PRIMARY KEY
wechat_openid TEXT UNIQUE NOT NULL
timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
last_login_at TIMESTAMPTZ
```

说明：

- `wechat_openid` 只保存在后端。
- 前端只使用后端生成的 `user.id` 和 token。

---

### 6.2 cards 表

用途：保存卡片当前快照。

建议字段：

```text
id UUID PRIMARY KEY
user_id UUID NOT NULL REFERENCES users(id)

english_text TEXT NOT NULL
normalized_text TEXT
card_type TEXT
exam_scene TEXT
exam_module TEXT

understanding TEXT
note TEXT
translation TEXT

analysis_status TEXT
validation_level TEXT
validation_errors JSONB
validation_warnings JSONB
understanding_source TEXT

mastery_level INTEGER NOT NULL DEFAULT 0
mastery_state TEXT NOT NULL DEFAULT 'new'
review_count INTEGER NOT NULL DEFAULT 0
again_count INTEGER NOT NULL DEFAULT 0
hard_count INTEGER NOT NULL DEFAULT 0
good_count INTEGER NOT NULL DEFAULT 0
easy_count INTEGER NOT NULL DEFAULT 0

last_reviewed_at TIMESTAMPTZ
last_review_result TEXT
next_review_at TIMESTAMPTZ

created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
deleted_at TIMESTAMPTZ

legacy_cloud_id TEXT
local_temp_id TEXT
```

说明：

- `cards` 表保存当前状态，不保存完整历史。
- 完整历史由 `review_logs` 保存。
- `deleted_at` 用于软删除。
- `legacy_cloud_id` 用于未来迁移微信云数据。
- `local_temp_id` 用于迁移期或弱网去重。

---

### 6.3 review_logs 表

用途：记录每一次复习行为，是不可变历史。

建议字段：

```text
id UUID PRIMARY KEY
user_id UUID NOT NULL REFERENCES users(id)
card_id UUID NOT NULL REFERENCES cards(id)
session_id UUID REFERENCES review_sessions(id)

result TEXT NOT NULL
reviewed_at TIMESTAMPTZ NOT NULL

old_mastery_level INTEGER
new_mastery_level INTEGER
old_next_review_at TIMESTAMPTZ
new_next_review_at TIMESTAMPTZ

client_action_id TEXT
source TEXT
created_at TIMESTAMPTZ NOT NULL
```

说明：

- 每次用户点击反馈按钮，都写入一条 `review_logs`。
- 后续历史复习页面、统计图、遗忘曲线都基于该表。
- Phase 3 引入 Action Queue 后，`client_action_id` 用于幂等。

---

### 6.4 review_sessions 表

用途：记录用户某一天的一次复习会话。

建议字段：

```text
id UUID PRIMARY KEY
user_id UUID NOT NULL REFERENCES users(id)
review_date DATE NOT NULL
timezone TEXT NOT NULL

status TEXT NOT NULL DEFAULT 'active'
total_count INTEGER NOT NULL DEFAULT 0
completed_count INTEGER NOT NULL DEFAULT 0

created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
finished_at TIMESTAMPTZ
```

`status` 可选值：

```text
active
completed
abandoned
```

说明：

- `review_date` 是用户本地日期。
- 该表用于管理“今天这一轮复习”。

---

### 6.5 review_session_items 表

用途：保存某个复习 Session 中的具体队列。

建议字段：

```text
id UUID PRIMARY KEY
session_id UUID NOT NULL REFERENCES review_sessions(id)
card_id UUID NOT NULL REFERENCES cards(id)

position INTEGER NOT NULL
status TEXT NOT NULL DEFAULT 'pending'
result TEXT

origin_item_id UUID
is_reappeared BOOLEAN NOT NULL DEFAULT FALSE
reappear_count INTEGER NOT NULL DEFAULT 0

created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
completed_at TIMESTAMPTZ
```

`status` 可选值：

```text
pending
completed
abandoned
```

说明：

- 前端不再维护完整复习队列。
- 队列排序、插队、重现全部由该表控制。

---

## 7. Cards API 契约

### 7.1 GET /api/cards

用途：获取当前用户的卡片列表。

请求 Header：

```http
Authorization: Bearer <access_token>
```

可选查询参数：

```text
status
keyword
limit
offset
```

返回示例：

```json
{
  "items": [
    {
      "id": "card_uuid",
      "english_text": "look forward to",
      "normalized_text": "look forward to",
      "card_type": "phrase",
      "understanding": "期待；盼望",
      "note": "后面接名词或动名词",
      "mastery_state": "new",
      "review_count": 0,
      "next_review_at": "2026-05-07T00:00:00Z",
      "created_at": "2026-05-07T10:00:00Z",
      "updated_at": "2026-05-07T10:00:00Z"
    }
  ],
  "total": 1
}
```

---

### 7.2 POST /api/cards

用途：新增卡片。

请求示例：

```json
{
  "english_text": "look forward to",
  "card_type": "phrase",
  "exam_scene": "考研",
  "exam_module": "阅读",
  "understanding": "",
  "note": ""
}
```

后端职责：

```text
1. 校验英文内容；
2. 规范化文本；
3. 翻译；
4. 生成或补全“我的理解”；
5. 写入 cards 表；
6. 返回完整 card。
```

---

### 7.3 PATCH /api/cards/{card_id}

用途：编辑卡片。

请求示例：

```json
{
  "understanding": "期待；盼望",
  "note": "look forward to doing sth."
}
```

说明：

- 只更新传入字段。
- 后端必须校验 `card_id` 是否属于当前用户。

---

### 7.4 DELETE /api/cards/{card_id}

用途：软删除卡片。

后端行为：

```text
deleted_at = 当前 UTC 时间
```

说明：

- 默认查询不返回 `deleted_at IS NOT NULL` 的卡片。
- 不做物理删除，方便未来恢复和同步。

---

## 8. Review API 契约

### 8.1 GET /api/reviews/today?limit=5

用途：获取当前用户今日复习 Batch。

请求 Header：

```http
Authorization: Bearer <access_token>
X-Timezone: Asia/Shanghai
```

后端处理流程：

```text
1. 解析 user_id；
2. 解析 timezone；
3. 回收旧 active session；
4. 查询今天是否已有 active session；
5. 如果没有，则创建新 session；
6. 生成今日 review_session_items；
7. 返回 pending items 中 position 最靠前的 limit 张。
```

返回示例：

```json
{
  "session_id": "session_uuid",
  "review_date": "2026-05-07",
  "timezone": "Asia/Shanghai",
  "batch_size": 5,
  "total_count": 20,
  "completed_count": 3,
  "remaining_count": 17,
  "cards": [
    {
      "item_id": "item_uuid",
      "card_id": "card_uuid",
      "english_text": "look forward to",
      "understanding": "期待；盼望",
      "note": "后面接名词或动名词",
      "card_type": "phrase",
      "mastery_state": "new",
      "is_reappeared": false
    }
  ]
}
```

---

### 8.2 POST /api/reviews/feedback

用途：提交复习反馈。

请求示例：

```json
{
  "session_id": "session_uuid",
  "item_id": "item_uuid",
  "card_id": "card_uuid",
  "result": "again",
  "reviewed_at": "2026-05-07T10:30:00+08:00",
  "client_action_id": "optional_uuid"
}
```

`result` 可选值：

```text
again
hard
good
easy
```

后端职责：

```text
1. 校验 session、item、card 是否属于当前用户；
2. 标记当前 review_session_item 为 completed；
3. 写入 review_logs；
4. 更新 cards.mastery_level；
5. 更新 cards.mastery_state；
6. 更新 cards.next_review_at；
7. 如果 result = again，按规则新增重现 item；
8. 更新 review_sessions.completed_count；
9. 返回当前处理结果和剩余数量。
```

---

## 9. 复习规则设计

### 9.1 四种反馈

| 前端文案 | 后端 result | 含义 |
|---|---|---|
| 没记住 | again | 完全没想起来 |
| 模糊 | hard | 想起来了，但不顺畅 |
| 记住了 | good | 正常想起来 |
| 太简单 | easy | 非常熟悉 |

### 9.2 本轮重现规则

第一版只允许 `again` 触发本轮重现。

| result | 本轮是否重现 | 说明 |
|---|---:|---|
| again | 是 | 延迟 5 张后重现，最多 2 次 |
| hard | 否 | 只影响长期调度 |
| good | 否 | 只影响长期调度 |
| easy | 否 | 只影响长期调度 |

### 9.3 Again 插队规则

当用户点击 `again` 时，如果该卡片在当天未超过重现上限，则新增一条 `review_session_items`。

位置计算规则：

```text
new_position = min(current_position + 5, max_position_in_session + 1)
```

说明：

```text
1. 队列还长：延迟 5 张后出现；
2. 队列快结束：放到当前 Session 队尾；
3. 单卡当天最多重现 2 次；
4. 超过上限后不再新增 item。
```

### 9.4 长期调度规则

第一版可使用固定间隔表：

```text
[1, 2, 4, 7, 15, 30, 60]
```

后端根据用户反馈调整 `mastery_level`，并据此计算新的 `next_review_at`。

示意规则：

```text
again: mastery_level 降低或重置，next_review_at = 明天
hard: mastery_level 小幅降低或不变，next_review_at = 较短间隔
good: mastery_level + 1，next_review_at = 对应间隔
easy: mastery_level + 2，next_review_at = 更长间隔
```

具体数值可在 Phase 2 实现前再次确认。

---

## 10. Session 烂尾回收机制

### 10.1 问题

用户可能当天只复习了一部分，然后退出小程序。第二天再次进入时，旧 session 仍为 `active`。

### 10.2 处理规则

每次调用：

```http
GET /api/reviews/today?limit=5
```

后端先执行：

```text
1. 计算用户本地 today；
2. 查找该用户 active 且 review_date < today 的 session；
3. 将旧 session 标记为 abandoned；
4. 将未完成 session_items 标记为 abandoned；
5. 不更新未完成卡片的 next_review_at；
6. 为今天生成或恢复新的 active session。
```

---

## 11. 本地 Storage 边界

重构后，本地 Storage 只保存：

```text
access_token
user_info
cardsCache
reviewQueueCache
actionQueue
lastSyncTime
```

不保存最终事实。

### 11.1 cardsCache

用途：

```text
1. 首页快速展示；
2. 后端短暂不可用时显示上一次缓存；
3. 不参与最终数据裁决。
```

### 11.2 reviewQueueCache

用途：

```text
1. 临时保存当前 batch；
2. 页面切换或短暂重载时恢复 UI；
3. 不负责完整复习调度。
```

### 11.3 actionQueue

用途：

```text
弱网或断网时保存用户动作，恢复网络后补发给后端。
```

Action Queue 在 Phase 3 实现，不在 Phase 1/2 抢先做。

---

## 12. Action Queue 与幂等设计

### 12.1 Action Queue 的作用

Action Queue 用于弱网防损。

用户点击反馈后，前端可以先记录动作，再后台发送：

```json
{
  "client_action_id": "uuid",
  "action_type": "review_feedback",
  "payload": {
    "session_id": "session_uuid",
    "item_id": "item_uuid",
    "card_id": "card_uuid",
    "result": "again",
    "reviewed_at": "2026-05-07T10:30:00+08:00"
  },
  "created_at": "2026-05-07T10:30:00+08:00"
}
```

### 12.2 幂等规则

幂等的含义是：

```text
同一个动作重复提交多次，只生效一次。
```

后端通过 `client_action_id` 判断是否已经处理过。

规则：

```text
1. 第一次收到 client_action_id：正常处理；
2. 再次收到相同 client_action_id：不重复写 review_logs，不重复更新 cards，不重复新增 reappeared item；
3. 返回第一次处理的结果或当前状态。
```

---

## 13. 微信云退出策略

### 13.1 当前策略

当前按“第一次正式开发”处理，不优先迁移微信开发者工具或微信云里的旧数据。

因此：

```text
Phase 1–4 不做历史迁移。
```

### 13.2 Phase 5 可选迁移

如果后续需要保留旧数据，再写 Python 离线迁移脚本。

迁移来源：

```text
微信云数据库 englishKnowledgeCards
微信云数据库 reviewRecords
```

迁移目标：

```text
PostgreSQL cards
PostgreSQL review_logs
```

去重依据：

```text
legacy_cloud_id
```

---

## 14. 后续实施阶段

### Phase 1：Auth + cards 主链路后端化

目标：

```text
1. 实现微信登录换 token；
2. 首页从 FastAPI 获取 cards；
3. 新增卡片写入 PostgreSQL；
4. 编辑卡片 PATCH 后端；
5. 删除卡片软删除；
6. 本地 Storage 只做 cardsCache。
```

完成标准：

```text
不依赖微信云数据库，小程序仍能完成卡片的增删改查。
```

---

### Phase 2：复习规则后端化

目标：

```text
1. 实现 GET /api/reviews/today?limit=5；
2. 实现 POST /api/reviews/feedback；
3. 后端生成 review_session 和 review_session_items；
4. 后端更新 mastery_level、mastery_state、next_review_at；
5. 后端写 review_logs；
6. 后端处理 again 本轮重现。
```

完成标准：

```text
前端 review.js 不再计算下一次复习时间，不再生成今日任务队列。
```

---

### Phase 3：Action Queue + 幂等 + 弱网体验

目标：

```text
1. 前端引入 actionQueue；
2. 用户反馈后 UI 乐观更新；
3. 后台补发动作；
4. 后端通过 client_action_id 保证幂等；
5. 网络恢复后自动同步。
```

完成标准：

```text
弱网或短暂断网时，用户复习操作不丢失，也不会重复生效。
```

---

### Phase 4：前端大扫除

目标：

```text
1. 删除或瘦身 recordStorage.js 中的复杂业务逻辑；
2. 删除 syncLocalCacheWithCloud；
3. 删除本地与微信云的复杂双向合并；
4. 保留极简 localCache.js；
5. 前端只负责展示、请求、缓存。
```

---

### Phase 5：历史数据迁移，微信云彻底退出

目标：

```text
1. 如有需要，迁移旧微信云数据到 PostgreSQL；
2. 删除 cloudfunctions；
3. 停止使用微信云数据库主链路；
4. 系统完全切换到 FastAPI + PostgreSQL。
```

---

## 15. Phase 0 最终结论

本次重构的最终方向为：

```text
微信小程序只做客户端。
FastAPI 作为唯一业务后端。
PostgreSQL 作为唯一事实来源。
本地 Storage 只做缓存与弱网动作队列。
微信云数据库与云函数最终退出主链路。
```

执行顺序为：

```text
Phase 0：架构设计文档 + 数据库表结构 + API 契约
↓
Phase 1：Auth + cards 主链路后端化
↓
Phase 2：review session + feedback + 复习规则后端化
↓
Phase 3：Action Queue + 幂等 + 弱网体验
↓
Phase 4：前端大扫除，recordStorage.js 瘦身
↓
Phase 5：历史数据迁移，微信云彻底退出
```

当前阶段只完成架构定稿，不直接修改代码。

