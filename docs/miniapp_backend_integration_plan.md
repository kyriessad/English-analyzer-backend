# 小程序接入后端与数据迁移方案

> 适用范围：微信小程序与 `English-analyzer-backend` 后端逐步接入  
> 当前阶段：只制定方案，不改小程序代码，不迁移真实数据  
> 核心原则：小步接入、可回退、弱网可用、避免重复数据

---

## 1. 当前状态

当前小程序仍然主要依赖：

- 小程序本地缓存；
- 本地复习状态和 pending 队列；
- 微信云数据库；
- 微信云函数中转部分请求；
- 本地 `recordStorage.js` 中的复习规则和记录维护逻辑。

Python FastAPI 后端目前已经具备以下能力：

- 微信小程序静默登录接口：`POST /api/auth/wechat-login`；
- JWT access_token 生成与解析；
- cards CRUD API；
- review preview API；
- 正式 review session API；
- review feedback 幂等写入能力；
- 基于 SQLAlchemy models 的数据库结构；
- Alembic 迁移配置和首版建表脚本。

但当前小程序还没有接入后端的 cards / review API：

- 首页卡片列表仍然使用旧数据来源；
- 新增、编辑、删除卡片仍然走旧逻辑；
- 今日复习和复习记录仍然主要由小程序本地逻辑维护；
- 后端数据库还没有成为 cards 和 review_records 的主数据源。

---

## 2. 接入目标

接入目标不是一次性替换全部旧逻辑，而是让后端逐步接管身份、卡片、复习记录和复习规则。

目标状态：

- 小程序启动或用户进入关键页面时，通过 `wx.login()` 获取 code；
- 小程序调用后端 `POST /api/auth/wechat-login`；
- 后端返回 `user_id` 和 `access_token`；
- 小程序后续 cards / review 请求优先携带：

```http
Authorization: Bearer <access_token>
```

- 后端通过 token 识别当前用户；
- 后端数据库逐步成为 cards 和 review_records 的主数据源；
- 小程序本地继续保留缓存和 pending 队列，保证弱网可用；
- 微信云数据库在迁移期继续保留，作为备份和回退来源。

---

## 3. 分阶段接入计划

### 阶段 A：只接入登录

目标：

- 小程序调用 `wx.login()`；
- 将 code 发送给后端 `POST /api/auth/wechat-login`；
- 后端换取 openid，创建或查找 users 表记录；
- 小程序保存 `user_id` 和 `access_token`。

本阶段不改：

- 首页卡片数据来源；
- 添加卡片流程；
- 复习流程；
- 微信云数据库读写逻辑。

验收点：

- 首次登录能创建后端 user；
- 再次登录返回同一个 user；
- 小程序本地能保存 token；
- 后端异常时，小程序仍可按旧逻辑运行。

### 阶段 B：新增卡片同时写后端，但首页仍用旧数据

目标：

- 用户新增卡片时，继续走旧本地/微信云保存逻辑；
- 同时调用后端 `POST /api/cards` 写入 cards 表；
- 请求携带 token；
- 后端保存 `local_temp_id`；
- 如果卡片已经有微信云 `_id`，保存到 `legacy_cloud_id`。

本阶段首页仍然读取旧数据，不直接切换为后端列表。

验收点：

- 新增卡片后，后端 cards 表能看到对应记录；
- 弱网时，先写本地 pending，网络恢复后补同步；
- 同一张本地卡片重复同步不会变成多张后端卡片。

### 阶段 C：卡片列表从后端读取，微信云数据库作为备份

目标：

- 首页卡片列表改为优先调用 `GET /api/cards`；
- 请求携带 token；
- 支持 keyword / limit / offset；
- 微信云数据库保留为备份和回退来源；
- 本地缓存仍然保留，用于弱网展示。

验收点：

- 后端列表数量与旧数据基本一致；
- 搜索结果符合预期；
- 删除状态不出现在默认 active 列表；
- 后端不可用时，小程序可以回退到本地缓存或微信云数据。

### 阶段 D：复习记录写后端

目标：

- 用户点击 again / hard / good / easy 后，调用 `POST /api/review/feedback`；
- 每条复习反馈带 `client_record_id`；
- 后端写入 review_records；
- 后端更新 cards.review_count、last_review_result、next_review_at 等统计字段；
- 小程序本地仍保留复习记录和 pending 队列。

本阶段可以先不替换今日复习任务生成逻辑。

验收点：

- 后端能保存 review_records；
- 同一个 `client_record_id` 重复提交不会重复增加 review_count；
- 弱网恢复后 pending 记录能安全补提交；
- 今日总结可先继续使用小程序旧逻辑。

### 阶段 E：复习规则切到后端 review API

目标：

