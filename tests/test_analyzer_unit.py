"""
Unit tests for app.services.analyzer.analyze_text.
Focus: example-sentence generation gate (Phase 8D-hotfix).
"""
import unittest
from unittest.mock import ANY, patch


class AnalyzerExampleGateTest(unittest.TestCase):
    """
    Verify that word/phrase inputs attempt example generation even when
    translation is unavailable (Phase 8D-hotfix: removed 'and translation' gate).
    """

    def _call(self, text, translation_ok=True, translation_value="测试中文"):
        """Run analyze_text with controlled translator and Ollama mocks."""
        from app.services.analyzer import analyze_text

        translation_result = (
            {"ok": True, "translation": translation_value, "provider": "tencent"}
            if translation_ok
            else {"ok": False, "translation": None, "provider": None}
        )

        with (
            patch("app.services.analyzer.get_cache", return_value=None),
            patch("app.services.analyzer.set_cache"),
            patch("app.services.analyzer.translate_to_zh", return_value=translation_result),
            patch("app.services.analyzer.generate_understanding", return_value="mocked understanding"),
            patch("app.services.analyzer.generate_analysis_with_ollama", return_value=None) as mock_ollama,
            patch("app.services.analyzer._generate_example_with_tmt") as mock_tmt,
        ):
            mock_tmt.return_value = (None, None)
            result = analyze_text(text)
            return result, mock_ollama, mock_tmt

    # ------------------------------------------------------------------
    # Word + translation available
    # ------------------------------------------------------------------

    def test_word_with_translation_calls_ollama(self):
        result, mock_ollama, mock_tmt = self._call("apply", translation_ok=True)
        self.assertEqual(result["category"], "word")
        mock_ollama.assert_called_once_with("apply", "word", deadline=ANY, attempt_recorder=None, regenerate_context=None)

    def test_word_with_translation_does_not_call_tmt_by_default(self):
        result, mock_ollama, mock_tmt = self._call("apply", translation_ok=True)
        mock_tmt.assert_not_called()

    # ------------------------------------------------------------------
    # Word + translation unavailable (the new gate-removal behavior)
    # ------------------------------------------------------------------

    def test_word_without_translation_still_calls_ollama(self):
        """Ollama must be attempted even when translation failed."""
        result, mock_ollama, mock_tmt = self._call("apply", translation_ok=False)
        self.assertEqual(result["category"], "word")
        mock_ollama.assert_called_once_with("apply", "word", deadline=ANY, attempt_recorder=None, regenerate_context=None)

    def test_word_without_translation_skips_tmt(self):
        """TMT needs a translation to build template sentences — must be skipped."""
        result, mock_ollama, mock_tmt = self._call("apply", translation_ok=False)
        mock_tmt.assert_not_called()

    # ------------------------------------------------------------------
    # Phrase
    # ------------------------------------------------------------------

    def test_phrase_without_translation_calls_ollama(self):
        result, mock_ollama, mock_tmt = self._call("give up", translation_ok=False)
        self.assertEqual(result["category"], "phrase")
        mock_ollama.assert_called_once_with("give up", "phrase", deadline=ANY, attempt_recorder=None, regenerate_context=None)

    def test_phrase_without_translation_skips_tmt(self):
        result, mock_ollama, mock_tmt = self._call("give up", translation_ok=False)
        mock_tmt.assert_not_called()

    # ------------------------------------------------------------------
    # Sentence — now prefers Ollama (word/phrase/sentence unified)
    # ------------------------------------------------------------------

    def test_sentence_calls_ollama(self):
        result, mock_ollama, mock_tmt = self._call(
            "I love English.", translation_ok=True
        )
        self.assertEqual(result["category"], "sentence")
        mock_ollama.assert_called_once_with("I love English.", "sentence", deadline=ANY, attempt_recorder=None, regenerate_context=None)
        mock_tmt.assert_not_called()

    def test_sentence_without_translation_calls_ollama(self):
        result, mock_ollama, mock_tmt = self._call(
            "I love English.", translation_ok=False
        )
        self.assertEqual(result["category"], "sentence")
        mock_ollama.assert_called_once_with("I love English.", "sentence", deadline=ANY, attempt_recorder=None, regenerate_context=None)

    # ------------------------------------------------------------------
    # Hunyuan success propagates to response
    # ------------------------------------------------------------------

    def test_ollama_result_returned_in_response(self):
        from app.services.analyzer import analyze_text
        with (
            patch("app.services.analyzer.get_cache", return_value=None),
            patch("app.services.analyzer.set_cache"),
            patch("app.services.analyzer.translate_to_zh",
                  return_value={"ok": False, "translation": None, "provider": None}),
            patch("app.services.analyzer.generate_understanding", return_value="u"),
            patch("app.services.analyzer.generate_analysis_with_ollama",
                  return_value={
                      "meaning": None,
                      "exampleSentence": "She craves success.",
                      "exampleTranslation": "她渴望成功。",
                      "synonyms": [],
                      "similarPhrases": [],
                  }),
            patch("app.services.analyzer._generate_example_with_tmt",
                  return_value=(None, None)),
        ):
            result = analyze_text("crave")
        self.assertEqual(result["exampleSentence"], "She craves success.")
        self.assertEqual(result["exampleTranslation"], "她渴望成功。")


