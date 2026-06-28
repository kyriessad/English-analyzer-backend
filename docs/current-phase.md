# Current Development Phase

## 2026-06-28 Local-Only AI Development Mode

**Status:** Active local development mode; former public production stack is offline.

Current facts:

- Tencent Cloud Lighthouse server: returned and destroyed.
- Production FastAPI/Nginx/PostgreSQL/systemd/Certbot stack: stopped and no longer exists.
- DNSPod `api.qingyacard.com` A record: deleted.
- `https://api.qingyacard.com`: invalid and not used by current code defaults.
- WeChat request legal domain: deleted or treated as unused.
- Tencent Cloud ICP filing subject: cancellation submitted.
- Public security network filing: no longer being handled.
- `qingyacard.com`: retained, not resolved, not used for the current app.
- Mini Program: remains paused, used only through WeChat DevTools for local development.

Current local backend:

- FastAPI: `http://127.0.0.1:8000`
- Database: SQLite `sqlite:///./english_analyzer.db`
- Translation provider: Argos Translate en -> zh
- Example generator: Ollama API `http://127.0.0.1:11434`, model `qwen3:8b`
- Startup command: `uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`

AI provider status:

- `app/providers/tencent_translator.py` remains as a legacy optional provider.
- `app/services/hunyuan_example.py` remains as a legacy optional provider.
- Default `.env.example` sets `ENABLE_TENCENT_TMT=false` and `ENABLE_HUNYUAN=false`.
- Normal analysis requests must not call Tencent TMT or Hunyuan.

Validation performed in this phase:

- Argos Translate installed in `.venv` and en -> zh model installed.
- Unit tests added for Argos provider, Ollama generator, and analyzer local AI integration.
- Word/phrase cache entries without `exampleSentence` are not written and stale empty entries are ignored.

Historical deployment notes below are retained for reference only and do not describe the current running environment.

## Phase 8L-backend-fill-session-to-target-size — 每次看几张按目标批量补位

**Status:** Completed (2026-05-30)
**Type:** Backend review selection fix + tests; no schema change, no migration, no API schema change
**Backend commit:** pending

### 行为语义

- “每次看几张”是新建一轮 daily_suggested 查看时的目标数量；卡片库可用卡片数足够时，系统会尽量凑满。
- 有未完成 session 时，“继续查看”继续原 session，不扩容，也不因设置变化重写已有 items。
- 新建 session 的高优先级候选仍按既有选卡桶产生；当候选不足目标数量时，追加稳定排序的低优先级补位池。
- 补位池允许今天已看过的卡、non-due reviewing、mastered / remembered 卡；补位卡永远排在新卡、到期卡、熟悉中/需巩固卡之后。
- 补位排序稳定：今天没看过优先于今天已看过；due / strengthening / reviewing 优先于 mastered；再按 created_at / id 排序。
- 同一 session 初始 items 内不重复同一张 card；回炉 item 仍只由反馈后的原有回炉算法追加。
- “今天看过 N 张”和 goal_progress 仍按当天 ReviewLog 的 distinct card_id 去重；同一张卡当天重复查看不重复增加去重计数。
- ReviewLog 仍记录每次真实反馈；“今天已达目标”不阻止用户继续查看卡片。

### 范围

| 文件 | 改动 |
|---|---|
| `app/services/review_rules.py` | 新增低优先级 fill 排序与补位；`select_review_cards(..., fill_target=...)` 仅在传入 fill_target 时补位到目标数量 |
| `app/routers/reviews.py` | `_create_or_return_session` 将 batch target 与 daily_goal remaining 解耦；daily_goal 只决定 goal_mode/今日已看集合，不再用 remaining 缩小本轮大小 |
| `tests/test_reviews_phase2_api.py` | 新增/更新 Phase 8L daily_suggested session 补位、卡片不足、resume 不扩容、去重、mastered 低优先级、3/5/10/15 设置相关测试 |
| `tests/test_review_rules.py` | 明确 `normalize_review_limit(3)` 仍 fallback 到 5，3 张设置由前端 legacy `dailyGoalToLimit(3) -> 5` + `daily_goal=3` 组合保持 |

