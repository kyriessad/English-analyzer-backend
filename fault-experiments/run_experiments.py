"""Single-request reliability fault experiments A/B/C against the live server.

Every request uses a *unique* text so results are never served from the AI
cache and never deduped by fingerprint: A/B/C are genuinely independent AI
calls, and C2 really blocks on the AI slot released by cancelled C1.

A: normal streaming request        -> start/field/final/done, ok=True
B: cancel mid-stream               -> classified as cancelled (never a 500)
C: cancel then immediately a 2nd   -> measures how fast the AI slot frees

Requires: server running on 127.0.0.1:8000, Ollama reachable, token in token.txt.
"""
import json
import sys
import time

import requests

BASE = "http://127.0.0.1:8000"
TOKEN = open("fault-experiments/token.txt").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# Distinct common English words: each run picks a fresh one per request so
# fingerprints are unique (no AI-cache hit, no keyless-dedup join).
_WORD_POOL = [
    "panorama", "luminous", "cascade", "vibrant", "meadow", "brittle",
    "harbor", "tranquil", "verdict", "satchel", "prism", "ember",
    "glacier", "willow", "bramble", "canyon", "locket", "zephyr",
    "monolith", "curtain", "fable", "mosaic", "orchard", "plume",
    "saffron", "thicket", "upland", "vortex", "wattle", "yonder",
]
_used: set[int] = set()


def _next_text() -> str:
    for i, w in enumerate(_WORD_POOL):
        if i not in _used:
            _used.add(i)
            return w
    # pool exhausted: fall back to a unique compound so no fingerprint collides
    return f"hill{tm_ns()}"  # pragma: no cover


def tm_ns() -> int:
    return time.time_ns()


def post_stream(text: str, timeout=150):
    # forceRefresh=True bypasses the AI result cache so every request does a
    # real Ollama call; combined with unique texts this keeps A/B/C independent
    # (no cache replay, no keyless-dedup join across runs).
    return requests.post(
        f"{BASE}/api/analyze-english/stream",
        headers=HEADERS,
        json={"text": text, "cardType": "word", "targetLang": "zh", "forceRefresh": True},
        stream=True,
        timeout=timeout,
    )


def _next_line(gen):
    for raw in gen:
        if raw:
            return json.loads(raw)
    return None


def first_line(resp, timeout=120):
    deadline = time.monotonic() + timeout
    gen = resp.iter_lines(decode_unicode=True)
    while time.monotonic() < deadline:
        try:
            raw = next(gen)
        except StopIteration:
            return None
        if raw:
            return json.loads(raw)
    return None


def drain(gen):
    """Consume the rest of an already-started iter_lines generator."""
    lines = []
    for raw in gen:
        if raw:
            lines.append(json.loads(raw))
    return lines


results: dict = {}

# ------------------------------------------------------------------ A. normal
text_a = _next_text()
t0 = time.perf_counter()
ra = post_stream(text_a)
req_a = ra.headers.get("X-Request-ID")
gen_a = ra.iter_lines(decode_unicode=True)
lines_a = [json.loads(r) for r in gen_a if r]
results["A"] = {
    "text": text_a,
    "request_id": req_a,
    "http_status": ra.status_code,
    "types": [ln["type"] for ln in lines_a],
    "final_ok": lines_a[-2]["data"].get("ok") if len(lines_a) >= 2 else None,
    "duration_s": round(time.perf_counter() - t0, 3),
}
print(f"A {req_a} status={ra.status_code} types={results['A']['types']} ok={results['A']['final_ok']} dur={results['A']['duration_s']}s", flush=True)

# ------------------------------------------------------------------ B. cancel
text_b = _next_text()
rb = post_stream(text_b)
req_b = rb.headers.get("X-Request-ID")
first_b = first_line(rb)
tb_close = time.perf_counter()
rb.close()  # simulate client disconnect right after the first NDJSON line
time.sleep(1.5)  # give the server time to propagate the disconnect
results["B"] = {
    "text": text_b,
    "request_id": req_b,
    "http_status": rb.status_code,
    "first_line_type": first_b["type"] if first_b else None,
}
print(f"B {req_b} status={rb.status_code} first={results['B']['first_line_type']}", flush=True)

# --------------- C. cancel, then immediately send a second request ------------
text_c1 = _next_text()
rc = post_stream(text_c1)
req_c1 = rc.headers.get("X-Request-ID")
first_c1 = first_line(rc)
tc_close = time.perf_counter()
rc.close()
# Second request with a DIFFERENT unique text: no cache, no dedup -> blocks on
# the AI slot until cancelled C1 releases it.
text_c2 = _next_text()
r2 = post_stream(text_c2)
tc_headers = time.perf_counter() - tc_close
req_c2 = r2.headers.get("X-Request-ID")
gen_c2 = r2.iter_lines(decode_unicode=True)
first_c2 = _next_line(gen_c2)
lines_c2 = drain(gen_c2)
results["C"] = {
    "text_c1": text_c1,
    "request_id_c1": req_c1,
    "http_status_c1": rc.status_code,
    "first_line_type_c1": first_c1["type"] if first_c1 else None,
    "text_c2": text_c2,
    "request_id_c2": req_c2,
    "http_status_c2": r2.status_code,
    "c2_seconds_to_headers_after_close": round(tc_headers, 3),
    "c2_first_line_type": first_c2["type"] if first_c2 else None,
    "c2_types": [ln["type"] for ln in lines_c2],
    "c2_final_ok": lines_c2[-2]["data"].get("ok") if len(lines_c2) >= 2 else None,
}
print(
    f"C c1={req_c1} c2={req_c2} c2_headers_after_close={results['C']['c2_seconds_to_headers_after_close']}s "
    f"c2_types={results['C']['c2_types']} c2_ok={results['C']['c2_final_ok']}",
    flush=True,
)

print(json.dumps(results, indent=2, ensure_ascii=False))