class ValidatorClassificationTest(unittest.TestCase):
    """
    Phase 8H: verify that validate_english classifies inputs correctly,
    including the Rule-3 fix for hyphenated compound words.
    """

    def _cat(self, text: str) -> str:
        from app.services.validator import validate_english
        return validate_english(text)["category"]

    # ── Common words ──────────────────────────────────────────────
    def test_clutch_is_word(self):
        self.assertEqual(self._cat("clutch"), "word")

    def test_crave_is_word(self):
        self.assertEqual(self._cat("crave"), "word")

    # ── Phrases ───────────────────────────────────────────────────
    def test_break_a_leg_is_phrase(self):
        self.assertEqual(self._cat("break a leg"), "phrase")

    def test_pick_up_is_phrase(self):
        self.assertEqual(self._cat("pick up"), "phrase")

    def test_commit_guilty_is_phrase(self):
        self.assertEqual(self._cat("commit guilty"), "phrase")

    # ── Hyphenated compound words (Rule-3 fix) ────────────────────
    def test_well_known_is_word(self):
        self.assertEqual(self._cat("well-known"), "word")

    def test_full_time_is_word(self):
        self.assertEqual(self._cat("full-time"), "word")

    def test_part_time_is_word(self):
        self.assertEqual(self._cat("part-time"), "word")

    def test_up_to_date_is_word(self):
        self.assertEqual(self._cat("up-to-date"), "word")

    def test_state_of_the_art_is_word(self):
        self.assertEqual(self._cat("state-of-the-art"), "word")

    def test_long_term_is_word(self):
        self.assertEqual(self._cat("long-term"), "word")

    def test_self_control_is_word(self):
        self.assertEqual(self._cat("self-control"), "word")

    def test_e_mail_is_word(self):
        self.assertEqual(self._cat("e-mail"), "word")

    def test_co_worker_is_word(self):
        self.assertEqual(self._cat("co-worker"), "word")

    def test_follow_up_is_word(self):
        self.assertEqual(self._cat("follow-up"), "word")

    def test_check_in_is_word(self):
        self.assertEqual(self._cat("check-in"), "word")

    def test_make_up_is_word(self):
        self.assertEqual(self._cat("make-up"), "word")

    # ── Numbers / symbols — must stay unknown or error ────────────
    def test_2024_not_example_eligible(self):
        result = __import__("app.services.validator", fromlist=["validate_english"]).validate_english("2024")
        self.assertIn(result["category"], ("unknown",))
        self.assertEqual(result["level"], "error")

    def test_numeric_range_not_example_eligible(self):
        result = __import__("app.services.validator", fromlist=["validate_english"]).validate_english("100-200")
        self.assertIn(result["category"], ("unknown",))
        self.assertEqual(result["level"], "error")

    def test_negative_number_not_example_eligible(self):
        result = __import__("app.services.validator", fromlist=["validate_english"]).validate_english("-50")
        self.assertIn(result["category"], ("unknown",))
        self.assertEqual(result["level"], "error")

    # ── Phase 8I: COVID-19 reclassified as word (has letter + digit) ──
    def test_covid19_is_word(self):
        self.assertEqual(self._cat("COVID-19"), "word")