### 未改动

- 数据库 schema / Alembic migration / 接口 schema 均不变。
- `review_state` 枚举、4 档反馈枚举、回炉算法、ReviewLog 结构不变。
- `new_only` / `free_review` 不新增 endpoint，不改前端 fallback 链。
- 前端 JS/WXML/WXSS、设置页 UI、`dailyGoal` storage key 不变。
- roadmap.md 未更新。

### 产品决策反转

- 旧测试 `test_goal_session_do_not_fill_with_reviewed_cards` 已替换为 `test_goal_session_fills_with_reviewed_cards_when_needed`。
- 旧语义“goal_mode 不用今天已看卡补位”反转为方案 A：“可用卡片总数足够时，新建 session 尽量凑满目标数量，今天已看卡仅作为低优先级补位”。

---

## extend-batch-size-options — 扩展 daily_goal 允许值至 15

**Status:** Completed (2026-05-27)
**Type:** Backend config change only — no schema change, no migration
**Backend commit:** pending

### 修改范围

| 文件 | 改动 |
|---|---|
| `app/services/review_rules.py` | `VALID_DAILY_GOALS = {3, 5, 10, 15}` |

### 行为变化

| daily_goal | 旧行为 | 新行为 |
|---|---|---|
| 15 | fallback → 5 | 合法，返回 15 |
| 1 / 7 / 20 | fallback → 5 | 不变，仍 fallback → 5 |
| 3 / 5 / 10 | 合法 | 不变 |

### 未改动

- 数据库 schema 不变，无新增 Alembic migration
- `VALID_LIMITS = {5, 10, 15}` 已包含 15，不变
- `normalize_review_limit` 不变（`limit=15` 本来就合法）
- `effective_limit = min(remaining, 15)` 的 15 上限不变
- ReviewSession / ReviewSessionItem / ReviewLog schema 不变
- 4 档反馈、回炉算法、Phase 8J cap 不变
- 12 个 pre-existing failures 在 `test_reviews_phase2_api.py` 不属于本次修改（goal_progress/goal_session timezone 问题，确认 pre-existing）

### 验证结果

```
python -m pytest tests/test_review_rules.py -q → 13 passed
python -m pytest -q → 262 passed, 12 pre-existing failures (unchanged)
git diff --check → OK
```

---

## Release P1 — .env.example Environment Variables Documentation

**Status:** Completed (2026-05-26)
**Type:** Documentation only — no code change, no schema change, no migration

`.env.example` 补充了发布所需的实际环境变量占位说明：

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` | PostgreSQL 连接字符串（占位示例） |
| `HUNYUAN_API_KEY` | TokenHub Hunyuan API 密钥（占位） |
| `HUNYUAN_BASE_URL` | TokenHub API base URL（占位） |
| `HUNYUAN_MODEL` | Hunyuan 模型名（占位，当前文档值 `hunyuan-role-latest`） |

原有 `TENCENT_*` / `WECHAT_*` / `JWT_*` 占位保留，未删除。

**未改动：**
- 业务代码不变
- `app/core/config.py` 不变
- 数据库 schema 不变
- 无新增 Alembic migration

---

## Phase 8J — Backend Hotfix: Cap Repeat Item from Restoring mastered

**Status:** Completed (2026-05-26)
**Type:** Backend hotfix — review_state cap only
**Backend commit:** `edc945b`

---

### 一、修复目的

Phase 8J 只读审查发现：回炉项（`is_repeat=True`）在同一 session 内，若 `first_result` 为 `shaky` 或 `forgot`，用户再次点 `fluent` 时，原本会直接恢复 `mastered`。本次 hotfix 精准堵住这个漏洞。

---

### 二、修改范围

| 文件 | 改动内容 |
|---|---|
| `app/services/review_rules.py` | `calculate_review_state_after_feedback` 增加 `is_reappear` / `first_failed_result` 可选参数；`apply_review_feedback_to_card` 同样增加并透传 |
| `app/routers/reviews.py` | Step 7 调用 `apply_review_feedback_to_card` 时传入 `is_reappear=item.is_repeat`，`first_failed_result=item.first_result` |
| `tests/test_review_rules.py` | 新增 `RepeatItemMasteredCapTest`（5 个测试用例） |

---

### 三、核心逻辑变化

`calculate_review_state_after_feedback` fluent 路径：

```python
if mastery_score_after >= 5 and recovery_stage_after == 0:
    # Phase 8J: repeat item that originally failed cannot restore mastered in the same round
    if is_reappear and first_failed_result in {"forgot", "shaky"}:
        return "reviewing"
    return "mastered"
