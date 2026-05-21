"""
Phase 8H — Example Generation Coverage Diagnostic
Run from the backend root directory:

    python scripts/diagnostic_phase8h.py

With HUNYUAN_API_KEY in environment (or .env), a real Hunyuan call is made
for a subset of samples.  Without the key only the classification matrix is
produced.

Output: prints a table to stdout.  No file is written.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow importing app.* from the backend root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Load .env before importing settings-dependent modules
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass  # dotenv optional

from app.services.validator import validate_english, _get_english_tokens, normalize_text  # noqa: E402

# ─────────────────────────────────────────────────────────
# Sample matrix (Phase 8H spec)
# ─────────────────────────────────────────────────────────
SAMPLES: list[dict] = [
    # A — common words
    {"input": "clutch",        "should_gen": True,  "group": "A"},
    {"input": "crave",         "should_gen": True,  "group": "A"},
    {"input": "charge",        "should_gen": True,  "group": "A"},
    {"input": "fine",          "should_gen": True,  "group": "A"},
    {"input": "case",          "should_gen": True,  "group": "A"},
    {"input": "awkward",       "should_gen": True,  "group": "A"},
    {"input": "confident",     "should_gen": True,  "group": "A"},
    {"input": "hesitate",      "should_gen": True,  "group": "A"},
    # B — verb/adj/noun
    {"input": "admire",        "should_gen": True,  "group": "B"},
    {"input": "avoid",         "should_gen": True,  "group": "B"},
    {"input": "guilty",        "should_gen": True,  "group": "B"},
    {"input": "honest",        "should_gen": True,  "group": "B"},
    {"input": "evidence",      "should_gen": True,  "group": "B"},
    {"input": "deadline",      "should_gen": True,  "group": "B"},
    # C — phrases
    {"input": "pick up",       "should_gen": True,  "group": "C"},
    {"input": "break out",     "should_gen": True,  "group": "C"},
    {"input": "give up",       "should_gen": True,  "group": "C"},
    {"input": "look into",     "should_gen": True,  "group": "C"},
    {"input": "figure out",    "should_gen": True,  "group": "C"},
    {"input": "come across",   "should_gen": True,  "group": "C"},
    # D — idioms
    {"input": "break a leg",   "should_gen": True,  "group": "D"},
    {"input": "piece of cake", "should_gen": True,  "group": "D"},
    {"input": "hit the road",  "should_gen": True,  "group": "D"},
    {"input": "on the same page", "should_gen": True, "group": "D"},
    # E — hyphenated words (Rule 3 bug targets)
    {"input": "well-known",    "should_gen": True,  "group": "E"},
    {"input": "full-time",     "should_gen": True,  "group": "E"},
    {"input": "part-time",     "should_gen": True,  "group": "E"},
    {"input": "up-to-date",    "should_gen": True,  "group": "E"},
    {"input": "state-of-the-art", "should_gen": True, "group": "E"},
    {"input": "long-term",     "should_gen": True,  "group": "E"},
    {"input": "self-control",  "should_gen": True,  "group": "E"},
    {"input": "e-mail",        "should_gen": True,  "group": "E"},
    {"input": "co-worker",     "should_gen": True,  "group": "E"},
    {"input": "follow-up",     "should_gen": True,  "group": "E"},
    {"input": "check-in",      "should_gen": True,  "group": "E"},
    {"input": "make-up",       "should_gen": True,  "group": "E"},
    # F — contractions / abbreviations
    {"input": "don't",         "should_gen": True,  "group": "F"},
    {"input": "can't",         "should_gen": True,  "group": "F"},
    {"input": "I'm",           "should_gen": True,  "group": "F"},
    {"input": "you're",        "should_gen": True,  "group": "F"},
    {"input": "U.S.",          "should_gen": False, "group": "F"},
    {"input": "e.g.",          "should_gen": False, "group": "F"},
    {"input": "i.e.",          "should_gen": False, "group": "F"},
    {"input": "Dr.",           "should_gen": False, "group": "F"},
    {"input": "etc.",          "should_gen": False, "group": "F"},
    # G — unnatural inputs
    {"input": "commit guilty", "should_gen": False, "group": "G"},
    {"input": "make a",        "should_gen": False, "group": "G"},
    {"input": "very",          "should_gen": True,  "group": "G"},
    {"input": "be",            "should_gen": True,  "group": "G"},
    {"input": "in order to",   "should_gen": True,  "group": "G"},
    {"input": "as well as",    "should_gen": True,  "group": "G"},
    # H — sentences
    {"input": "I don't know",              "should_gen": False, "group": "H"},
    {"input": "I crave chocolate at night.", "should_gen": False, "group": "H"},
    {"input": "Good luck with your interview.", "should_gen": False, "group": "H"},
    {"input": "I couldn't break out of the cycle.", "should_gen": False, "group": "H"},
    # I — numbers / symbols / reject
    {"input": "2024",    "should_gen": False, "group": "I"},
    {"input": "100-200", "should_gen": False, "group": "I"},
    {"input": "-50",     "should_gen": False, "group": "I"},
    {"input": "#N/A",    "should_gen": False, "group": "I"},
    {"input": "abc123",  "should_gen": False, "group": "I"},
]

# Key samples to run through the full Hunyuan chain
HUNYUAN_PROBE_INPUTS = {
    "clutch", "crave", "break a leg",
    "well-known", "full-time", "follow-up",
    "commit guilty",
}


def classify(text: str) -> dict:
    """Return validate_english result dict."""
    return validate_english(text)


def _root_cause(
    category: str,
    should_gen: bool,
    entered_hunyuan: bool | None,
    final_sentence: str | None,
    final_trans: str | None,
    fail_reason: str | None,
) -> str:
    if category == "unknown" and should_gen:
        return "classification_bug"
    if not should_gen and category in ("sentence", "paragraph"):
        return "expected_no_example"
    if not should_gen and category == "unknown":
        if not fail_reason:
            return "unsupported_input"
        return "validation_rejected"
    if fail_reason in ("model_api_error", "model_timeout"):
        return "model_api_error"
    if fail_reason in ("empty_response", "json_parse_failed",
                       "missing_example_sentence", "exact_match_failed",
                       "too_few_words", "loose_match_failed"):
        return "model_empty_or_parse_error"
    if fail_reason == "tmt_fallback_failed":
        return "model_empty_or_parse_error"
    if final_sentence:
        return "ok"
    if entered_hunyuan is False and not should_gen:
        return "expected_no_example"
    return "unknown"


def run_hunyuan_probe(text: str, category: str) -> dict:
    """
    Call analyze_text for real.  Returns a dict with:
    entered_hunyuan, strict_result, loose_result,
    final_sentence, final_trans, fail_reason, entered_tmt.
    """
    result = {
        "entered_hunyuan": None,
        "strict_result": "n/a",
        "loose_result": "n/a",
        "final_sentence": None,
        "final_trans": None,
        "fail_reason": None,
        "entered_tmt": None,
    }

    if category not in ("word", "phrase"):
        result["entered_hunyuan"] = False
        result["entered_tmt"] = False
        return result

    from app.core.config import settings
    api_key = (settings.hunyuan_api_key or "").strip()
    if not api_key:
        result["entered_hunyuan"] = False
        result["fail_reason"] = "model_api_error (no key)"
        return result

    result["entered_hunyuan"] = True

    import logging
    import io
    import re as _re

    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.DEBUG)
    # Capture hunyuan and tmt logs
    for logger_name in ("app.services.hunyuan_example", "app.services.analyzer"):
        lg = logging.getLogger(logger_name)
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)

    try:
        from app.services.hunyuan_example import generate_example_with_hunyuan
        from app.services.analyzer import _generate_example_with_tmt
        # patch cache to avoid stale hits
        import unittest.mock as mock
        with mock.patch("app.services.analyzer.get_cache", return_value=None), \
             mock.patch("app.services.analyzer.set_cache"):
            sentence, trans = generate_example_with_hunyuan(text, None)
    except Exception as exc:
        sentence, trans = None, None
        result["fail_reason"] = f"exception: {exc}"
    finally:
        for logger_name in ("app.services.hunyuan_example", "app.services.analyzer"):
            logging.getLogger(logger_name).removeHandler(handler)

    result["final_sentence"] = sentence
    result["final_trans"] = trans

    logs = log_stream.getvalue()

    # Parse fail_reason from logs
    fail_matches = _re.findall(r"fail_reason=(\S+)", logs)
    if fail_matches:
        result["fail_reason"] = fail_matches[-1]
    elif sentence:
        result["fail_reason"] = None

    # Detect strict/loose results
    if "mode=strict" in logs:
        if "[hunyuan][diag] pass" in logs and "mode=strict" in logs:
            result["strict_result"] = "pass"
        else:
            strict_fail = [m for m in _re.findall(
                r"\[hunyuan\]\[diag\] fail_reason=(\S+) \| text=.+? \| mode=strict", logs
            )]
            result["strict_result"] = strict_fail[-1] if strict_fail else "fail"

    if "mode=loose" in logs:
        if "[hunyuan][diag] pass" in logs and "mode=loose" in logs:
            result["loose_result"] = "pass"
        else:
            loose_fail = [m for m in _re.findall(
                r"\[hunyuan\]\[diag\] fail_reason=(\S+) \| text=.+? \| mode=loose", logs
            )]
            result["loose_result"] = loose_fail[-1] if loose_fail else "fail"

    # TMT
    result["entered_tmt"] = "[tmt][diag] start" in logs

    return result


def fmt(v) -> str:
    if v is None:
        return "—"
    if v is True:
        return "Y"
    if v is False:
        return "N"
    s = str(v)
    return s[:40]


def main() -> None:
    from app.core.config import settings
    api_key = (settings.hunyuan_api_key or "").strip()
    has_api = bool(api_key)

    print(f"\nPhase 8H Diagnostic — {'HUNYUAN available' if has_api else 'NO HUNYUAN KEY (classification only)'}")
    print(f"HUNYUAN_BASE_URL = {settings.hunyuan_base_url}")
    print(f"HUNYUAN_MODEL    = {settings.hunyuan_model}")
    print()

    COL_W = {
        "input":         22,
        "grp":            3,
        "cat":            9,
        "tokens":         6,
        "should":         7,
        "entered":        8,
        "strict":        22,
        "loose":         22,
        "fail":          28,
        "tmt":            5,
        "sent":           6,
        "tr":             5,
        "root":          26,
    }

    header = (
        f"{'input':<{COL_W['input']}} "
        f"{'G':<{COL_W['grp']}} "
        f"{'category':<{COL_W['cat']}} "
        f"{'tokens':<{COL_W['tokens']}} "
        f"{'should?':<{COL_W['should']}} "
        f"{'entered?':<{COL_W['entered']}} "
        f"{'strict':<{COL_W['strict']}} "
        f"{'loose':<{COL_W['loose']}} "
        f"{'fail_reason':<{COL_W['fail']}} "
        f"{'tmt?':<{COL_W['tmt']}} "
        f"{'sent?':<{COL_W['sent']}} "
        f"{'tr?':<{COL_W['tr']}} "
        f"{'root_cause':<{COL_W['root']}}"
    )
    print(header)
    print("-" * len(header))

    for s in SAMPLES:
        text = s["input"]
        should_gen = s["should_gen"]
        group = s["group"]

        val = classify(text)
        category = val["category"]
        normalized = val.get("normalizedText") or text
        token_count = len(_get_english_tokens(normalized))

        run_probe = has_api and text in HUNYUAN_PROBE_INPUTS
        if run_probe:
            probe = run_hunyuan_probe(text, category)
        else:
            probe = {
                "entered_hunyuan": None,
                "strict_result": "—",
                "loose_result": "—",
                "final_sentence": None,
                "final_trans": None,
                "fail_reason": None,
                "entered_tmt": None,
            }

        root = _root_cause(
            category, should_gen,
            probe["entered_hunyuan"],
            probe["final_sentence"],
            probe["final_trans"],
            probe["fail_reason"],
        )

        row = (
            f"{text:<{COL_W['input']}} "
            f"{group:<{COL_W['grp']}} "
            f"{category:<{COL_W['cat']}} "
            f"{token_count:<{COL_W['tokens']}} "
            f"{'Y' if should_gen else 'N':<{COL_W['should']}} "
            f"{fmt(probe['entered_hunyuan']):<{COL_W['entered']}} "
            f"{fmt(probe['strict_result']):<{COL_W['strict']}} "
            f"{fmt(probe['loose_result']):<{COL_W['loose']}} "
            f"{fmt(probe['fail_reason']):<{COL_W['fail']}} "
            f"{fmt(probe['entered_tmt']):<{COL_W['tmt']}} "
            f"{fmt(bool(probe['final_sentence'])):<{COL_W['sent']}} "
            f"{fmt(bool(probe['final_trans'])):<{COL_W['tr']}} "
            f"{root:<{COL_W['root']}}"
        )
        print(row)

    print()
    print("Notes:")
    print("  entered? = entered Hunyuan (None = not probed; N = skipped by category)")
    print("  should? = per product spec, should example be generated")
    print("  tmt? = TMT fallback triggered")
    print("  sent? / tr? = final exampleSentence / exampleTranslation non-empty")
    if not has_api:
        print()
        print("  WARNING: HUNYUAN_API_KEY not configured.")
        print("  Hunyuan chain not validated. Only classification results are shown.")


if __name__ == "__main__":
    main()