class HyphenatedWordExampleChainTest(unittest.TestCase):
    """
    Phase 8H: verify that hyphenated words (after Rule-3 fix) enter
    example generation, and that the gate logic is correct.
    """

    def _call_with_mocks(self, text):
        from app.services.analyzer import analyze_text

        with (
            patch("app.services.analyzer.get_cache", return_value=None),
            patch("app.services.analyzer.set_cache"),
            patch("app.services.analyzer.translate_to_zh",
                  return_value={"ok": True, "translation": "测试", "provider": "tencent"}),
            patch("app.services.analyzer.generate_understanding", return_value="u"),
            patch("app.services.analyzer.generate_analysis_with_ollama", return_value=None) as mock_ollama,
            patch("app.services.analyzer._generate_example_with_tmt") as mock_tmt,
        ):
            mock_tmt.return_value = (None, None)
            result = analyze_text(text)
            return result, mock_ollama, mock_tmt

    def test_well_known_calls_ollama(self):
        result, mock_ollama, _ = self._call_with_mocks("well-known")
        self.assertEqual(result["category"], "word")
        mock_ollama.assert_called_once_with("well-known", "word", deadline=ANY, attempt_recorder=None, regenerate_context=None)

    def test_full_time_calls_ollama(self):
        result, mock_ollama, _ = self._call_with_mocks("full-time")
        self.assertEqual(result["category"], "word")
        mock_ollama.assert_called_once()

    def test_follow_up_calls_ollama(self):
        result, mock_ollama, _ = self._call_with_mocks("follow-up")
        self.assertEqual(result["category"], "word")
        mock_ollama.assert_called_once()

    def test_covid19_calls_ollama(self):
        """Phase 8I: COVID-19 is now classified as word and enters Ollama."""
        result, mock_ollama, _ = self._call_with_mocks("COVID-19")
        self.assertEqual(result["category"], "word")
        mock_ollama.assert_called_once()

    def test_hyphenated_word_ollama_result_propagated(self):
        from app.services.analyzer import analyze_text

        with (
            patch("app.services.analyzer.get_cache", return_value=None),
            patch("app.services.analyzer.set_cache"),
            patch("app.services.analyzer.translate_to_zh",
                  return_value={"ok": True, "translation": "全职", "provider": "tencent"}),
            patch("app.services.analyzer.generate_understanding", return_value="u"),
            patch("app.services.analyzer.generate_analysis_with_ollama",
                  return_value={
                      "meaning": None,
                      "exampleSentence": "She works full-time.",
                      "exampleTranslation": "她全职工作。",
                      "synonyms": [],
                      "similarPhrases": [],
                  }),
            patch("app.services.analyzer._generate_example_with_tmt",
                  return_value=(None, None)),
        ):
            result = analyze_text("full-time")

        self.assertEqual(result["category"], "word")
        self.assertEqual(result["exampleSentence"], "She works full-time.")
        self.assertEqual(result["exampleTranslation"], "她全职工作。")