```

- 仅在 `is_reappear=True` 且 `first_failed_result in {"forgot", "shaky"}` 时将 `review_state_after` 从 `mastered` 改为 `reviewing`
- 默认参数 `is_reappear=False` 保持原行为完全不变

---

### 四、明确未改动的内容

- `mastery_score` 计算：不受 cap 影响，`fluent` 后仍正常提升
- `recovery_stage` 计算：不受 cap 影响
- `next_review_at` 计算：不受 cap 影响
- `ReviewLog.result`：仍记录真实 feedback（`fluent`），不改
- `ReviewLog` schema：不变
- 数据库 schema：不变，无 migration
- 4 档反馈枚举：不变
- 回炉算法（`should_append_reappear_item`）：不变
- `daily_suggested` / `new_only` / `free_review` 调度链：不变
- 前端：不变

---

### 五、测试结果

```
tests/test_review_rules.py — 13 passed (8 original + 5 new)
RepeatItemMasteredCapTest::test_repeat_fluent_after_shaky_capped_to_reviewing PASSED
RepeatItemMasteredCapTest::test_repeat_fluent_after_forgot_capped_to_reviewing PASSED
RepeatItemMasteredCapTest::test_non_repeat_fluent_still_reaches_mastered PASSED
RepeatItemMasteredCapTest::test_repeat_fluent_after_got_it_not_blocked PASSED
RepeatItemMasteredCapTest::test_apply_feedback_repeat_shaky_fluent_caps_state_not_score PASSED

python -m pytest -q — 262 passed, 12 pre-existing failures in
  test_reviews_phase2_api.py (goal_progress/goal_session timezone tests,
  confirmed pre-existing: same 12 failures before this diff)

git diff --check — OK
```

---

## Phase 8I — Alphanumeric Classification, Abbreviation Detection & Example Morphology Matching

**Status:** Completed (2026-05-21)
**Type:** Classification fix + Example validation improvement
**Backend commit:** `54db753`

---

### 一、修复了什么

#### 后端：`app/services/validator.py`

**`_classify_text` no-space 分支重写**：

旧 Rule 3 只允许纯字母连字符词，含数字一律 `unknown`。新规则：

```
无空格输入 → 检查是否全由 [A-Za-z0-9.\-'']+ 组成
  → 不是（含 # / @ ! 等）→ unknown
  → 是，但无英文字母 → unknown
  → 是，有英文字母，末尾有 . 且满足 _is_abbreviation_like → word（绕过 SENTENCE_END_RE）
  → 其他 → word
```

效果：
- `COVID-19 / 5G / B2B / H1N1 / MP3 / Web3 / GPT-4` → `word`（进入例句生成）
- `U.S. / e.g. / i.e. / Dr.` → `word`（不再被 SENTENCE_END_RE 判为 sentence）
- `#N/A` → `unknown`（含 `#` `/` 非法字符）
- `2024 / 100-200 / -50` → `unknown`（无英文字母）

**新函数 `_is_abbreviation_like(text)`**：

判断缩写句点模式：末尾有 `.`，所有点分段均为 1-4 个字母。

#### 后端：`app/services/hunyuan_example.py`

**新增 `_IRREGULAR_FORMS` 表**（36 个常见不规则动词）和 **`_generate_word_forms(base)`**：

生成通用词形集合：
- 不规则形式（break→broke/broken，give→gave/given，come→came 等）
- 规则 +s / +ed / +ing
- e 结尾去 e（crave→craving/craved）
- y→ies/ied（study→studied）
- 同化结尾 +es（watch→watches）

**`_text_in_sentence` 重写**：

