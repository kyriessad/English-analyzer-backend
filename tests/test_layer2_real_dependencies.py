"""Opt-in Layer 2 checks using installed language and speech resources."""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.core.config import settings as app_settings
from app.services import harper_service, piper_service, validator
from app.services.ecdict_service import (
    dictionary_available,
    get_dictionary_distractor_entries,
    get_dictionary_entry,
    get_dictionary_translations,
    get_phonetic,
)


RUN_LAYER2 = os.environ.get("RUN_LAYER2") == "1"
pytestmark = [
    pytest.mark.layer2,
    pytest.mark.skipif(not RUN_LAYER2, reason="set RUN_LAYER2=1 to run real dependencies"),
]


def _source(evidence: list[dict], name: str) -> dict:
    return next(item for item in evidence if item.get("source") == name)


def test_layer2_real_ecdict_exact_case_miss_phonetic_pos_translation_and_distractors():
    assert dictionary_available()
    lower = get_dictionary_entry("because")
    upper = get_dictionary_entry("  BECAUSE  ")
    assert lower is not None and upper is not None
    assert lower.word.lower() == upper.word.lower() == "because"
    assert lower.meanings and lower.translation and lower.pos
    assert get_phonetic("because")
    assert get_phonetic("definitely-not-in-ecdict-layer-two") is None
    assert get_dictionary_translations("because")

    distractors = get_dictionary_distractor_entries(
        exclude_words={"because"},
        exclude_meanings=set(lower.meanings),
        pos=lower.pos,
        frq=lower.frq,
        bnc=lower.bnc,
        limit=8,
    )
    assert len(distractors) >= 3
    assert all(item.word.lower() != "because" for item in distractors)
    assert all(item.meanings and item.meanings[0] not in lower.meanings for item in distractors)


@pytest.mark.parametrize(
    ("text", "expected_result", "expected_distance"),
    [
        ("because", "exact", 0),
        ("becuase", "suggestion", 1),
        ("becasee", "no_suggestion", None),
    ],
)
def test_layer2_real_symspell_exact_distance_one_and_two(text, expected_result, expected_distance):
    evidence = validator._get_symspell_evidence(text, "word", [text])
    assert evidence["result"] == expected_result
    if expected_result == "suggestion":
        assert evidence["distance"] == expected_distance


def test_layer2_real_symspell_unavailable_then_recovers(monkeypatch):
    validator._get_symspell.cache_clear()
    assert validator._get_symspell() is not None
    monkeypatch.setattr(validator, "_get_symspell", lambda: None)
    unavailable = validator._get_symspell_evidence("becuase", "word", ["becuase"])
    assert unavailable["result"] == "unavailable"
    monkeypatch.undo()
    validator._get_symspell.cache_clear()
    recovered = validator._get_symspell_evidence("becuase", "word", ["becuase"])
    assert recovered["result"] == "suggestion"


class _FaultHandler(BaseHTTPRequestHandler):
    mode = "503"

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length", "0")))
        if self.mode == "timeout":
            time.sleep(0.2)
        if self.mode == "503":
            payload, status = b'{"error":"controlled unavailable"}', 503
        elif self.mode == "malformed":
            payload, status = b'{not-json', 200
        else:
            payload, status = json.dumps({"lints": []}).encode("utf-8"), 200
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        return


@contextmanager
def _fault_server(mode: str):
    handler = type("FaultHandler", (_FaultHandler,), {"mode": mode})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _harper_settings(base_url: str, *, enabled: bool = True, timeout: float = 0.5):
    return replace(
        app_settings,
        harper_enabled=enabled,
        harper_base_url=base_url,
        harper_timeout_seconds=timeout,
    )


def test_layer2_real_harper_healthy_spelling_grammar_punctuation_usage_and_multiple():
    base_url = os.environ.get("HARPER_BASE_URL", "http://127.0.0.1:8082")
    original = harper_service.settings
    harper_service.settings = _harper_settings(base_url)
    try:
        cases = (
            "I goed there yesterday.",
            "This are not right.",
            "Really???",
            "I could of done that.",
            "This are definately not right!!!",
        )
        outputs = [harper_service.get_harper_evidence(text, "sentence") for text in cases]
    finally:
        harper_service.settings = original

    assert all(_source(evidence, "harper")["result"] != "unavailable" for evidence, _ in outputs)
    assert any(warnings for _, warnings in outputs)
    assert sum(len(warnings) for _, warnings in outputs) >= 4
    lint_types = {
        item["type"]
        for evidence, _ in outputs
        for item in evidence
        if item.get("result") == "lint"
    }
    assert len(lint_types) >= 2


@pytest.mark.parametrize("mode", ["503", "timeout", "malformed"])
def test_layer2_harper_real_http_faults_fail_open_and_recover(mode):
    original = harper_service.settings
    with _fault_server(mode) as base_url:
        harper_service.settings = _harper_settings(
            base_url,
            timeout=0.05 if mode == "timeout" else 0.5,
        )
        try:
            evidence, warnings = harper_service.get_harper_evidence(
                "I really like this movie.", "sentence"
            )
        finally:
            harper_service.settings = original
    assert warnings == []
    assert _source(evidence, "harper")["result"] == "unavailable"

    recovered, _ = harper_service.get_harper_evidence(
        "I really like this movie.", "sentence"
    )
    assert _source(recovered, "harper")["result"] != "unavailable"


def test_layer2_harper_disabled_and_stopped_fail_open():
    original = harper_service.settings
    try:
        harper_service.settings = _harper_settings("http://127.0.0.1:1", enabled=False)
        disabled, _ = harper_service.get_harper_evidence("This is a sentence.", "sentence")
        assert _source(disabled, "harper")["result"] == "skipped"

        harper_service.settings = _harper_settings("http://127.0.0.1:1", timeout=0.05)
        stopped, _ = harper_service.get_harper_evidence("This is a sentence.", "sentence")
        assert _source(stopped, "harper")["result"] == "unavailable"
    finally:
        harper_service.settings = original


def test_layer2_real_piper_known_phrase_sentence_voices_and_cache(tmp_path):
    assert piper_service.pronunciation_available_all() == {"male": True, "female": True}
    original = piper_service.settings
    piper_service.settings = replace(app_settings, piper_audio_cache_dir=str(tmp_path))
    try:
        outputs = []
        for text, voice in (
            ("because", "male"),
            ("give up", "female"),
            ("I love English.", "male"),
        ):
            output = piper_service.synthesize_or_get_cached_audio(text, voice=voice)
            data = output.read_bytes()
            assert len(data) > 44 and data.startswith(b"RIFF") and data[8:12] == b"WAVE"
            outputs.append(output)

        cached = piper_service.get_cached_audio("because", voice="male")
        assert cached == outputs[0]
        assert piper_service.synthesize_or_get_cached_audio("because", voice="male") == outputs[0]
    finally:
        piper_service.settings = original