class AlphanumericClassificationTest(unittest.TestCase):
    """Phase 8I: alphanumeric terms and abbreviations classified as word."""

    def _cat(self, text: str) -> str:
        from app.services.validator import validate_english
        return validate_english(text)["category"]

    # ── Alphanumeric terms → word ──────────────────────────────────
    def test_5g_is_word(self):
        self.assertEqual(self._cat("5G"), "word")

    def test_b2b_is_word(self):
        self.assertEqual(self._cat("B2B"), "word")

    def test_h1n1_is_word(self):
        self.assertEqual(self._cat("H1N1"), "word")

    def test_mp3_is_word(self):
        self.assertEqual(self._cat("MP3"), "word")

    def test_web3_is_word(self):
        self.assertEqual(self._cat("Web3"), "word")

    def test_gpt4_is_word(self):
        self.assertEqual(self._cat("GPT-4"), "word")

    def test_covid19_is_word(self):
        self.assertEqual(self._cat("COVID-19"), "word")

    # ── Abbreviations with dots → word ────────────────────────────
    def test_us_abbreviation_is_word(self):
        self.assertEqual(self._cat("U.S."), "word")

    def test_eg_abbreviation_is_word(self):
        self.assertEqual(self._cat("e.g."), "word")

    def test_ie_abbreviation_is_word(self):
        self.assertEqual(self._cat("i.e."), "word")

    def test_dr_abbreviation_is_word(self):
        self.assertEqual(self._cat("Dr."), "word")

    # ── Symbol-like inputs → unknown ──────────────────────────────
    def test_hash_na_is_unknown(self):
        self.assertEqual(self._cat("#N/A"), "unknown")

    def test_at_signs_is_unknown(self):
        from app.services.validator import validate_english
        result = validate_english("@@@")
        self.assertIn(result["level"], ("error",))

    def test_slash_is_unknown(self):
        from app.services.validator import validate_english
        result = validate_english("/")
        self.assertIn(result["level"], ("error",))

    # ── Pure numbers stay unknown/error ───────────────────────────
    def test_pure_number_2024(self):
        from app.services.validator import validate_english
        result = validate_english("2024")
        self.assertEqual(result["level"], "error")

    def test_numeric_range(self):
        from app.services.validator import validate_english
        result = validate_english("100-200")
        self.assertEqual(result["level"], "error")

    # ── Real sentence still → sentence (not confused with abbrev) ─
    def test_complete_sentence_not_word(self):
        self.assertEqual(self._cat("I went home."), "sentence")

    def test_multiword_sentence_not_word(self):
        self.assertEqual(self._cat("She loves English."), "sentence")


class HardRulesV1Test(unittest.TestCase):
    def _level(self, text: str) -> str:
        from app.services.validator import validate_english

        return validate_english(text)["level"]

    def test_invalid_inputs_are_hard_rejected(self):
        invalid_inputs = [
            "",
            "   ",
            "中文",
            "hello 中文",
            "123",
            "12.5",
            "!!!",
            "😀",
            "https://example.com",
            "test@example.com",
            "<div>Hello</div>",
            "console.log('hi');",
            r"C:\Users\test",
            "hello\u200b",
            "hello\x00",
            "aaaaaaaaaaaa",
        ]

        for text in invalid_inputs:
            with self.subTest(text=repr(text)):
                self.assertEqual(self._level(text), "error")

    def test_valid_english_inputs_are_not_hard_rejected(self):
        long_text = "This is a normal English sentence with useful context. " * 12
        valid_inputs = [
            "gonna",
            "ain't",
            "Netflix",
            "ChatGPT",
            "LOL",
            "no way",
            "what the hell",
            "Coming?",
            "So good.",
            "I dunno.",
            "soooo good",
            "nooooo",
            "yessss",
            "U.S.",
            "e.g.",
            "can't",
            "mother-in-law",
            long_text,
        ]

        self.assertGreater(len(long_text), 500)
        for text in valid_inputs:
            with self.subTest(text=repr(text)):
                self.assertNotEqual(self._level(text), "error")

    def test_normalize_is_idempotent_for_typical_input(self):
        from app.services.validator import normalize_text

        normalized = normalize_text("I don \u2019 t know \u3002 ")
        self.assertEqual(normalized, "I don't know.")
        self.assertEqual(normalize_text(normalized), normalized)

    def test_ai_analysis_rejects_invalid_text_before_downstream_work(self):
        from app.services.analyzer import analyze_text

        with (
            patch("app.services.analyzer.translate_to_zh") as mock_translate,
            patch("app.services.analyzer.generate_understanding") as mock_understanding,
            patch("app.services.analyzer.generate_analysis_with_ollama") as mock_ai,
        ):
            result = analyze_text("hello \u4e2d\u6587")

        self.assertFalse(result["ok"])
        self.assertEqual(result["level"], "error")
        self.assertEqual(result["normalizedText"], "hello \u4e2d\u6587")
        mock_translate.assert_not_called()
        mock_understanding.assert_not_called()
        mock_ai.assert_not_called()


