# LEVEL 7 REAL WECHAT CLIENT E2E REPORT

Date: 2026-08-26

## 1. Scope And Architecture

The real-client runs used WeChat Developer Tools, `miniprogram-automator`, real Mini Program pages, real `wx.login`, real `wx.request`, the isolated FastAPI listener on `127.0.0.1:18000`, isolated PostgreSQL database `english_analyzer_phase1_e2e`, Ollama transport `127.0.0.1:18114`, and isolated Piper assets/cache.

The protocol/load runs used the same isolated backend dependencies but did not use WeChat pages. They are reported separately and are not real-client multi-user results.

## 2. Real Client Evidence

### CLIENT PAGE CONTROL: PASS

The client runner reached `pages/index/index` through DevTools automation, used `currentPage`, `reLaunch`, page selectors, tap/input actions, and captured non-empty page screenshots.

### REAL CLIENT CORE: PASS

Artifact: `.e2e-artifacts/20260826T072147Z-2d49f8/`

Verified through the real page:

- `wx.login` returned a real code; the code value was never written.
- `/api/auth/wechat-login` returned 200 and created the isolated PostgreSQL user.
- JWT was saved in namespaced real Mini Program Storage.
- Card create/read/edit, sync replay, AI stream, TTS playback, Review, logout, and 401 recovery completed.
- AI had progressive page updates, `wx.onChunkReceived` events, Qwen `qwen3:8b`, and a final event.
- TTS observed loading, playback, valid WAV response, and no client pronunciation errors.
- 401 recovery recorded backend 401 request IDs, performed real relogin, then completed protected 200 requests.

### REAL CLIENT CARD CRUD: PASS

Artifact: `.e2e-artifacts/20260826T072441Z-ff29dc/`

Three complete UI rounds passed: Create -> Read -> Update -> Delete. PostgreSQL showed exactly one real user, three owned cards, no duplicate records, and no active cards after deletion. FastAPI POST/PATCH/DELETE logs contained request IDs.

## 3. Real User Identity Capacity

### REAL USER IDENTITY CAPACITY = 1

Three runtime projects and automation ports `19420`, `19421`, and `19422` each produced a real login code, but all mapped to the same openid hash and backend user ID.

Therefore:

- REAL MULTI-USER CLIENT E2E: `BLOCKED`
- The following are not honestly labeled real multi-user: 5, 10, 25, 50, or 100 clients.
- Multiple real DevTools instances with one signed-in identity are single-identity concurrency only.
- Expanding capacity requires additional real WeChat tester identities or an officially supported identity mechanism.

Identity artifact: `.e2e-artifacts/20260826T073025Z-c19135/REPORT.md`

## 4. Scale Matrix

| Level | Real client result | Identity count | Classification | Result |
|---:|---|---:|---|---|
| 1 | Complete UI chain and CRUD | 1 | REAL CLIENT / SINGLE IDENTITY | PASS |
| 5 | Backend protocol load only | 1 available, not 5 | PROTOCOL / BACKEND LOAD | PASS |
| 10 | Backend protocol load only | 1 available, not 10 | PROTOCOL / BACKEND LOAD | PASS |
| 25 | Not run | 1 available | REAL MULTI-USER PLATFORM BLOCKED | NOT TESTED |
| 50 | Not run | 1 available | REAL MULTI-USER PLATFORM BLOCKED | NOT TESTED |
| 100 | Backend protocol load only | 1 available, not 100 | PROTOCOL / BACKEND LOAD | PASS |

The existing backend load experiment also ran a 30-user level; it was not a real-client run and is retained as protocol evidence.

## 5. Protocol / Backend Load Results

Artifact: `.e2e-artifacts/20260826T073744Z-099ca9/REPORT.md`

