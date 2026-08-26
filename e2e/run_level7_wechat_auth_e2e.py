"""Stage 1 only: real WeChat DevTools wx.login -> code2session -> JWT E2E."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import e2e.run_level7_wechat_e2e as wechat
from e2e.run_level7_e2e import (
    APP_PORT,
    PROXY_PORT,
    E2EFailure,
    Level7Runner,
    iso_now,
    port_listener_pid,
    sanitize_value,
    write_json,
)


def _auth_http_checks(evidence: dict[str, Any]) -> dict[str, Any]:
    login_requests = [
        item
        for item in evidence.get("requests", [])
        if item.get("path") == "/api/auth/wechat-login"
    ]
    protected_requests = [
        item
        for item in evidence.get("requests", [])
        if str(item.get("path") or "").startswith("/api/cards")
        and item.get("status_code") == 200
    ]
    return {
        "login_request_count": len(login_requests),
        "login_http_200": any(item.get("status_code") == 200 for item in login_requests),
        "login_request_ids": [item.get("request_id") for item in login_requests if item.get("request_id")],
        "login_trace_ids": [item.get("trace_id") for item in login_requests if item.get("trace_id")],
        "protected_request_http_200": bool(protected_requests),
        "protected_request_ids": [item.get("request_id") for item in protected_requests if item.get("request_id")],
    }


async def execute() -> tuple[int, Path]:
    preflight = wechat._preflight()
    runner = Level7Runner()
    runtime_project = runner.run_dir / "runtime-miniapp"
    runner.result.update(
        {
            "task": "real_wechat_auth_client_e2e",
            "scope": "wx.login -> code2session -> JWT/storage -> protected wx.request only",
            "preflight": preflight,
            "dependencies": {
                "postgresql": "REAL",
                "http": "REAL",
                "auth": "REAL",
                "wechat_client": "REAL",
                "qwen": "NOT_EXECUTED",
                "piper": "NOT_EXECUTED",
            },
            "client": {},
            "acceptance": {},
        }
    )
    client: dict[str, Any] | None = None
    database: dict[str, Any] | None = None
    http_evidence: dict[str, Any] = {}
    failed = False

    try:
        print(f"[{iso_now()}] START isolated Level 7 backend setup", flush=True)
        await runner.setup()
        runner.result["environment"]["wechat_automation"] = {
            "service_port": wechat.WECHAT_SERVICE_PORT,
            "automation_port": wechat.WECHAT_AUTOMATION_PORT,
            "runtime_copy": str(runtime_project),
            "source_project_unchanged": True,
        }
        runner.result["initial_database_snapshot"] = runner.database.snapshot()

        print(f"[{iso_now()}] START real WeChat auth-only client flow", flush=True)
        client = await asyncio.to_thread(
            wechat._run_client, runner, runtime_project, "auth", 120.0
        )
        runner.result["client"]["auth"] = client
        database = wechat._real_auth_state(runner)
        runner.result["real_auth_database_evidence"] = database
        http_evidence = wechat._http_evidence(runner.run_dir / "uvicorn.log")
        runner.result["http_evidence"] = http_evidence
        http_checks = _auth_http_checks(http_evidence)
        runner.result["auth_http_checks"] = http_checks
        write_json(runner.run_dir / "client-http-evidence.json", http_evidence)
        write_json(runner.run_dir / "auth-postgresql-evidence.json", database)

        auth = client.get("auth") or {}
        storage = client.get("storage") or {}
        db_user = (database.get("users") or [{}])[0]
        checks = {
            "wx_login_real_code": bool(auth.get("code_present")) and int(auth.get("code_length") or 0) > 0,
            "wechat_login_request_received": bool(http_checks.get("login_http_200")) and bool(http_checks.get("login_request_ids")),
            "code2session_openid_created": database.get("user_count") == 1 and bool(db_user.get("openid_present")),
            "jwt_returned_and_saved": bool(auth.get("jwt_saved")) and bool(storage.get("hasAccessToken")) and int(storage.get("accessTokenLength") or 0) > 0,
            "protected_wx_request_with_session": bool(auth.get("protected_request_finished")) and bool(http_checks.get("protected_request_http_200")),
            "page_login_success_visible": (client.get("page") or {}).get("login_status_text") == "WX LOGIN E2E: PASS",
            "last_login_recorded": bool(db_user.get("last_login_present")),
        }
        runner.result["auth_checks"] = checks
        if not all(checks.values()):
            raise E2EFailure(f"Real WeChat auth checks failed: {checks}")
    except BaseException as exc:
        failed = True
        wechat._append_exception(runner.result, "wechat_auth_e2e", exc)
        print(f"[{iso_now()}] FAIL {type(exc).__name__}: {exc}", flush=True)
    finally:
        if runner.database.created and database is None:
            try:
                database = wechat._real_auth_state(runner)
                runner.result["real_auth_database_evidence"] = database
                write_json(runner.run_dir / "auth-postgresql-evidence.json", database)
            except Exception as exc:
                wechat._append_exception(runner.result, "auth_database_evidence", exc)
                failed = True
        try:
            http_evidence = wechat._http_evidence(runner.run_dir / "uvicorn.log")
            runner.result["http_evidence"] = http_evidence
            runner.result["auth_http_checks"] = _auth_http_checks(http_evidence)
            write_json(runner.run_dir / "client-http-evidence.json", http_evidence)
        except Exception as exc:
            wechat._append_exception(runner.result, "auth_http_evidence", exc)
            failed = True

        automation_cleanup: dict[str, Any] = {"started_at": iso_now()}
        if port_listener_pid(wechat.WECHAT_AUTOMATION_PORT) is not None:
            try:
                automation_cleanup["client_cleanup"] = await asyncio.to_thread(
                    wechat._run_client, runner, runtime_project, "cleanup", 45.0
                )
            except Exception as exc:
                automation_cleanup["client_cleanup_error"] = f"{type(exc).__name__}: {exc}"
        if port_listener_pid(wechat.WECHAT_AUTOMATION_PORT) is not None and runtime_project.exists():
            try:
                completed = subprocess.run(
                    [
                        str(wechat.WECHAT_CLI),
                        "close",
                        "--project",
                        str(runtime_project),
                        "--port",
                        str(wechat.WECHAT_SERVICE_PORT),
                    ],
                    cwd=wechat.MINIAPP,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=False,
                )
                (runner.run_dir / "wechat-cli-close.stdout.log").write_text(completed.stdout or "", encoding="utf-8")
                (runner.run_dir / "wechat-cli-close.stderr.log").write_text(completed.stderr or "", encoding="utf-8")
                automation_cleanup["cli_close_returncode"] = completed.returncode
                time.sleep(2)
            except Exception as exc:
                automation_cleanup["cli_close_error"] = f"{type(exc).__name__}: {exc}"
        automation_cleanup["automation_port_after_close"] = port_listener_pid(wechat.WECHAT_AUTOMATION_PORT)

        if runtime_project.exists():
            try:
                automation_cleanup.update(
                    wechat._remove_runtime_project(runtime_project, runner.run_dir)
                )
            except Exception as exc:
                automation_cleanup["runtime_project_removed"] = False
                automation_cleanup["runtime_project_remove_error"] = f"{type(exc).__name__}: {exc}"
        else:
            automation_cleanup["runtime_project_removed"] = True
        automation_cleanup["finished_at"] = iso_now()

        try:
            await runner.cleanup()
        except Exception as exc:
            wechat._append_exception(runner.result, "level7_cleanup", exc)
            failed = True
        runner.result["cleanup"]["wechat_automation"] = automation_cleanup
        database_cleanup = (runner.result.get("cleanup") or {}).get("database") or {}
        cleanup_ok = (
            (database_cleanup.get("dropped") is True or database_cleanup.get("attempted") is False)
            and ((runner.result.get("cleanup") or {}).get("ports_after_stop") or {}).get(str(APP_PORT)) is None
            and ((runner.result.get("cleanup") or {}).get("ports_after_stop") or {}).get(str(PROXY_PORT)) is None
            and automation_cleanup.get("automation_port_after_close") is None
            and automation_cleanup.get("runtime_project_removed") is True
        )
        auth_ok = bool(runner.result.get("auth_checks")) and all((runner.result.get("auth_checks") or {}).values())
        runner.result["acceptance"] = {
            "wx_login": "REAL PASS" if auth_ok else "REAL FAIL",
            "code2session": "REAL PASS" if auth_ok else "REAL FAIL",
            "jwt_storage": "REAL PASS" if auth_ok else "REAL FAIL",
            "protected_wx_request": "PASS" if auth_ok else "FAIL",
            "postgresql_auth_state": "PASS" if auth_ok else "FAIL",
            "cleanup": "PASS" if cleanup_ok else "FAIL",
            "card_ai_tts_review": "NOT_EXECUTED",
        }
        runner.result["overall_status"] = "PASS" if auth_ok and cleanup_ok and not failed else "FAIL"
        runner.result["finished_at"] = iso_now()
        result = sanitize_value(runner.result)
        write_json(runner.run_dir / "result.json", result)
        report = [
            "# Level 7 WeChat Auth-only E2E",
            "",
            f"- WX LOGIN E2E: **{runner.result['overall_status']}**",
            f"- FastAPI: `127.0.0.1:{APP_PORT}`",
            f"- PostgreSQL: `english_analyzer_phase1_e2e`",
            f"- Automation: `127.0.0.1:{wechat.WECHAT_AUTOMATION_PORT}`",
            "- Card/AI/TTS/Review: `NOT_EXECUTED`",
            "",
            "No wx.login code, openid, AppSecret, or JWT value is stored in this report.",
        ]
        (runner.run_dir / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
        print(f"[{iso_now()}] ARTIFACT_DIR {runner.run_dir}", flush=True)
        print(f"[{iso_now()}] OVERALL {runner.result['overall_status']}", flush=True)

    return (0 if runner.result["overall_status"] == "PASS" else 1), runner.run_dir


def main() -> int:
    try:
        code, _ = asyncio.run(execute())
        return code
    except KeyboardInterrupt:
        return 130
    except BaseException as exc:
        print(f"[{iso_now()}] FATAL {type(exc).__name__}: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