class AlphanumericExampleChainTest(unittest.TestCase):
    """Phase 8I: alphanumeric / abbreviation words enter Ollama generation."""

    def _call_with_mocks(self, text):
        from app.services.analyzer import analyze_text
        with (
            patch("app.services.analyzer.get_cache", return_value=None),
            patch("app.services.analyzer.set_cache"),
            patch("app.services.analyzer.translate_to_zh",
                  return_value={"ok": True, "translation": "测试", "provider": "tencent"}),
            patch("app.services.analyzer.generate_understanding", return_value="u"),
            patch("app.services.analyzer.generate_analysis_with_ollama", return_value=None) as mock_ollama,
            patch("app.services.analyzer._generate_example_with_tmt") as mock_tmt,
        ):
            mock_tmt.return_value = (None, None)
            result = analyze_text(text)
            return result, mock_ollama, mock_tmt

    def test_5g_calls_ollama(self):
        result, mock_ollama, _ = self._call_with_mocks("5G")
        self.assertEqual(result["category"], "word")
        mock_ollama.assert_called_once()

    def test_b2b_calls_ollama(self):
        result, mock_ollama, _ = self._call_with_mocks("B2B")
        self.assertEqual(result["category"], "word")
        mock_ollama.assert_called_once()

    def test_gpt4_calls_ollama(self):
        result, mock_ollama, _ = self._call_with_mocks("GPT-4")
        self.assertEqual(result["category"], "word")
        mock_ollama.assert_called_once()

    def test_us_abbreviation_calls_ollama(self):
        result, mock_ollama, _ = self._call_with_mocks("U.S.")
        self.assertEqual(result["category"], "word")
        mock_ollama.assert_called_once()

    def test_eg_abbreviation_calls_ollama(self):
        result, mock_ollama, _ = self._call_with_mocks("e.g.")
        self.assertEqual(result["category"], "word")
        mock_ollama.assert_called_once()

    def test_pure_number_skips_ollama(self):
        result, mock_ollama, mock_tmt = self._call_with_mocks("2024")
        self.assertEqual(result["ok"], False)
        mock_ollama.assert_not_called()
        mock_tmt.assert_not_called()

    def test_hash_na_skips_ollama(self):
        # #N/A has no validation error (N/A are letters) but category=unknown → no example
        result, mock_ollama, mock_tmt = self._call_with_mocks("#N/A")
        self.assertEqual(result["category"], "unknown")
        mock_ollama.assert_not_called()
        mock_tmt.assert_not_called()

    def test_alphanumeric_example_propagated(self):
        from app.services.analyzer import analyze_text
        with (
            patch("app.services.analyzer.get_cache", return_value=None),
            patch("app.services.analyzer.set_cache"),
            patch("app.services.analyzer.translate_to_zh",
                  return_value={"ok": True, "translation": "新冠", "provider": "tencent"}),
            patch("app.services.analyzer.generate_understanding", return_value="u"),
            patch("app.services.analyzer.generate_analysis_with_ollama",
                  return_value={
                      "meaning": None,
                      "exampleSentence": "COVID-19 changed the world.",
                      "exampleTranslation": "新冠疫情改变了世界。",
                      "synonyms": [],
                      "similarPhrases": [],
                  }),
            patch("app.services.analyzer._generate_example_with_tmt", return_value=(None, None)),
        ):
            result = analyze_text("COVID-19")
        self.assertEqual(result["category"], "word")
        self.assertEqual(result["exampleSentence"], "COVID-19 changed the world.")