| 输入类型 | 新行为 |
|---|---|
| 单词 | 精确子串检查 + 词形集合 token 匹配 |
| 短语 | 精确子串检查 + **仅第一词**词形变化，其余词**连续出现** |

核心约束：短语不允许拆分匹配，防止 `commit guilty` 因 `committing` 和 `guilty` 分散出现而通过。

#### 测试：`tests/test_analyzer_unit.py`

新增 51 个测试，3 个新类：
- `AlphanumericClassificationTest`：5G/B2B/H1N1/GPT-4/U.S./e.g./Dr./#N/A/2024 等分类验证
- `AlphanumericExampleChainTest`：含 mock 确认 COVID-19/5G/GPT-4/U.S. 进入 Hunyuan，#N/A/2024 不进入
- `ExampleValidationTest`：crave→craves/craved/craving、break out→broke out/broken out、commit guilty 负例等 22 项

更新 2 个旧测试：`test_covid19_is_unknown` → `test_covid19_is_word`，`test_unknown_category_skips_hunyuan` → `test_covid19_calls_hunyuan`。

全量：**269 passed**（含 31 subtests）。

---

### 二、修改前后分类对照

| input | old category | new category | should_gen_example | reason |
|---|---|---|---|---|
| COVID-19 | unknown | **word** | Y | 含字母，合法字符集 |
| 5G | unknown | **word** | Y | 含字母，合法字符集 |
| B2B | unknown | **word** | Y | 含字母，合法字符集 |
| H1N1 | unknown | **word** | Y | 含字母，合法字符集 |
| MP3 | unknown | **word** | Y | 含字母，合法字符集 |
| Web3 | unknown | **word** | Y | 含字母，合法字符集 |
| GPT-4 | unknown | **word** | Y | 含字母，合法字符集 |
| U.S. | sentence | **word** | Y | 缩写检测，绕过 SENTENCE_END_RE |
| e.g. | sentence | **word** | Y | 缩写检测，绕过 SENTENCE_END_RE |
| i.e. | sentence | **word** | Y | 缩写检测 |
| Dr. | sentence | **word** | Y | 缩写检测 |
| #N/A | phrase | **unknown** | N | 含 # / 非法字符 |
| 2024 | unknown | unknown | N | 无英文字母（error） |
| well-known | word | word | Y | 不变（Phase 8H 已修复） |
| break a leg | phrase | phrase | Y | 不变 |
| I went home. | sentence | sentence | N | 不变 |

---

### 三、例句词形校验对照

| input | example sentence | old result | new result | reason |
|---|---|---|---|---|
| crave | She craves chocolate. | pass | pass | "crave" 是 "craves" 子串 |
| crave | He was craving attention. | pass | pass | e-stem: craving |
| crave | She really wanted chocolate. | fail | fail | 纯同义，无 crave 形式 |
| avoid | She avoided the question. | pass | pass | "avoid" 是 "avoided" 子串 |
| admire | She was admiring the view. | pass | pass | e-stem: admiring |
| break out | A fire broke out last night. | **fail** | **pass** | 短语词形：broke out |
| break out | The disease has broken out. | **fail** | **pass** | 短语词形：broken out |
| give up | She gave up smoking. | **fail** | **pass** | 短语词形：gave up |
| pick up | He picked up the phone. | **fail** | **pass** | 短语词形：picked up |
| come across | I came across an old photo. | **fail** | **pass** | 短语词形：came across |
| break a leg | Good luck with your interview. | fail | fail | 无 break a leg 形式 |
| commit guilty | He was found guilty of committing a crime. | fail | fail | committing + guilty 不连续 |
| COVID-19 | COVID-19 changed the world. | N/A（不生成） | pass | 精确子串，现在进入生成 |

---

### 四、明确没有处理的边界

| 场景 | 状态 |
|---|---|
| 完整句子（I love English.）不生成例句 | 不变，产品语义保留 |
| `commit guilty` 分类仍为 phrase，进入 Hunyuan | 不阻断（产品无害），但词形校验正确拒绝非连续匹配 |
| `make a` 分类为 phrase，`made a` 通过 loose 校验 | 可接受（产品允许 phrase） |
| 完整缩写如 `NASA` 无点号 → word | 不变，一直正确 |
| 不规则名词复数（analysis→analyses）| 未处理，但 analysis 是 analyses 的子串，实际可过 |
| 不做例句持久化 | 不变 |
| 不换模型 | 不变 |
| 数据库 schema 不变 | 不变 |

