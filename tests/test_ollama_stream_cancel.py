import json
import unittest
from unittest.mock import patch


class _FakeResponse:
    def __init__(self, lines, *, status_code=200):
        self.status_code = status_code
        self._lines = iter(lines)
        self.closed = False

    def iter_lines(self, decode_unicode=False):
        return self._lines

    def close(self):
        self.closed = True


class _FakeDirectResponse:
    def __init__(self, content):
        self.status_code = 200
        self.text = ""
        self._content = content

    def json(self):
        return {"response": self._content}


def _ollama_lines(content, *, chunk_sizes=(1, 2, 3, 5, 8)):
    pieces = []
    start = 0
    size_index = 0
    while start < len(content):
        size = chunk_sizes[size_index % len(chunk_sizes)]
        pieces.append(content[start : start + size])
        start += size
        size_index += 1
    lines = [
        json.dumps({"response": piece, "done": False}, ensure_ascii=False).encode("utf-8")
        for piece in pieces
    ]
    lines.append(json.dumps({"response": "", "done": True}).encode("utf-8"))
    return lines


def _analysis_data(*, example_sentence="I crave hot soup after a long winter walk."):
    return {
        "meaning": "渴望继续\\学习\n并说：\"好\" 😀",
        "expressionType": "literal",
        "alternativeMeanings": [{"meaning": "嵌套内容不应流出", "type": "literal", "note": "仅测试"}],
        "usageScenario": "表达非常想要某物时。",
        "exampleSentence": example_sentence,
        "exampleTranslation": "冬日长途散步后，我很想喝热汤。",
        "dialogue": {"english": ["A: What do you want?", "B: I crave soup."], "chinese": ["A：你想吃什么？", "B：我想喝汤。"]},
        "synonyms": [{"english": "desire", "chinese": "渴望"}],
        "similarPhrases": [{"english": "long for", "chinese": "渴望"}],
    }


class IncrementalJsonStringTest(unittest.TestCase):
    def test_every_character_boundary_decodes_escapes_without_loss_or_duplicates(self):
        from app.services.ollama_example import (
            _STREAM_DELTA_FIELDS,
            _extract_streamable_string_prefixes,
        )

        data = _analysis_data()
        # ASCII escaping forces Chinese and the emoji through partial \\uXXXX
        # sequences, including a split UTF-16 surrogate pair.
        raw = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
        previous = {field: "" for field in _STREAM_DELTA_FIELDS}
        additions = {field: [] for field in _STREAM_DELTA_FIELDS}

        for end in range(1, len(raw) + 1):
            prefixes = _extract_streamable_string_prefixes(raw[:end])
            for field, current in prefixes.items():
                self.assertTrue(current.startswith(previous[field]))
                additions[field].append(current[len(previous[field]) :])
                previous[field] = current

        for field in _STREAM_DELTA_FIELDS:
            self.assertEqual(data[field], "".join(additions[field]))
        self.assertNotIn("嵌套内容不应流出", "".join(additions["meaning"]))

    def test_arbitrary_model_chunks_emit_deltas_and_authoritative_final(self):
        from app.services.ollama_example import (
            generate_analysis_with_ollama,
            generate_analysis_with_ollama_stream,
        )

        data = _analysis_data()
        content = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
        fake = _FakeResponse(_ollama_lines(content))
        attempts = []

        with patch("app.services.ollama_example._post_generate_stream", return_value=fake):
            events = list(
                generate_analysis_with_ollama_stream(
                    "crave",
                    "word",
                    attempt_recorder=lambda: attempts.append(1),
                )
            )
        with patch(
            "app.services.ollama_example._post_generate",
            return_value=_FakeDirectResponse(content),
        ):
            direct_result = generate_analysis_with_ollama("crave", "word")

        deltas = [event for event in events if event[0] == "delta"]
        for field in ("meaning", "usageScenario", "exampleSentence", "exampleTranslation"):
            self.assertEqual(data[field], "".join(event[2] for event in deltas if event[1] == field))
        self.assertEqual(list(range(1, len(deltas) + 1)), [event[3] for event in deltas])
        self.assertTrue(all(event[4] == 1 for event in deltas))

        final = events[-1]
        self.assertEqual("result", final[0])
        self.assertEqual(1, final[2])
        self.assertEqual(data, final[1])
        self.assertEqual(direct_result, final[1])
        self.assertEqual([1], attempts)
        self.assertNotIn("reset", [event[0] for event in events])
        self.assertTrue(fake.closed)

    def test_retryable_validation_failure_resets_before_strict_attempt(self):
        from app.services.ollama_example import generate_analysis_with_ollama_stream

        invalid = json.dumps(_analysis_data(example_sentence="crave"), ensure_ascii=False)
        valid_data = _analysis_data()
        valid = json.dumps(valid_data, ensure_ascii=False)
        responses = iter([
            _FakeResponse(_ollama_lines(invalid, chunk_sizes=(7,))),
            _FakeResponse(_ollama_lines(valid, chunk_sizes=(7,))),
        ])
        payloads = []
        attempts = []

        def fake_post(payload, deadline):
            payloads.append(payload)
            return next(responses)

        with patch("app.services.ollama_example._post_generate_stream", side_effect=fake_post):
            events = list(
                generate_analysis_with_ollama_stream(
                    "crave",
                    "word",
                    attempt_recorder=lambda: attempts.append(1),
                )
            )

        reset_index = events.index(("reset", 2))
        self.assertTrue(any(event[0] == "delta" and event[4] == 1 for event in events[:reset_index]))
        self.assertTrue(any(event[0] == "delta" and event[4] == 2 for event in events[reset_index + 1 :]))
        self.assertEqual(("result", valid_data, 2), events[-1])
        self.assertEqual([1, 1], attempts)
        self.assertEqual(2, len(payloads))
        self.assertNotIn("This is a retry", payloads[0]["prompt"])
        self.assertIn("This is a retry", payloads[1]["prompt"])

        sequences = [event[3] for event in events if event[0] == "delta"]
        self.assertEqual(list(range(1, len(sequences) + 1)), sequences)

    def test_transport_failure_does_not_start_strict_attempt(self):
        from app.services.ollama_example import generate_analysis_with_ollama_stream

        attempts = []
        fake = _FakeResponse([], status_code=503)
        with patch("app.services.ollama_example._post_generate_stream", return_value=fake) as post:
            events = list(
                generate_analysis_with_ollama_stream(
                    "crave",
                    "word",
                    attempt_recorder=lambda: attempts.append(1),
                )
            )

        self.assertEqual([("result", None, 1)], events)
        self.assertEqual([1], attempts)
        self.assertEqual(1, post.call_count)
        self.assertTrue(fake.closed)