class ExampleValidationTest(unittest.TestCase):
    """Phase 8I: word and phrase morphology matching in _text_in_sentence."""

    def _check(self, text: str, sentence: str, allow_inflection: bool) -> bool:
        from app.services.hunyuan_example import _text_in_sentence
        return _text_in_sentence(text, sentence, allow_inflection=allow_inflection)

    # ── Exact match (both modes) ───────────────────────────────────
    def test_exact_word_match(self):
        self.assertTrue(self._check("crave", "She craves chocolate.", False))

    def test_exact_phrase_match(self):
        self.assertTrue(self._check("break a leg", "Break a leg at the interview!", False))

    # ── Single-word inflections (loose mode) ──────────────────────
    def test_crave_craves(self):
        self.assertTrue(self._check("crave", "She craves chocolate at night.", True))

    def test_crave_craved(self):
        self.assertTrue(self._check("crave", "He craved attention.", True))

    def test_crave_craving(self):
        self.assertTrue(self._check("crave", "He was craving attention.", True))

    def test_avoid_avoided(self):
        self.assertTrue(self._check("avoid", "She avoided the question.", True))

    def test_avoid_avoiding(self):
        self.assertTrue(self._check("avoid", "He is avoiding her.", True))

    def test_admire_admired(self):
        self.assertTrue(self._check("admire", "I admired his courage.", True))

    def test_admire_admiring(self):
        self.assertTrue(self._check("admire", "She was admiring the view.", True))

    def test_deadline_deadlines(self):
        self.assertTrue(self._check("deadline", "We have two deadlines this week.", True))

    # ── Phrase core-verb inflections (loose mode) ──────────────────
    def test_break_out_broke_out(self):
        self.assertTrue(self._check("break out", "A fire broke out last night.", True))

    def test_break_out_broken_out(self):
        self.assertTrue(self._check("break out", "The disease has broken out again.", True))

    def test_break_out_breaking_out(self):
        self.assertTrue(self._check("break out", "A conflict is breaking out.", True))

    def test_give_up_gave_up(self):
        self.assertTrue(self._check("give up", "She gave up smoking.", True))

    def test_give_up_given_up(self):
        self.assertTrue(self._check("give up", "He has given up the idea.", True))

    def test_pick_up_picked_up(self):
        self.assertTrue(self._check("pick up", "He picked up the phone.", True))

    def test_pick_up_picking_up(self):
        self.assertTrue(self._check("pick up", "She is picking up speed.", True))

    def test_come_across_came_across(self):
        self.assertTrue(self._check("come across", "I came across an old photo.", True))

    # ── Alphanumeric / hyphenated: exact match suffices ───────────
    def test_covid19_exact(self):
        self.assertTrue(self._check("COVID-19", "COVID-19 changed the way people work.", False))

    def test_gpt4_exact(self):
        self.assertTrue(self._check("GPT-4", "GPT-4 can answer questions.", False))

    def test_well_known_exact(self):
        self.assertTrue(self._check("well-known", "He is a well-known actor.", False))

    # ── Must NOT pass: pure synonym, phrase not present ───────────
    def test_crave_synonym_fails(self):
        self.assertFalse(self._check("crave", "She really wanted chocolate.", True))

    def test_break_out_synonym_fails(self):
        self.assertFalse(self._check("break out", "A fire started last night.", True))

    def test_break_a_leg_good_luck_fails(self):
        self.assertFalse(self._check("break a leg", "Good luck with your interview.", True))

    def test_commit_guilty_dispersed_fails(self):
        # "committing" and "guilty" both appear but not adjacent → must fail
        self.assertFalse(
            self._check("commit guilty", "He was found guilty of committing a crime.", True)
        )


if __name__ == "__main__":
    unittest.main()