---

### 五、硬编码白名单声明

**本次实现不含任何针对具体样例的白名单。**

- 分类规则基于字符集结构（`[A-Za-z0-9.\-'']+` + `_has_english`）
- 缩写检测基于段长通用规则（每段 1-4 字母）
- 词形基于通用生成规则（+s/ed/ing/e-stem/y-stem）+ 不规则动词补充表
- COVID-20 / GPT-5 / part-time / co-founder / avoided / admiring / picked up 等同类词无需新增白名单即可适配

---

### 六、人工验收步骤

重启后端：`uvicorn app.main:app --reload`

微信开发者工具重新编译后，在小程序输入：

| 输入 | 预期 category | 预期 | 日志 |
|---|---|---|---|
| COVID-19 | word | 有例句，含 "COVID-19" | [hunyuan][diag] pass |
| GPT-4 | word | 有例句，含 "GPT-4" | [hunyuan][diag] pass |
| 5G | word | 有例句，含 "5G" | [hunyuan][diag] pass |
| B2B | word | 有例句，含 "B2B" | [hunyuan][diag] pass |
| U.S. | word | 有例句，含 "U.S." | [hunyuan][diag] pass |
| e.g. | word | 有例句，含 "e.g." | [hunyuan][diag] pass |
| clutch | word | 有例句 | [hunyuan][diag] pass |
| crave | word | 有例句 | [hunyuan][diag] pass |
| break out | phrase | 有例句（broke out 可通过） | [hunyuan][diag] pass |
| give up | phrase | 有例句（gave up 可通过） | [hunyuan][diag] pass |
| well-known | word | 有例句 | [hunyuan][diag] pass |
| #N/A | unknown | 无例句，error/pass | 不进入 Hunyuan |
| 2024 | unknown | 无例句，error | 不进入 Hunyuan |

---

### 七、测试验证

- `python -m pytest tests/test_analyzer_unit.py -v` → 86/86 passed
- `python -m pytest -q` → 269 passed, 31 subtests passed
- `node --check pages/add/add.js` → OK
- `git diff --check` → OK（仅 LF/CRLF 警告，无内容问题）

---

## Phase 8H — Example Generation Full-Coverage Diagnosis & Minimal Fix

**Status:** Completed (2026-05-21)
**Type:** Diagnostic + Behavioral fix
**Backend commit:** `ef4f946`
**Frontend commit:** `8d10689`

---

### 一、诊断样本矩阵摘要（关键行）

| input | category | should_gen | entered_hunyuan | fail_reason | final_example | root_cause |
|---|---|---|---|---|---|---|
| clutch | word | Y | Y | — | Y | **ok** (模型正常) |
| crave | word | Y | Y | — | Y | **ok** (模型正常) |
| break a leg | phrase | Y | Y | — | Y | **ok** (模型正常) |
| well-known | unknown | Y | N | — | N | **classification_bug** |
| full-time | unknown | Y | N | — | N | **classification_bug** |
| follow-up | unknown | Y | N | — | N | **classification_bug** |
| e-mail | unknown | Y | N | — | N | **classification_bug** |
| co-worker | unknown | Y | N | — | N | **classification_bug** |
| 2024 | unknown | N | N | — | N | expected_no_example |
| 100-200 | unknown | N | N | — | N | expected_no_example |
| -50 | unknown | N | N | — | N | expected_no_example |
| commit guilty | phrase | N | Y | — | Y | ok (generates; not product-intended but not blocked) |
| I don't know | phrase | N | — | — | N | not probed (phrase) |

完整矩阵输出见 `scripts/diagnostic_phase8h.py`。

---

### 二、修复了什么

#### 后端：`app/services/validator.py`

