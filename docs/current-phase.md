# Current Development Phase

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