- 5: 27/27 HTTP 200, PASS.
- 10: 54/54 HTTP 200, PASS.
- 30: 160/160 HTTP 200, PASS.
- 100: 374 HTTP 200, 87 HTTP 503, PASS under configured admission/capacity behavior.
- HTTP burst: 30 accepted, 71 fast 503, recovery 200, PASS.
- AI burst: queue limits and follower limits exercised, PASS.
- TTS burst: 3 successful and 5 queue-full 503 responses, PASS.
- Mixed backend business load: PASS for the implemented protocol scenario.

At 100, the observed boundary was controlled rejection, not data corruption. Peak observed resources included DB checkout 15 plus overflow 10, AI active 1 with waiting 2, and TTS active 1. Final DB/AI/TTS resource gauges returned to zero.

## 6. Isolation

### USER ISOLATION: PASS

The backend isolation experiment proved cross-user read, patch, delete, sync/replay, and review-feedback attacks returned 404 with request IDs; the victim remained unchanged and cross-user review rows were zero.

This is real backend authorization evidence. It is not a two-real-WeChat-account client test because identity capacity is one.

## 7. Fault And Recovery

### PROTOCOL FAULT TESTS: PASS

- Ollama E2E transport disabled: AI returned explicit business failure; Card, Review, and TTS remained available; AI recovered after transport restoration.
- Piper isolated model links disabled: TTS returned 503; Auth, Review, and AI remained available; valid WAV returned after restoration.
- Isolated DB pool exhausted: `DB_POOL_TIMEOUT` 503 occurred near 3 seconds; holders released; `/api/auth/me` recovered 200; no idle-in-transaction leak.

### REAL CLIENT OFFLINE / COMPONENT FAULT TESTS: NOT TESTED

The real-client chain did test token invalidation and 401 recovery. OS/network offline, client-side stream interruption, DevTools mid-run close, and real-client FastAPI/Piper/Ollama outage actions were not executed in this continuation.

## 8. Product Bugs

### PRODUCT BUG FOUND: FAIL

The existing backend report identifies mojibake in async AI queue-full/timeout Chinese error messages. Capacity counters, status codes, request IDs, and cleanup were correct, but the user-facing error contract is corrupted. It was not changed because business-code modification was outside scope.

## 9. Harness Bugs Fixed

- Recovery assertion incorrectly required two client-observed 401 events although one was sufficient for the scenario; backend logs now provide authoritative 401 request IDs.
- Recovery validation used client console observation for the 401 itself even when DevTools console capture missed that callback.
- Final resource validation treated the `/metrics` request's own transient `http_requests_in_progress=1` as a leak; DB/AI/TTS gauges remain strict and must return to zero.

## 10. Cleanup And Formal Environment

All continuation runs recorded runtime paths, automation ports, and owned process information. Cleanup passed for each run. Final checks found no listeners on `8000`, `18000`, `18114`, `19420`, `19421`, or `19422`. The isolated database was dropped after each run. Formal `.env`, formal PostgreSQL database `english_analyzer`, formal TTS assets/cache, formal port 8000, and production user data were untouched.

## 11. Final Status

- REAL CLIENT PAGE CONTROL: `PASS`
- REAL CLIENT AUTH / wx.login / wx.request: `PASS`
- REAL CLIENT CARD CRUD: `PASS`
- REAL CLIENT AI STREAMING: `PASS`
- REAL CLIENT TTS: `PASS`
- REAL CLIENT REVIEW: `PASS`
- REAL CLIENT 401 RECOVERY: `PASS`
- REAL USER IDENTITY CAPACITY: `1`
- REAL MULTI-USER CLIENT E2E: `BLOCKED`
- PROTOCOL / BACKEND LOAD 5/10/30/100: `PASS`
- PROTOCOL FAULT / RECOVERY: `PASS`
- REAL CLIENT OFFLINE AND COMPONENT FAULTS: `NOT TESTED`
- Overall Level 7 continuation: `PARTIAL`

The result is intentionally `PARTIAL`: the real single-identity client chain is proven, backend capacity/fault behavior is proven, but platform identity capacity prevents claiming real multi-user client E2E at 5/10/25/50/100.