**Rule 3 修复**：`_classify_text` 原规则：
```python
if not re.search(r"\s", text) and re.search(r"[\d-]", text):
    return "unknown"
```
改为：
```python
if not re.search(r"\s", text) and re.search(r"[\d-]", text):
    # Allow hyphenated alphabetic compound words
    if re.search(r"\d", text) or not re.fullmatch(r"[A-Za-z][A-Za-z-]+", text):
        return "unknown"
```

效果：
- `well-known` / `full-time` / `e-mail` / `follow-up` / `check-in` / `make-up` / `co-worker` / `self-control` / `up-to-date` / `state-of-the-art` / `long-term` / `part-time` → 从 `unknown` 变为 `word`，进入 Hunyuan 例句生成
- `2024` / `100-200` / `-50` → 仍为 `unknown`（被 `_is_numeric_value_only` 拦截，Rule 3 不变）
- `COVID-19` / `abc123` → 仍为 `unknown`（含数字，Rule 3 数字条件命中）

#### 前端：`pages/add/add.js`

**stale cache 读取侧修复**：`getAnalyzeCacheItem` 增加检查——若缓存的 word/phrase 条目没有 exampleSentence，则丢弃该条目并触发新请求。这清理了 Phase 8D 前写入的"无例句"旧缓存，避免用户旧设备继续看不到例句。

#### 未改动
- Hunyuan prompt、model、温度、TMT fallback 模板
- 数据库 schema
- sentence/paragraph 不生成例句的产品语义
- 前端展示结构（例句区块）
- 云函数

---

### 三、clutch 为什么没例句

**根因：stale cache（Phase 8D 前写入的旧本地缓存）。**

诊断确认：模型链路完全正常（`strict=pass, final_sentence=Y`）。clutch 被正确分类为 `word`，进入 Hunyuan，第一次 strict 调用即通过。

问题出在用户设备：Phase 8D-hotfix 只修复了"写入"侧（空例句不再被写入缓存），但旧缓存中已存在的 `clutch` 无例句条目继续被读出，直到 30 天 TTL 到期。

**Phase 8H 修复**：`getAnalyzeCacheItem` 读取时检测 word/phrase 且 exampleSentence 为空的条目 → 丢弃 → 触发新请求。

---

### 四、crave 为什么没例句

同 clutch，**根因相同：stale cache**。模型链路正常（`strict=pass, final_sentence=Y`）。旧缓存条目（含空 exampleSentence）在读取时被静默返回。

Phase 8H 同样修复。

---

### 五、break a leg 为什么没例句

同 clutch，**根因相同：stale cache**。模型链路正常（`phrase` 分类，strict=pass，final_sentence=Y）。

---

### 六、连字符词为什么没例句，以及是否已修复

**根因：Rule 3 分类 bug。**

- `well-known`：无空格，含 `-` → Rule 3 → `unknown` → 不进 Hunyuan → 无例句。
- `full-time`：同上。
- `follow-up`：同上。

**已修复（Phase 8H）**：Rule 3 仅在含数字或非字母开头时才拒绝。纯字母+连字符的合法复合词现在正确分类为 `word`，进入 Hunyuan 生成例句。

---

### 七、剩余边界（Phase 8I 待处理）

| 场景 | 当前行为 | 状态 |
|---|---|---|
| 不自然输入 `commit guilty` | 被分类为 phrase，Hunyuan 会生成例句（非预期但无害） | 不在本次修复范围 |
| 缩写句点 `U.S.` / `e.g.` / `Dr.` | 被分类为 sentence（SENTENCE_END_RE 命中末尾句点），不生成例句 | 延至 Phase 8I |
| 数字词 `COVID-19` | 含数字，Rule 3 仍判 unknown，不生成例句 | 延至 Phase 8I（需清晰规则） |
| `#N/A` | 无字母以外符号，被归为 phrase（N、A 两个 token）—产品行为待确认 | 延至 Phase 8I |
| 完整句子是否应生成例句 | sentence/paragraph 不生成（产品语义不变） | 不变 |
| `I don't know` | phrase（3 tokens），当前进入例句生成链路 | 观察，未强制阻断 |

---

### 八、人工验收步骤

重启后端后：`uvicorn app.main:app --reload`