- 今日复习入口调用 `GET /api/review/today`；
- 后端恢复 active session 或创建新的 review_session；
- 用户反馈调用 `POST /api/review/feedback`；
- 复习完成页调用 `GET /api/review/sessions/{session_id}/summary`；
- 小程序本地只负责展示、缓存和 pending 队列。

验收点：

- 中途退出后再次进入，能恢复 active session；
- again / hard 能追加本轮 repeat item；
- session 完成后状态变为 completed；
- 今日总结数据能由后端返回。

### 阶段 F：微信云数据库降级为备份或停止使用

目标：

- 确认后端 cards、review_records、review_sessions 稳定；
- 小程序主要读写后端；
- 微信云数据库只作为迁移期备份；
- 稳定运行一段时间后，再决定是否停止写入微信云数据库。

注意：

- 不直接删除微信云数据库旧数据；
- 不做不可逆切换；
- 停用前需要完整的数据核对和回退预案。

---

## 4. 数据迁移策略

### 4.1 迁移对象

第一批迁移对象建议为微信云数据库中的：

```text
englishKnowledgeCards
```

目标表：

```text
cards
```

### 4.2 字段映射建议

| 微信云字段 | 后端 cards 字段 | 说明 |
|---|---|---|
| `_id` | `legacy_cloud_id` | 用于避免重复迁移 |
| 英文内容字段 | `content` | 原始英文内容 |
| 规范化内容 | `content_normalized` | 后端生成或迁移脚本生成 |
| 卡片类型 | `card_type` | word / phrase / sentence |
| 考试场景 | `exam_scene` | 可为空 |
| 考试模块 | `exam_module` | 可为空 |
| 我的理解 | `understanding` | 可为空 |
| 备注 | `note` | 可为空 |
| 翻译 | `translation` | 可为空 |
| 分析状态 | `analysis_status` | pending / done / failed |
| 复习次数 | `review_count` | 迁移旧统计 |
| again 次数 | `again_count` | 迁移旧统计 |
| hard 次数 | `hard_count` | 迁移旧统计 |
| good 次数 | `good_count` | 迁移旧统计 |
| easy 次数 | `easy_count` | 迁移旧统计 |
| 最近结果 | `last_review_result` | again / hard / good / easy |
| 最近复习时间 | `last_reviewed_at` | 注意时区 |
| 下次复习时间 | `next_review_at` | 注意时区 |

### 4.3 使用 legacy_cloud_id 避免重复迁移

后端 cards 表已保留：

```text
legacy_cloud_id
```

迁移时必须使用：

```text
user_id + legacy_cloud_id
```

作为去重依据。

规则：

- 如果后端已存在相同 `user_id + legacy_cloud_id`，不重复插入；
- 可以选择跳过，也可以做字段补齐更新；
- 不允许同一个微信云卡片迁移成多张后端卡片。

### 4.4 处理 local_temp_id

`local_temp_id` 用于弱网新增卡片。

处理规则：

- 弱网本地创建的卡片先生成 `local_temp_id`；
- 同步到后端时写入 `cards.local_temp_id`；
- 后端使用 `user_id + local_temp_id` 防止重复创建；
- 如果后续该卡片也同步到了微信云数据库，再拿到云端 `_id`，可以补写 `legacy_cloud_id`；
- 迁移脚本不应覆盖已有 `local_temp_id` 映射。

### 4.5 迁移前后校验

迁移前记录：

- 微信云 cards 总数；
- 按用户分组的 cards 数量；
- 删除/归档状态数量；
- 有效 active 卡片数量；
- review_count 非 0 的卡片数量；
- next_review_at 为空和非空数量。

迁移后校验：

- 后端 cards 总数是否一致；
- 每个用户的 cards 数量是否一致；
- `legacy_cloud_id` 是否完整；
- 是否存在重复 `legacy_cloud_id`；
- `review_count` 是否一致；
- `last_review_result` 是否一致；
- `next_review_at` 是否一致；
- 首页筛选结果是否基本一致。

建议先做 dry-run：

- 只读取微信云数据；
- 生成迁移预览报告；
- 不写入后端数据库；
- 人工检查报告后再执行真实导入。

---

## 5. 弱网策略

弱网策略不应被后端化删除。小程序仍然需要本地优先能力。

### 5.1 新增卡片

流程：

1. 用户新增卡片；
2. 小程序先写本地缓存；
3. 生成 `local_temp_id`；
4. 标记 `sync_status=pending`；
5. 网络可用时调用后端 `POST /api/cards`；
6. 后端返回正式 `card_id`；
7. 小程序保存：

```text
local_temp_id -> backend_card_id
```

避免重复：

- 后端用 `user_id + local_temp_id` 唯一约束；
- 小程序重试时带同一个 `local_temp_id`；
- 后端发现已存在则应返回已有卡片或等价结果。