class OllamaStreamCancelTest(unittest.TestCase):
    def test_cancelled_analysis_never_finalizes_or_writes_success_cache(self):
        from app.services.analyzer import analyze_text_streaming

        def cancelled_stream(*_args, **_kwargs):
            yield ("delta", "meaning", "临时", 1, 1)
            yield ("cancelled", None)

        with (
            patch(
                "app.services.analyzer._validation_context",
                return_value=({"level": "ok"}, "sentence", "pass", "I'm down.", [], []),
            ),
            patch(
                "app.services.analyzer.generate_analysis_with_ollama_stream",
                side_effect=cancelled_stream,
            ),
            patch("app.services.analyzer._finalize_analysis") as finalize,
            patch("app.services.analyzer.set_cache") as set_cache,
        ):
            generator = analyze_text_streaming("I'm down.", force_refresh=True)
            self.assertEqual(("delta", "meaning", "临时", 1, 1), next(generator))
            with self.assertRaises(GeneratorExit):
                next(generator)

        finalize.assert_not_called()
        set_cache.assert_not_called()

    def test_controller_cancel_after_delta_stops_further_body_events(self):
        from app.services.ollama_example import generate_analysis_with_ollama_stream
        from app.services.request_reliability import StreamCancelController

        first_piece = '{"meaning":"渴'
        remaining_piece = '望","exampleSentence":"I crave soup every cold evening."}'
        fake = _FakeResponse(
            [
                json.dumps({"response": first_piece, "done": False}, ensure_ascii=False).encode("utf-8"),
                json.dumps({"response": remaining_piece, "done": True}, ensure_ascii=False).encode("utf-8"),
            ]
        )
        controller = StreamCancelController()

        with patch("app.services.ollama_example._post_generate_stream", return_value=fake):
            gen = generate_analysis_with_ollama_stream(
                "crave",
                "word",
                cancel_controller=controller,
            )
            first = next(gen)
            self.assertEqual(("delta", "meaning", "渴", 1, 1), first)
            controller.cancel()
            remaining = list(gen)

        self.assertEqual([("cancelled", None)], remaining)
        self.assertTrue(fake.closed)

    def test_generator_close_closes_underlying_ollama_stream(self):
        """Aborting the stream (GeneratorExit) must run response.close().

        This is the backend half of the cancel feature: when the client aborts
        the NDJSON response, the generator chain is closed, which must release
        the underlying Ollama HTTP stream rather than leaking the connection.
        """
        from app.services.ollama_example import generate_analysis_with_ollama_stream

        line = json.dumps({
            "response": json.dumps({"meaning": "渴望"}, ensure_ascii=False),
            "done": False,
        }).encode("utf-8")

        fake = _FakeResponse([line])

        with patch("app.services.ollama_example._post_generate_stream", return_value=fake):
            gen = generate_analysis_with_ollama_stream("crave", "word")
            first = next(gen)
            self.assertEqual("delta", first[0])
            self.assertFalse(fake.closed)
            gen.close()

        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
