# Current Development Phase

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