### 5.2 复习反馈

流程：

1. 用户点击 again / hard / good / easy；
2. 小程序立即更新本地 UI；
3. 生成 `client_record_id`；
4. 本地写入 pending 队列；
5. 网络恢复后调用 `POST /api/review/feedback`；
6. 后端用 `user_id + client_record_id` 保证幂等。

避免重复：

- 同一条反馈重试必须使用同一个 `client_record_id`；
- 后端重复收到时不重复插入 review_records；
- 后端重复收到时不重复增加 cards.review_count；
- 小程序收到成功响应后再从 pending 队列移除。

### 5.3 本地缓存继续保留

本地缓存用途：

- 弱网首页展示；
- 弱网新增卡片；
- 弱网复习；
- 后端异常时临时回退；
- pending 队列可靠存储。

后端接入不应让小程序在弱网下完全不可用。

---

## 6. 回退方案

每个阶段都必须能回退。

### 6.1 后端登录异常

回退：

- 小程序继续使用旧本地/微信云逻辑；
- 不阻断现有功能；
- 可以延迟后端同步。

### 6.2 cards 写后端异常

回退：

- 本地保存成功即可；
- 微信云旧逻辑继续执行；
- 后端同步任务保留在 pending 队列；
- 后续网络恢复或后端恢复后重试。

### 6.3 cards 列表切后端后异常

回退：

- 使用本地缓存；
- 或临时切回微信云数据库列表；
- 不删除后端已同步数据；
- 不删除微信云旧数据。

### 6.4 review API 异常

回退：

- 本地继续复习；
- feedback 进入 pending 队列；
- 今日任务可暂时使用旧本地规则；
- 后端恢复后补提交 review_records。

### 6.5 数据迁移异常

回退：

- 停止迁移任务；
- 保留微信云数据库原始数据；
- 根据 `legacy_cloud_id` 清理或重跑后端导入；
- 不在未核对前切换小程序主数据源。

---

## 7. 风险清单

| 风险 | 表现 | 应对 |
|---|---|---|
| 用户重复卡片 | 同一张卡在后端出现多次 | 使用 `legacy_cloud_id`、`local_temp_id`、内容规范化辅助去重 |
| 卡片 ID 映射错误 | 本地卡片、云卡片、后端卡片对应不上 | 维护 `local_temp_id -> backend_card_id`、`legacy_cloud_id -> backend_card_id` 映射 |
| 复习记录重复 | 弱网重试导致 review_count 重复增加 | 使用 `client_record_id` 幂等约束 |
| 今日复习状态错乱 | 退出再进入生成新任务 | 后端优先恢复 active review_session |
| 弱网重复提交 | pending 队列多次提交同一操作 | 所有重试复用同一个业务 ID |
| 后端和本地数据不一致 | 首页、今日复习、历史记录显示不同 | 分阶段灰度、增加校验报告、保留回退路径 |
| 时区错误 | 今日任务跨天错乱 | 请求统一传 timezone，后端按用户本地日期计算 |
| 删除状态不一致 | 已删除卡片又出现在列表中 | 后端使用软删除，默认只返回 active |

---

## 8. 第一阶段真正要改的小程序文件清单

这里只列清单，不修改代码。

第一阶段只建议接入登录，不替换 cards/review 流程。

可能涉及文件：

- `app.js`
  - 启动时或合适时机调用登录；
  - 保存 `user_id` 和 `access_token`。

- `utils/request.js` 或新增 `apiClient`
  - 封装后端 base URL；
  - 自动附带 `Authorization: Bearer <access_token>`；
  - 统一处理 401、网络错误、重试。

- `recordStorage.js`
  - 后续阶段处理 pending 队列同步；
  - 第一阶段不建议大改。

- `pages/add/add.js`
  - 阶段 B 时新增卡片同步到后端；
  - 第一阶段不改。

- `pages/index/index.js`
  - 阶段 C 时从后端读取卡片列表；
  - 第一阶段不改。

- `pages/review/review.js`
  - 阶段 D/E 时接入后端 review API；
  - 第一阶段不改。

---

## 9. 下一步建议

下一步只做阶段 A：接入登录接口。

建议顺序：

1. 小程序新增后端 API client；
2. 在 `app.js` 中调用 `wx.login()`；
3. 调用 `POST /api/auth/wechat-login`；
4. 本地保存 `user_id`、`access_token`、过期时间或登录时间；
5. 后端异常时不阻断原功能；
6. 上线观察登录成功率和后端 users 表增长情况。

暂时不要做：

- 不直接替换首页卡片列表；
- 不直接替换复习流程；
- 不直接迁移全部历史卡片；
- 不删除微信云数据库旧数据；
- 不强制所有接口必须登录。

等登录稳定后，再进入阶段 B：新增卡片同时写后端。