微信开发者工具重新编译后，在小程序输入：

| 输入 | 预期 | 是否有 `[hunyuan][diag]` 日志 |
|---|---|---|
| clutch | 有例句（含 clutch 原词或屈折形） | Y（strict 通过） |
| crave | 有例句 | Y |
| break a leg | 有例句 | Y |
| well-known | 有例句（修复后进入 Hunyuan） | Y |
| full-time | 有例句 | Y |
| follow-up | 有例句 | Y |
| commit guilty | 可能有例句（phrase 类，未阻断） | Y |

若任一词无例句且后端日志出现 `fail_reason`，记录具体 code（`model_api_error` / `loose_match_failed` 等）。

---

### 测试验证

- `python -m pytest tests/test_analyzer_unit.py -v` → 35/35 passed
- `python -m pytest -q` → 218/218 passed
- `node --check pages/add/add.js` → OK
- `git diff --check` → OK（后端）

---

## Phase 8E-diagnostic — Example Generation Diagnostic Logging

**Status:** Completed (2026-05-21)
**Type:** Diagnostic hotfix — logging only, no business logic change

### Purpose

Add structured diagnostic logs to pinpoint why example sentences fail to generate.
This phase does NOT change any validation rules, model configuration, prompts, or
fallback behavior. It only adds log output.

### Files Changed

- `app/services/hunyuan_example.py`
  - `_call_and_validate()`: added entry log, raw-response log (≤300 chars),
    parsed-sentence log, and structured `fail_reason` codes at every exit path.
  - `generate_example_with_hunyuan()`: added entry log, no-retry log, retry log,
    and final loose-match failure log.
- `app/services/analyzer.py`
  - `_generate_example_with_tmt()`: added entry log, per-template attempt log,
    and `fail_reason=tmt_fallback_failed` on all failure paths.

### Diagnostic fail_reason Codes

| Code | Trigger |
|---|---|
| `model_api_error` | Non-200 HTTP status, no API key, or unexpected exception |
| `model_timeout` | `requests.exceptions.Timeout` (15 s limit) |
| `empty_response` | No `choices` in response, or content string is empty |
| `json_parse_failed` | No `{}` braces found, or `json.JSONDecodeError` |
| `missing_example_sentence` | `exampleSentence` or `exampleTranslation` empty after parse |
| `exact_match_failed` | Sentence equals bare input text (strict mode: word not in sentence) |
| `too_few_words` | Sentence has fewer than 3 tokens |
| `loose_match_failed` | Word/inflection not found in sentence (loose mode) |
| `tmt_fallback_failed` | All TMT template translations failed to produce a matching sentence |

### Log Format

All diagnostic log lines use the prefix `[hunyuan][diag]` or `[tmt][diag]` and
follow the pattern `key=value | key=value` for easy grepping.

Examples:
```
[hunyuan][diag] start | text='clutch' | mode=strict | has_translation=True
[hunyuan][diag] raw_response(300)='{"exampleSentence": ...' | text='clutch' | mode=strict
[hunyuan][diag] parsed | text='clutch' | mode=strict | exampleSentence='She clutched her bag tightly.'
[hunyuan][diag] pass | text='clutch' | mode=strict | sentence='She clutched her bag tightly.'
[tmt][diag] fail_reason=tmt_fallback_failed | text='commit guilty' | all templates failed
```

### Safety Constraints

- No API keys, tokens, or request headers are logged.
- Raw model response is capped at 300 characters.
- `sentence/paragraph` inputs are still excluded from example generation.
- Cache behavior is unchanged.
- Return structure (`exampleSentence`, `exampleTranslation`) is unchanged.

### Test Verification

- `python -m pytest tests/test_analyzer_unit.py -v` → 9/9 passed
- `python -m pytest -q` → 191/192 passed (1 pre-existing failure in
  `test_reviews_phase2_api.py::test_processing_zombie_allows_reprocessing`,
  unrelated to example generation)

### Next Steps

After collecting logs from real requests with the 3 sample inputs
(`clutch`, `break a leg`, `commit guilty`), diagnose which fail_reason
code appears and decide if a Phase 8F behavior fix is warranted.
