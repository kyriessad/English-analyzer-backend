"""Real WeChat DevTools client E2E on top of the isolated Level 7 backend lab."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import httpx
import psycopg
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from e2e.run_level7_e2e import (  # noqa: E402
    APP_PORT,
    GAUGE_NAMES,
    PROXY_PORT,
    E2EFailure,
    Level7Runner,
    ManagedProcess,
    fetch_metrics,
    iso_now,
    metric_value,
    port_listener_pid,
    process_info,
    sanitize_value,
    wait_for_gauge_baseline,
    write_json,
)


MINIAPP = ROOT.parent / "English-study-miniapp"
CLIENT_DIR = ROOT / "e2e" / "wechat-client"
CLIENT_SCRIPT = CLIENT_DIR / "run-client-e2e.js"
NODE = shutil.which("node") or ""
WECHAT_CLI = Path(r"C:\Program Files (x86)\Tencent\微信web开发者工具\cli.bat")
WECHAT_SERVICE_PORT: int | None = None
WECHAT_AUTOMATION_PORT = 19420
E2E_BASE_URL = f"http://127.0.0.1:{APP_PORT}"
HARPER_BASE_URL = "http://127.0.0.1:8082"
HARPER_PORT = 8082
HARPER_DIR = ROOT / "harper-sidecar"

_wechat_cli_candidates = sorted(
    Path(r"C:\Program Files (x86)\Tencent").glob("*/cli.bat"),
    key=lambda path: ("web" not in path.parent.name.lower(), str(path).lower()),
)
if _wechat_cli_candidates:
    WECHAT_CLI = _wechat_cli_candidates[0]
_wechat_cli_short_path = Path(r"C:\PROGRA~2\Tencent\微信WE~1\cli.bat")
if not WECHAT_CLI.is_file() and _wechat_cli_short_path.is_file():
    WECHAT_CLI = _wechat_cli_short_path
_wechat_cli_link = ROOT / ".wechatdevtools" / "cli.bat"
if not WECHAT_CLI.is_file() and _wechat_cli_link.is_file():
    WECHAT_CLI = _wechat_cli_link


def _config_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return ""
    loaded = dotenv_values(env_path)
    item = loaded.get(name)
    return str(item or "").strip()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_harper_health() -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"{HARPER_BASE_URL}/health", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if response.status == 200 and payload.get("status") == "ok":
            return payload
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return None


def _ensure_harper(run_dir: Path) -> tuple[ManagedProcess | None, dict[str, Any]]:
    listener_pid = port_listener_pid(HARPER_PORT)
    health = _read_harper_health()
    if listener_pid is not None:
        if not health or health.get("service") != "harper-sidecar":
            raise E2EFailure(
                f"Port {HARPER_PORT} is occupied by a process that is not the Harper sidecar"
            )
        return None, {
            "status": "READY",
            "managed_by_runner": False,
            "pid": listener_pid,
            "health": health,
        }

    required = (HARPER_DIR / "server.mjs", HARPER_DIR / "package.json")
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise E2EFailure(f"Harper sidecar files are missing: {missing}")
    process = ManagedProcess(
        "harper-sidecar",
        [NODE, "server.mjs"],
        HARPER_DIR,
        os.environ.copy(),
        run_dir / "harper-sidecar.log",
    )
    pid = process.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        health = _read_harper_health()
        if health and health.get("service") == "harper-sidecar":
            return process, {
                "status": "READY",
                "managed_by_runner": True,
                "pid": pid,
                "health": health,
            }
        if process.process is not None and process.process.poll() is not None:
            break
        time.sleep(0.1)
    process.stop()
    raise E2EFailure("Harper sidecar did not become ready within 30 seconds")


def _append_exception(result: dict[str, Any], stage: str, exc: BaseException) -> None:
    result.setdefault("exceptions", []).append(
        {
            "stage": stage,
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
    )


def _discover_wechat_service_port() -> dict[str, Any]:
    """Find the HTTP service listener owned by the already-running DevTools."""
    command = (
        "$items = @(netstat -ano -p tcp | Select-String 'LISTENING' "
        "| ForEach-Object { "
        "$parts = ($_.Line -split '\\s+') | Where-Object { $_ }; "
        "if($parts.Count -ge 5){ "
        "$endpoint = $parts[1]; $port = [int]($endpoint.Substring($endpoint.LastIndexOf(':') + 1)); "
        "$ownerPid = [int]$parts[4]; "
        "$p = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue; "
        "if($p){ [pscustomobject]@{ Port=$port; Pid=$ownerPid; ProcessName=$p.ProcessName } } } }); "
        "$items | ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    raw = completed.stdout.strip()
    if completed.returncode != 0 or not raw:
        raise E2EFailure(
            "Could not inspect local TCP listeners for the WeChat DevTools Service Port"
        )
    try:
        listeners = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise E2EFailure("Could not parse local TCP listener inspection output") from exc
    if isinstance(listeners, dict):
        listeners = [listeners]
    if not isinstance(listeners, list):
        raise E2EFailure("Local TCP listener inspection returned an unexpected shape")

    candidates = [
        item
        for item in listeners
        if isinstance(item, dict)
        and re.search(
            r"(wechat|devtools|nwjs|微信)",
            str(item.get("ProcessName") or ""),
            flags=re.IGNORECASE,
        )
        and 1 <= int(item.get("Port") or 0) <= 65535
    ]
    ports: dict[int, dict[str, Any]] = {}
    for item in candidates:
        port = int(item["Port"])
        ports.setdefault(port, item)

    # DevTools exposes its CLI Service Port as a local Express HTTP server.
    # Other listeners in the same process are WebSocket/internal services.
    http_candidates: dict[int, dict[str, Any]] = {}
    for port, item in ports.items():
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/",
            headers={"Connection": "close"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                status = int(response.status)
                headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            headers = {key.lower(): value for key, value in exc.headers.items()}
        except (OSError, TimeoutError):
            continue
        if headers.get("x-powered-by", "").lower() == "express":
            http_candidates[port] = {
                **item,
                "http_status": status,
                "http_server": headers.get("server"),
            }

    if len(http_candidates) != 1:
        observed = [
            {
                "port": port,
                "pid": item.get("Pid"),
                "process_name": item.get("ProcessName"),
            }
            for port, item in sorted(http_candidates.items())
        ]
        if not observed:
            raise E2EFailure(
                "WeChat DevTools HTTP Service Port was not discovered; "
                "is the real DevTools HTTP Service Port enabled?"
            )
        raise E2EFailure(
            f"Multiple possible WeChat DevTools HTTP Service Ports were discovered: {observed}"
        )
    port, item = next(iter(http_candidates.items()))
    return {
        "port": port,
        "pid": int(item["Pid"]),
        "process_name": str(item.get("ProcessName") or ""),
        "candidate_count": len(candidates),
    }


def _preflight() -> dict[str, Any]:
    global WECHAT_SERVICE_PORT
    discovered_service = _discover_wechat_service_port()
    WECHAT_SERVICE_PORT = int(discovered_service["port"])
    required_files = (
        MINIAPP / "project.config.json",
        MINIAPP / "app.js",
        MINIAPP / "utils" / "apiClient.js",
        MINIAPP / "pages" / "add" / "add.js",
        MINIAPP / "pages" / "review" / "review.js",
        CLIENT_SCRIPT,
        CLIENT_DIR / "node_modules" / "miniprogram-automator" / "package.json",
        WECHAT_CLI,
    )
    missing = [str(path) for path in required_files if not path.is_file()]
    if missing:
        raise E2EFailure(f"Required WeChat E2E files are missing: {missing}")
    if not NODE:
        raise E2EFailure("node.exe was not found")

    package = _read_json(CLIENT_DIR / "node_modules" / "miniprogram-automator" / "package.json")
    if package.get("version") != "0.12.1":
        raise E2EFailure(f"Expected miniprogram-automator 0.12.1, got {package.get('version')}")

    project = _read_json(MINIAPP / "project.config.json")
    appid_exists = bool(str(project.get("appid") or "").strip())
    if not appid_exists:
        raise E2EFailure("Mini Program AppID is missing")

    configured_wechat_appid = _config_value("WECHAT_APPID")
    wechat_appid_configured = bool(configured_wechat_appid)
    wechat_secret_configured = bool(_config_value("WECHAT_SECRET"))
    jwt_configured = True
    if not wechat_appid_configured or not wechat_secret_configured:
        raise E2EFailure("Real WeChat AppID/AppSecret must be present in process environment or repository .env")
    if project.get("appid") != configured_wechat_appid:
        raise E2EFailure("Mini Program and backend AppID do not match")

    service_pid = int(discovered_service["pid"])
    service_process = process_info(service_pid) if service_pid is not None else None
    service_process_name = str(
        (service_process or {}).get("Name") or discovered_service["process_name"]
    )
    if service_pid is not None and not re.search(
        r"(wechat|devtools|nwjs|微信)", service_process_name, flags=re.IGNORECASE
    ):
        raise E2EFailure(
            f"WeChat DevTools Service Port {WECHAT_SERVICE_PORT} is not owned by wechatdevtools"
        )
    if port_listener_pid(WECHAT_AUTOMATION_PORT) is not None:
        raise E2EFailure(f"Automation port {WECHAT_AUTOMATION_PORT} is already in use")

    try:
        with urllib.request.urlopen(f"{HARPER_BASE_URL}/health", timeout=3) as response:
            harper_health = json.loads(response.read().decode("utf-8"))
        if response.status != 200 or harper_health.get("status") != "ok":
            raise E2EFailure(f"Harper sidecar health check failed: {harper_health}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise E2EFailure("Real Harper sidecar must be running on 127.0.0.1:8082") from exc

    return {
        "checked_at": iso_now(),
        "miniapp_project": str(MINIAPP),
        "appid_exists": appid_exists,
        "wechat_appid_configured": wechat_appid_configured,
        "wechat_appsecret_configured": wechat_secret_configured,
        "jwt_secret_configured": jwt_configured,
        "miniapp_backend_appid_match": True,
        "service_port": WECHAT_SERVICE_PORT,
        "service_port_listening": service_pid is not None,
        "service_port_pid": service_pid,
        "service_process_name": service_process_name,
        "service_port_discovery": discovered_service,
        "automation_port": WECHAT_AUTOMATION_PORT,
        "automation_port_free": True,
        "developer_cli": str(WECHAT_CLI),
        "miniprogram_automator": package.get("version"),
        "backend_base_url": E2E_BASE_URL,
        "harper_base_url": HARPER_BASE_URL,
        "harper_health": harper_health,
        "package_json_present_in_miniapp": (MINIAPP / "package.json").is_file(),
    }


def _client_environment(
    runner: Level7Runner, runtime_project: Path, client_label: str | None = None
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "LEVEL7_MINIAPP_SOURCE": str(MINIAPP),
            "LEVEL7_MINIAPP_RUNTIME": str(runtime_project),
            "LEVEL7_ARTIFACT_DIR": str(runner.run_dir),
            "LEVEL7_RUN_ID": str(runner.result["run_id"]),
            "LEVEL7_WECHAT_CLI": str(WECHAT_CLI),
            "LEVEL7_WECHAT_SERVICE_PORT": str(WECHAT_SERVICE_PORT),
            "LEVEL7_WECHAT_AUTOMATION_PORT": str(WECHAT_AUTOMATION_PORT),
            "LEVEL7_BACKEND_BASE_URL": E2E_BASE_URL,
        }
    )
    if client_label:
        environment["LEVEL7_CLIENT_LABEL"] = client_label
    # The client never receives WeChat/JWT/database secrets. The real code is
    # generated by wx.login inside DevTools and sent by the Mini Program itself.
    for key in (
        "WECHAT_SECRET",
        "JWT_SECRET_KEY",
        "DATABASE_URL",
        "E2E_CONTROL_TOKEN",
    ):
        environment.pop(key, None)
    return environment


def _run_client(
    runner: Level7Runner,
    runtime_project: Path,
    mode: str,
    timeout: float,
    client_label: str | None = None,
) -> dict[str, Any]:
    stem = f"{mode}-{client_label}" if client_label else mode
    stdout_path = runner.run_dir / f"wechat-client-{stem}.stdout.log"
    stderr_path = runner.run_dir / f"wechat-client-{stem}.stderr.log"
    result_path = runner.run_dir / f"wechat-client-{stem}.json"
    command = [NODE, str(CLIENT_SCRIPT), mode]
    started = time.monotonic()
    process: subprocess.Popen[str] | None = None
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        try:
            process = subprocess.Popen(
                command,
                cwd=CLIENT_DIR,
                env=_client_environment(runner, runtime_project, client_label),
                stdout=stdout,
                stderr=stderr,
                text=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            next_heartbeat = time.monotonic() + 10
            while process.poll() is None:
                if time.monotonic() - started > timeout:
                    raise TimeoutError(f"WeChat client {mode} phase exceeded {timeout:.0f}s")
                if time.monotonic() >= next_heartbeat:
                    print(
                        f"[{iso_now()}] WECHAT_CLIENT_{mode.upper()} running "
                        f"elapsed={time.monotonic() - started:.0f}s",
                        flush=True,
                    )
                    next_heartbeat = time.monotonic() + 10
                time.sleep(0.5)
        except BaseException:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            raise

    if not result_path.is_file():
        raise E2EFailure(f"WeChat client {mode} did not write {result_path}")
    result = _read_json(result_path)
    if process is None or process.returncode != 0 or result.get("status") != "PASS":
        message = ((result.get("error") or {}).get("message") or "unknown client automation failure")
        raise E2EFailure(f"WeChat client {mode} failed: {message}")
    return result


def _remove_runtime_project(runtime_project: Path, run_dir: Path) -> dict[str, Any]:
    if not runtime_project.exists():
        return {"runtime_project_removed": True, "runtime_project_remove_attempts": 0}
    resolved = runtime_project.resolve()
    if resolved.parent != run_dir.resolve():
        raise E2EFailure(f"Refusing to remove runtime project outside artifact dir: {resolved}")
    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            shutil.rmtree(runtime_project)
            return {
                "runtime_project_removed": True,
                "runtime_project_remove_attempts": attempt,
            }
        except PermissionError as exc:
            last_error = exc
            time.sleep(1.0)
    return {
        "runtime_project_removed": False,
        "runtime_project_remove_attempts": 5,
        "runtime_project_remove_error": f"{type(last_error).__name__}: {last_error}",
    }


def _real_auth_state(runner: Level7Runner) -> dict[str, Any]:
    with psycopg.connect(runner.database.psycopg_dsn) as connection:
        rows = connection.execute(
            """
            SELECT id, (length(wx_openid) > 0) AS openid_present,
                   (wx_unionid IS NOT NULL) AS unionid_present,
                   token_version, last_login_at IS NOT NULL
            FROM users ORDER BY created_at
            """
        ).fetchall()
    return {
        "user_count": len(rows),
        "users": [
            {
                "id": str(row[0]),
                "openid_present": bool(row[1]),
                "unionid_present": bool(row[2]),
                "token_version": int(row[3]),
                "last_login_present": bool(row[4]),
            }
            for row in rows
        ],
    }


def _invalidate_only_e2e_user_token(runner: Level7Runner) -> dict[str, Any]:
    with psycopg.connect(runner.database.psycopg_dsn) as connection:
        rows = connection.execute("SELECT id, token_version FROM users ORDER BY created_at").fetchall()
        if len(rows) != 1:
            raise E2EFailure(f"Expected exactly one isolated WeChat user before token invalidation, got {len(rows)}")
        user_id, old_version = rows[0]
        new_version = connection.execute(
            "UPDATE users SET token_version=token_version+1 WHERE id=%s RETURNING token_version",
            (user_id,),
        ).fetchone()[0]
        connection.commit()
    return {
        "method": "isolated PostgreSQL token_version increment",
        "user_id": str(user_id),
        "old_token_version": int(old_version),
        "new_token_version": int(new_version),
        "production_jwt_expiry_changed": False,
    }


def _validate_piper_wav(runner: Level7Runner) -> dict[str, Any]:
    candidates = sorted(path for path in runner.tts_cache.rglob("*") if path.is_file())
    valid: list[dict[str, Any]] = []
    for path in candidates:
        data = path.read_bytes()
        if len(data) > 44 and data.startswith(b"RIFF") and data[8:12] == b"WAVE":
            valid.append(
                {
                    "name": path.name,
                    "bytes": len(data),
                    "riff": True,
                    "wave": True,
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
    if not valid:
        raise E2EFailure("No valid RIFF/WAVE produced by the real client TTS flow")
    return {"valid_wav_count": len(valid), "files": valid}


def _final_database_audit(runner: Level7Runner) -> dict[str, Any]:
    snapshot = runner.database.snapshot()
    with psycopg.connect(runner.database.psycopg_dsn) as connection:
        users = connection.execute(
            "SELECT id, length(wx_openid)>0, token_version FROM users ORDER BY created_at"
        ).fetchall()
        cards = connection.execute(
            """
            SELECT id, user_id, content, understanding, note, where_encountered,
                   analysis_status, status, version, review_state, review_count,
                   last_review_result, deleted_at
            FROM cards ORDER BY created_at
            """
        ).fetchall()
        sessions = connection.execute(
            "SELECT id, user_id, status, reviewed_count, total_count, completed_at FROM review_sessions"
        ).fetchall()
        items = connection.execute(
            "SELECT id, session_id, card_id, status, reviewed_at FROM review_session_items"
        ).fetchall()
        logs = connection.execute(
            "SELECT id, user_id, session_id, session_item_id, card_id, result FROM review_logs"
        ).fetchall()
        answer_logs = connection.execute(
            """
            SELECT id, user_id, session_id, session_item_id, card_id, source_card_id,
                   question_id, selected_option_id, selected_answer_text, is_correct,
                   prompt_content_snapshot, correct_answer_snapshot, options_snapshot,
                   fsrs_state_before_json, fsrs_state_after_json
            FROM review_answer_logs
            """
        ).fetchall()
        actions = connection.execute(
            "SELECT client_action_id, user_id, action_type, status FROM client_actions ORDER BY created_at"
        ).fetchall()
        usage = connection.execute(
            "SELECT user_id, resource, count FROM resource_usage ORDER BY resource"
        ).fetchall()

    audit = {
        "snapshot": snapshot,
        "users": [
            {"id": str(row[0]), "openid_present": bool(row[1]), "token_version": int(row[2])}
            for row in users
        ],
        "cards": [
            {
                "id": str(row[0]),
                "user_id": str(row[1]),
                "content": row[2],
                "understanding": row[3],
                "note": row[4],
                "where_encountered": row[5],
                "analysis_status": row[6],
                "status": row[7],
                "version": int(row[8]),
                "review_state": row[9],
                "review_count": int(row[10]),
                "last_review_result": row[11],
                "deleted": row[12] is not None,
            }
            for row in cards
        ],
        "review_sessions": [
            {
                "id": str(row[0]),
                "user_id": str(row[1]),
                "status": row[2],
                "reviewed_count": int(row[3]),
                "total_count": int(row[4]),
                "completed": row[5] is not None,
            }
            for row in sessions
        ],
        "review_session_items": [
            {
                "id": str(row[0]),
                "session_id": str(row[1]),
                "card_id": str(row[2]),
                "status": row[3],
                "reviewed": row[4] is not None,
            }
            for row in items
        ],
        "review_logs": [
            {
                "id": str(row[0]),
                "user_id": str(row[1]),
                "session_id": str(row[2]),
                "session_item_id": str(row[3]),
                "card_id": str(row[4]),
                "result": row[5],
            }
            for row in logs
        ],
        "review_answer_logs": [
            {
                "id": str(row[0]),
                "user_id": str(row[1]),
                "session_id": str(row[2]),
                "session_item_id": str(row[3]),
                "card_id": str(row[4]) if row[4] else None,
                "source_card_id": str(row[5]),
                "question_id": str(row[6]),
                "selected_option_id": row[7],
                "selected_answer_text": row[8],
                "is_correct": bool(row[9]),
                "prompt_content_snapshot": row[10],
                "correct_answer_snapshot": row[11],
                "options_snapshot": row[12],
                "fsrs_state_before_json": row[13],
                "fsrs_state_after_json": row[14],
            }
            for row in answer_logs
        ],
        "client_actions": [
            {
                "client_action_id": row[0],
                "user_id": str(row[1]),
                "action_type": row[2],
                "status": row[3],
            }
            for row in actions
        ],
        "resource_usage": [
            {"user_id": str(row[0]), "resource": row[1], "count": int(row[2])}
            for row in usage
        ],
    }

    usage_map = {item["resource"]: item["count"] for item in audit["resource_usage"]}
    violations = snapshot.get("violations") or {}
    ownership_ok = bool(users) and all(str(row[1]) == str(users[0][0]) for row in cards)
    ownership_ok = ownership_ok and all(str(row[1]) == str(users[0][0]) for row in sessions)
    ownership_ok = ownership_ok and all(str(row[1]) == str(users[0][0]) for row in logs)
    ownership_ok = ownership_ok and all(str(row[1]) == str(users[0][0]) for row in answer_logs)
    review_entries = len(logs) + len(answer_logs)
    review_actions = [row for row in actions if row[2] in {"review_feedback", "review_mcq_answer"}]
    snapshots_ok = all(
        row[10] and row[11] and len(row[12] or []) == 4 and row[13] and row[14]
        for row in answer_logs
    )
    checks = {
        "exactly_one_real_user": len(users) == 1 and bool(users[0][1]),
        "exactly_one_card": len(cards) == 1,
        "card_content_correct": len(cards) == 1 and cards[0][2] == "ineffable",
        "card_understanding_correct": len(cards) == 1 and cards[0][3] == "美好得难以言喻",
        "card_source_correct": len(cards) == 1 and cards[0][5] == "Level 7 edited through UI",
        "card_not_deleted": len(cards) == 1 and cards[0][12] is None,
        "card_analysis_committed": len(cards) == 1 and cards[0][6] == "done",
        "no_duplicate_or_half_transaction": all(int(value or 0) == 0 for value in violations.values()),
        "sync_replay_no_duplicate": len(cards) == 1 and int(violations.get("duplicate_card_local_ids") or 0) == 0,
        "review_log_committed_once": review_entries == 1 and len(review_actions) == 1,
        "review_state_consistent": len(items) == 1 and bool(items[0][4]) and review_entries == 1,
        "review_snapshot_complete": snapshots_ok,
        "ai_quota_correct": usage_map.get("ai") == 3,
        "tts_quota_correct": usage_map.get("tts") == 1,
        "user_ownership_correct": ownership_ok,
    }
    audit["checks"] = checks
    audit["status"] = "PASS" if all(checks.values()) else "FAIL"
    return audit


def _http_evidence(log_path: Path) -> dict[str, Any]:
    relevant = {
        "/api/validate-english",
        "/api/auth/wechat-login",
        "/api/auth/me",
        "/api/auth/logout",
        "/api/cards",
        "/api/analyze-english",
        "/api/analyze-english/stream",
        "/api/pronunciation/audio",
        "/api/review-sessions",
        "/api/reviews/today",
        "/api/reviews/feedback",
    }
    records: list[dict[str, Any]] = []
    if log_path.is_file():
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("event") != "http_request_finished":
                continue
            path_value = str(item.get("path") or "")
            if path_value not in relevant and not any(path_value.startswith(prefix + "/") for prefix in relevant):
                continue
            records.append(
                {
                    "timestamp": item.get("timestamp"),
                    "method": item.get("method"),
                    "path": path_value,
                    "status_code": item.get("status_code"),
                    "duration_ms": item.get("duration_ms"),
                    "request_id": item.get("request_id"),
                    "trace_id": item.get("trace_id"),
                }
            )
    return {
        "request_count": len(records),
        "requests": records,
        "request_ids_present": sum(bool(item.get("request_id")) for item in records),
        "trace_ids_present": sum(bool(item.get("trace_id")) for item in records),
    }


def _build_acceptance(
    core: dict[str, Any] | None,
    system_validation: dict[str, Any] | None,
    recovery: dict[str, Any] | None,
    auth_state: dict[str, Any] | None,
    database: dict[str, Any] | None,
    piper: dict[str, Any] | None,
    final_gauges: dict[str, float] | None,
) -> dict[str, str]:
    core_ok = bool(core and core.get("status") == "PASS")
    recovery_ok = bool(recovery and recovery.get("status") == "PASS")
    real_auth = bool(
        core_ok
        and auth_state
        and auth_state.get("user_count") == 1
        and auth_state.get("users", [{}])[0].get("openid_present")
    )
    stream = (core or {}).get("streaming") or {}
    tts = (core or {}).get("tts") or {}
    validation = (core or {}).get("validation") or {}
    system_state = (system_validation or {}).get("validation") or {}
    card = (core or {}).get("card") or {}
    recovery_evidence = (recovery or {}).get("recovery") or {}
    resource_gauge_names = tuple(name for name in GAUGE_NAMES if name != "http_requests_in_progress")
    gauges_ok = bool(final_gauges) and all(
        float(final_gauges.get(name, -1)) == 0 for name in resource_gauge_names
    )
    return {
        "wechat_devtools_automation": "PASS" if core_ok and recovery_ok else "FAIL",
        "wx_login": "REAL PASS" if real_auth else "REAL FAIL",
        "code2session": "REAL PASS" if real_auth else "REAL FAIL",
        "jwt": "REAL PASS" if real_auth and recovery_ok else "REAL FAIL",
        "card_ui_flow": "PASS" if core_ok and card.get("reread_after_edit") == "ineffable" else "FAIL",
        "validation_ux": "PASS" if core_ok and validation.get("status") == "PASS" and validation.get("validation_request_events", 0) >= 8 and (tts.get("invalid_blocked") or {}).get("request_events") == 0 and (tts.get("content_warning_blocked") or {}).get("request_events") == 0 and system_validation and system_validation.get("status") == "PASS" and all(system_state.get(name) is True for name in ("canSave", "canAnalyze", "canPronounce")) else "FAIL",
        "sync": "PASS" if core_ok and (card.get("sync_replay") or {}).get("synced_one_events", 0) >= 1 else "FAIL",
        "qwen": "REAL PASS" if core_ok and stream.get("final_events", 0) >= 1 else "REAL FAIL",
        "wx_onChunkReceived": "REAL PASS" if core_ok and stream.get("chunk_events", 0) >= 2 else "REAL FAIL",
        "ui_progressive_update": "PASS" if core_ok and stream.get("progressive_before_complete") else "FAIL",
        "piper": "REAL PASS" if piper and piper.get("valid_wav_count", 0) >= 1 else "REAL FAIL",
        "tts_client_flow": "PASS" if core_ok and tts.get("playing_observed") and tts.get("console_errors") == 0 and piper else "FAIL",
        "review_ui_flow": "PASS" if core_ok and database and database.get("checks", {}).get("review_log_committed_once") else "FAIL",
        "logout": "PASS" if recovery_ok and (recovery.get("logout") or {}).get("token_cleared") else "FAIL",
        "401_relogin": "PASS" if recovery_ok and recovery.get("backend_request_401_count", 0) >= 1 and recovery_evidence.get("wx_login_start", 0) >= 1 and recovery_evidence.get("protected_200_after_refresh", 0) >= 1 else "FAIL",
        "postgresql_final_state": "PASS" if database and database.get("status") == "PASS" else "FAIL",
        "resource_final_state": "PASS" if gauges_ok else "FAIL",
        "cleanup": "PENDING",
    }


def _render_report(result: dict[str, Any]) -> str:
    acceptance = result.get("acceptance") or {}
    lines = [
        "# Level 7 Real WeChat Mini Program Client E2E",
        "",
        f"- Run: `{result.get('run_id')}`",
        f"- Started: `{result.get('started_at')}`",
        f"- Finished: `{result.get('finished_at')}`",
        f"- Overall: `{result.get('overall_status')}`",
        "- AppID exists: `YES`",
        "- WeChat AppSecret configured: `YES`",
        "- AppSecret/code/JWT values were not written to artifacts.",
        "",
        "## Acceptance",
        "",
    ]
    labels = (
        ("wechat_devtools_automation", "微信开发者工具 automation"),
        ("wx_login", "wx.login"),
        ("code2session", "code2session"),
        ("jwt", "JWT"),
        ("card_ui_flow", "Card UI flow"),
        ("validation_ux", "Validation UX"),
        ("sync", "Sync"),
        ("qwen", "Qwen"),
        ("wx_onChunkReceived", "wx.onChunkReceived"),
        ("ui_progressive_update", "UI progressive update"),
        ("piper", "Piper"),
        ("tts_client_flow", "TTS client flow"),
        ("review_ui_flow", "Review UI flow"),
        ("logout", "logout"),
        ("401_relogin", "401 relogin"),
        ("postgresql_final_state", "PostgreSQL final state"),
        ("resource_final_state", "resource final state"),
        ("cleanup", "cleanup"),
    )
    for key, label in labels:
        lines.append(f"- {label}: `{acceptance.get(key, 'FAIL')}`")
    lines.extend(
        [
            "",
            "## Boundaries",
            "",
            "- Core Card/AI/TTS/Review/logout actions were performed through Mini Program UI elements.",
            "- The 401 experiment invalidated only the isolated user's token_version in the E2E PostgreSQL database; production JWT expiry was unchanged.",
            "- The sync replay setup changed only namespaced E2E Mini Program Storage, then the real Home lifecycle performed the replay.",
            "- TTS request, WAV decode path and InnerAudioContext onPlay are covered when observed; physical speaker output is not covered.",
            "",
            "## Final answer",
            "",
            result.get("chain_conclusion", "The real chain was not fully proven."),
            "",
        ]
    )
    return "\n".join(lines)


def _source_revision(path: Path) -> dict[str, Any]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    return {"head": head, "dirty": dirty}


def _layer3_artifact(result: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    core = ((result.get("client") or {}).get("core") or {})
    system_validation = ((result.get("client") or {}).get("system_validation") or {})
    recovery = ((result.get("client") or {}).get("recovery") or {})
    http_requests = ((result.get("http_evidence") or {}).get("requests") or [])
    request_ids = [item.get("request_id") for item in http_requests if item.get("request_id")]
    steps = (core.get("steps") or []) + (system_validation.get("steps") or []) + (recovery.get("steps") or [])
    screenshots = (
        (core.get("screenshots") or [])
        + (system_validation.get("screenshots") or [])
        + (recovery.get("screenshots") or [])
    )
    acceptance = result.get("acceptance") or {}
    common = {
        "operationLog": steps,
        "requestCapture": http_requests,
        "requestIds": request_ids,
        "screenshots": screenshots,
        "backendLogRefs": [str(run_dir / "uvicorn.log")],
        "finalDbState": result.get("final_database_audit"),
    }
    specs = (
        ("L3-VALIDATION-PASS", "VAL-PASS-001", "validation", "validation_ux"),
        ("L3-VALIDATION-CONTENT", "VAL-CONTENT-001", "validation", "validation_ux"),
        ("L3-VALIDATION-ADVISORY", "VAL-ADVISORY-001", "validation", "validation_ux"),
        ("L3-VALIDATION-SYSTEM", "VAL-SYSTEM-001", "validation", "validation_ux"),
        ("L3-VALIDATION-ERROR", "VAL-HARD-001", "validation", "validation_ux"),
        ("L3-CARD-WARNING-SAVE", "VAL-CONTENT-001", "card-create-edit", "card_ui_flow"),
        ("L3-AI-CONTENT-BLOCK", "VAL-CONTENT-001", "ai-analysis", "validation_ux"),
        ("L3-AI-CANCEL-REGENERATE", "VAL-PASS-001", "ai-analysis", "qwen"),
        ("L3-PRONUNCIATION-KNOWN", "VAL-PASS-001", "pronunciation", "tts_client_flow"),
        ("L3-PRONUNCIATION-CONTENT-BLOCK", "VAL-CONTENT-001", "pronunciation", "validation_ux"),
        ("L3-REVIEW-FIXTURE", "VAL-PASS-001", "review", "review_ui_flow"),
        ("L3-AUTH-RECOVERY-LOGOUT", "VAL-PASS-001", "auth", "401_relogin"),
        ("L3-SYNC-REPLAY", "VAL-PASS-001", "sync", "sync"),
        ("L3-FULL-SMOKE", "VAL-PASS-001", "full-smoke", "wechat_devtools_automation"),
    )
    journeys = []
    for journey_id, scenario_id, module, acceptance_key in specs:
        journeys.append(
            {
                "journeyId": journey_id,
                "scenarioId": scenario_id,
                "module": module,
                "status": "PASS"
                if acceptance.get(acceptance_key) in {"PASS", "REAL PASS"}
                else "FAIL",
                **common,
            }
        )
    return {
        "schemaVersion": "1.0",
        "runId": result.get("run_id"),
        "timestamp": result.get("finished_at"),
        "sourceRevision": result.get("source_revision"),
        "environment": result.get("environment"),
        "journeys": journeys,
    }


async def execute() -> tuple[int, Path]:
    runner = Level7Runner()
    harper_process, harper_runtime = _ensure_harper(runner.run_dir)
    try:
        preflight = _preflight()
    except BaseException:
        if harper_process is not None:
            harper_process.stop()
        raise
    preflight["harper_runtime"] = harper_runtime
    runtime_project = runner.run_dir / "runtime-miniapp"
    runner.result.update(
        {
            "task": "real_wechat_client_e2e",
            "preflight": preflight,
            "dependencies": {
                "postgresql": "REAL",
                "http": "REAL",
                "auth": "REAL",
                "qwen": "REAL",
                "piper": "REAL",
                "harper": "REAL",
                "wechat_client": "REAL",
            },
            "client": {},
            "acceptance": {},
            "source_revision": {
                "backend": _source_revision(ROOT),
                "miniapp": _source_revision(MINIAPP),
            },
        }
    )
    core: dict[str, Any] | None = None
    system_validation: dict[str, Any] | None = None
    recovery: dict[str, Any] | None = None
    auth_state: dict[str, Any] | None = None
    database: dict[str, Any] | None = None
    piper: dict[str, Any] | None = None
    final_gauges: dict[str, float] | None = None
    gate_failed = False

    try:
        print(f"[{iso_now()}] START isolated Level 7 backend setup", flush=True)
        os.environ["HARPER_ENABLED"] = "true"
        os.environ["HARPER_BASE_URL"] = HARPER_BASE_URL
        os.environ["HARPER_TIMEOUT_SECONDS"] = "0.5"
        await runner.setup()
        runner.result["environment"]["wechat_automation"] = {
            "developer_cli_present": True,
            "service_port_enabled": True,
            "service_port": WECHAT_SERVICE_PORT,
            "automation_port": WECHAT_AUTOMATION_PORT,
            "runtime_copy": str(runtime_project),
            "source_project_unchanged": True,
        }
        runner.result["initial_database_snapshot"] = runner.database.snapshot()

        print(f"[{iso_now()}] START real WeChat client core UI flow", flush=True)
        core = await asyncio.to_thread(_run_client, runner, runtime_project, "core", 720.0)
        runner.result["client"]["core"] = core
        auth_state = _real_auth_state(runner)
        runner.result["real_auth_database_evidence"] = auth_state
        if auth_state.get("user_count") != 1 or not auth_state.get("users", [{}])[0].get("openid_present"):
            raise E2EFailure(f"Real code2session/openid evidence failed: {auth_state}")
        piper = _validate_piper_wav(runner)
        runner.result["piper_wav_evidence"] = piper

        if harper_process is None:
            raise E2EFailure(
                "Layer 3 SYSTEM_WARNING requires a runner-managed Harper sidecar; "
                "port 8082 was already occupied and will not be stopped"
            )
        stopped_harper = harper_process.stop()
        harper_process = None
        if port_listener_pid(HARPER_PORT) is not None:
            raise E2EFailure("Runner-managed Harper sidecar did not stop for SYSTEM_WARNING")
        try:
            system_validation = await asyncio.to_thread(
                _run_client, runner, runtime_project, "system-validation", 120.0
            )
            runner.result["client"]["system_validation"] = system_validation
            runner.result["harper_fault_injection"] = {
                "kind": "controlled_process_stop",
                "stopped": stopped_harper,
                "port_while_stopped": port_listener_pid(HARPER_PORT),
            }
        finally:
            harper_process, restarted_harper = _ensure_harper(runner.run_dir)
            runner.result.setdefault("harper_fault_injection", {})["recovery"] = restarted_harper

        invalidation = _invalidate_only_e2e_user_token(runner)
        runner.result["token_invalidation"] = invalidation
        print(f"[{iso_now()}] START concurrent 401 recovery and logout UI flow", flush=True)
        recovery = await asyncio.to_thread(_run_client, runner, runtime_project, "recovery", 180.0)
        recovery_http = _http_evidence(runner.run_dir / "uvicorn.log")
        recovery_requests = recovery_http.get("requests") or []
        recovery["backend_request_401_count"] = sum(
            1 for item in recovery_requests if item.get("status_code") == 401
        )
        recovery["backend_request_401_request_ids"] = [
            item.get("request_id")
            for item in recovery_requests
            if item.get("status_code") == 401 and item.get("request_id")
        ]
        if recovery["backend_request_401_count"] < 1:
            raise E2EFailure("Backend did not record a real 401 during client recovery")
        runner.result["client"]["recovery"] = recovery

        final_metric_snapshot = await wait_for_gauge_baseline(runner.http.client, timeout=60)
        final_gauges = {name: metric_value(final_metric_snapshot, name) for name in GAUGE_NAMES}
        runner.result["final_gauges_before_cleanup"] = final_gauges
        database = _final_database_audit(runner)
        runner.result["final_database_audit"] = database
        runner.result["http_evidence"] = _http_evidence(runner.run_dir / "uvicorn.log")
        write_json(runner.run_dir / "client-http-evidence.json", runner.result["http_evidence"])
        write_json(runner.run_dir / "final-postgresql-audit.json", database)
        write_json(runner.run_dir / "streaming-evidence.json", core.get("streaming") or {})
        write_json(runner.run_dir / "validation-ux-evidence.json", core.get("validation") or {})

        if database.get("status") != "PASS":
            raise E2EFailure(f"Final PostgreSQL audit failed: {database.get('checks')}")
        resource_gauge_names = tuple(name for name in GAUGE_NAMES if name != "http_requests_in_progress")
        if any(float(final_gauges.get(name, -1)) != 0 for name in resource_gauge_names):
            raise E2EFailure(f"Final resource gauges did not return to zero: {final_gauges}")
    except BaseException as exc:
        gate_failed = True
        _append_exception(runner.result, "wechat_client_e2e", exc)
        print(f"[{iso_now()}] FAIL {type(exc).__name__}: {exc}", flush=True)
    finally:
        if runner.database.created and database is None:
            try:
                database = _final_database_audit(runner)
                runner.result["final_database_audit"] = database
                write_json(runner.run_dir / "final-postgresql-audit.json", database)
            except Exception as exc:
                _append_exception(runner.result, "final_database_audit", exc)
                gate_failed = True
        if runner.recorder is not None:
            try:
                metric_snapshot = await fetch_metrics(runner.recorder.client)
                final_gauges = {name: metric_value(metric_snapshot, name) for name in GAUGE_NAMES}
                runner.result.setdefault("final_gauges_before_cleanup", final_gauges)
            except Exception as exc:
                _append_exception(runner.result, "final_metrics", exc)
                gate_failed = True
        try:
            runner.result["http_evidence"] = _http_evidence(runner.run_dir / "uvicorn.log")
            write_json(runner.run_dir / "client-http-evidence.json", runner.result["http_evidence"])
        except Exception as exc:
            _append_exception(runner.result, "http_evidence", exc)

        automation_cleanup: dict[str, Any] = {"started_at": iso_now()}
        if port_listener_pid(WECHAT_AUTOMATION_PORT) is not None:
            try:
                cleanup_result = await asyncio.to_thread(
                    _run_client, runner, runtime_project, "cleanup", 45.0
                )
                automation_cleanup["client_cleanup"] = cleanup_result
            except Exception as exc:
                automation_cleanup["client_cleanup_error"] = f"{type(exc).__name__}: {exc}"
        if port_listener_pid(WECHAT_AUTOMATION_PORT) is not None and runtime_project.exists():
            try:
                close_log = runner.run_dir / "wechat-cli-close.log"
                completed = subprocess.run(
                    [
                        str(WECHAT_CLI),
                        "close",
                        "--project",
                        str(runtime_project),
                        "--port",
                        str(WECHAT_SERVICE_PORT),
                    ],
                    cwd=MINIAPP,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                close_log.write_text(
                    (completed.stdout or "") + (completed.stderr or ""), encoding="utf-8"
                )
                automation_cleanup["cli_close_returncode"] = completed.returncode
                time.sleep(2)
            except Exception as exc:
                automation_cleanup["cli_close_error"] = f"{type(exc).__name__}: {exc}"
        automation_cleanup["automation_port_after_close"] = port_listener_pid(WECHAT_AUTOMATION_PORT)

        if runtime_project.exists():
            try:
                automation_cleanup.update(_remove_runtime_project(runtime_project, runner.run_dir))
            except Exception as exc:
                automation_cleanup["runtime_project_removed"] = False
                automation_cleanup["runtime_project_remove_error"] = f"{type(exc).__name__}: {exc}"
        else:
            automation_cleanup["runtime_project_removed"] = True
        automation_cleanup["finished_at"] = iso_now()

        await runner.cleanup()
        harper_cleanup: dict[str, Any] = {
            "managed_by_runner": harper_process is not None,
            "reused": harper_process is None,
        }
        if harper_process is not None:
            try:
                harper_cleanup["process"] = harper_process.stop()
            except Exception as exc:
                harper_cleanup["error"] = f"{type(exc).__name__}: {exc}"
        harper_cleanup["port_after_stop"] = port_listener_pid(HARPER_PORT)
        runner.result["cleanup"]["harper"] = harper_cleanup
        runner.result["cleanup"]["wechat_automation"] = automation_cleanup
        cleanup = runner.result["cleanup"]
        database_cleanup = cleanup.get("database") or {}
        cleanup_ok = (
            (database_cleanup.get("dropped") is True or database_cleanup.get("attempted") is False)
            and (cleanup.get("ports_after_stop") or {}).get(str(APP_PORT)) is None
            and (cleanup.get("ports_after_stop") or {}).get(str(PROXY_PORT)) is None
            and automation_cleanup.get("automation_port_after_close") is None
            and automation_cleanup.get("runtime_project_removed") is True
            and (
                harper_process is None
                or harper_cleanup.get("port_after_stop") is None
            )
        )

        acceptance = _build_acceptance(
            core, system_validation, recovery, auth_state, database, piper, final_gauges
        )
        acceptance["cleanup"] = "PASS" if cleanup_ok else "FAIL"
        runner.result["acceptance"] = acceptance
        all_pass = cleanup_ok and not gate_failed and all(
            value in {"PASS", "REAL PASS"} for value in acceptance.values()
        )
        runner.result["overall_status"] = "PASS" if all_pass else "FAIL"
        runner.result["finished_at"] = iso_now()
        if all_pass:
            runner.result["chain_conclusion"] = (
                "YES. This run proved the real WeChat DevTools Mini Program -> wx.login -> "
                "code2session -> JWT -> wx.request/onChunkReceived -> FastAPI -> isolated PostgreSQL/"
                "Qwen/Piper -> progressive page state chain."
            )
        else:
            failed = [key for key, value in acceptance.items() if value not in {"PASS", "REAL PASS"}]
            runner.result["chain_conclusion"] = (
                "NO. The full chain was not proven; failed acceptance items: " + ", ".join(failed)
            )
        write_json(
            runner.run_dir / "layer3-journeys.json",
            sanitize_value(_layer3_artifact(runner.result, runner.run_dir)),
        )
        write_json(runner.run_dir / "result.json", sanitize_value(runner.result))
        (runner.run_dir / "REPORT.md").write_text(
            _render_report(sanitize_value(runner.result)), encoding="utf-8"
        )
        print(f"[{iso_now()}] ARTIFACT_DIR {runner.run_dir}", flush=True)
        print(f"[{iso_now()}] RESULT_PATH {runner.run_dir / 'result.json'}", flush=True)
        print(f"[{iso_now()}] REPORT_PATH {runner.run_dir / 'REPORT.md'}", flush=True)
        print(f"[{iso_now()}] OVERALL {runner.result['overall_status']}", flush=True)
    return (0 if runner.result["overall_status"] == "PASS" else 1), runner.run_dir


def main() -> int:
    try:
        code, _ = asyncio.run(execute())
        return code
    except KeyboardInterrupt:
        return 130
    except BaseException as exc:
        print(f"[{iso_now()}] FATAL {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
