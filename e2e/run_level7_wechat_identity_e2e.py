"""Step 1: real WeChat identity capacity probe for Level 7 client E2E."""

from __future__ import annotations

import asyncio
import hashlib
import json
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
    DB_NAME,
    PROXY_PORT,
    E2EFailure,
    Level7Runner,
    iso_now,
    port_listener_pid,
    sanitize_value,
    write_json,
)

import psycopg


PROBES = (
    {"label": "identity-a", "port": 19420},
    {"label": "identity-b", "port": 19421},
    {"label": "identity-c", "port": 19422},
)


def _openid_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _identity_rows(runner: Level7Runner) -> list[dict[str, Any]]:
    with psycopg.connect(runner.database.psycopg_dsn) as connection:
        rows = connection.execute(
            """
            SELECT id, wx_openid, wx_unionid IS NOT NULL, token_version,
                   last_login_at IS NOT NULL, created_at, updated_at
            FROM users
            ORDER BY created_at, id
            """
        ).fetchall()
    return [
        {
            "user_id": str(row[0]),
            "openid_hash": _openid_hash(str(row[1])),
            "openid_length": len(str(row[1])),
            "unionid_present": bool(row[2]),
            "token_version": int(row[3]),
            "last_login_present": bool(row[4]),
            "created_at": row[5].isoformat() if row[5] else None,
            "updated_at": row[6].isoformat() if row[6] else None,
        }
        for row in rows
    ]


def _latest_user(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> dict[str, Any] | None:
    before_ids = {item["user_id"] for item in before}
    created = [item for item in after if item["user_id"] not in before_ids]
    if created:
        return created[-1]
    if after:
        return after[-1]
    return None


def _http_login_count(runner: Level7Runner) -> int:
    return sum(
        1
        for item in wechat._http_evidence(runner.run_dir / "uvicorn.log").get("requests", [])
        if item.get("path") == "/api/auth/wechat-login" and item.get("status_code") == 200
    )


def _close_runtime_project(runtime_project: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"attempted": True}
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
        result["returncode"] = completed.returncode
        result["stdout_bytes"] = len(completed.stdout or "")
        result["stderr_bytes"] = len(completed.stderr or "")
        time.sleep(2)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


async def _run_probe(
    runner: Level7Runner, probe: dict[str, Any], runtime_project: Path
) -> dict[str, Any]:
    label = str(probe["label"])
    port = int(probe["port"])
    if port_listener_pid(port) is not None:
        raise E2EFailure(f"Automation port {port} is already in use before {label}")

    before = _identity_rows(runner)
    login_count_before = _http_login_count(runner)
    old_port = wechat.WECHAT_AUTOMATION_PORT
    wechat.WECHAT_AUTOMATION_PORT = port
    started = iso_now()
    client: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    cleanup: dict[str, Any] = {}
    try:
        client = await asyncio.to_thread(
            wechat._run_client,
            runner,
            runtime_project,
            "auth",
            150.0,
            label,
        )
        after = _identity_rows(runner)
        login_count_after = _http_login_count(runner)
        user = _latest_user(before, after)
        owner = None
        for step in reversed(client.get("steps") or []):
            details = step.get("details") or {}
            candidate = details.get("automation_port_owner")
            if candidate:
                owner = candidate
                break
        checks = {
            "real_wx_login_code_observed": bool((client.get("auth") or {}).get("code_present"))
            and int((client.get("auth") or {}).get("code_length") or 0) > 0,
            "backend_wechat_login_http_200": login_count_after > login_count_before,
            "postgresql_user_present": user is not None and bool(user.get("openid_hash")),
            "jwt_saved_in_isolated_storage": bool((client.get("storage") or {}).get("hasAccessToken")),
        }
        result = {
            "label": label,
            "started_at": started,
            "finished_at": iso_now(),
            "runtime_project": str(runtime_project),
            "automation_port": port,
            "devtools_pid": (owner or {}).get("Pid") or (owner or {}).get("pid"),
            "devtools_process_name": (owner or {}).get("ProcessName")
            or (owner or {}).get("process_name"),
            "wechat_account_marker": "current signed-in WeChat DevTools account; exact account id not exposed to automator",
            "wx_login_code": {
                "present": bool((client.get("auth") or {}).get("code_present")),
                "length": int((client.get("auth") or {}).get("code_length") or 0),
                "value_logged": False,
            },
            "backend_user": user,
            "database_user_count_after_probe": len(after),
            "wechat_login_http_200_delta": login_count_after - login_count_before,
            "checks": checks,
            "status": "PASS" if all(checks.values()) else "FAIL",
        }
        return result
    finally:
        try:
            if port_listener_pid(port) is not None:
                cleanup["client_cleanup"] = await asyncio.to_thread(
                    wechat._run_client,
                    runner,
                    runtime_project,
                    "cleanup",
                    45.0,
                    label,
                )
        except Exception as exc:
            cleanup["client_cleanup_error"] = f"{type(exc).__name__}: {exc}"
        cleanup["cli_close"] = _close_runtime_project(runtime_project)
        cleanup["automation_port_after_close"] = port_listener_pid(port)
        try:
            cleanup.update(wechat._remove_runtime_project(runtime_project, runner.run_dir))
        except Exception as exc:
            cleanup["runtime_project_removed"] = False
            cleanup["runtime_project_remove_error"] = f"{type(exc).__name__}: {exc}"
        wechat.WECHAT_AUTOMATION_PORT = old_port
        if result is not None:
            result["cleanup"] = cleanup


def _render_report(result: dict[str, Any]) -> str:
    lines = [
        "# Level 7 WeChat Real User Identity Capacity",
        "",
        f"- Overall: `{result.get('overall_status')}`",
        f"- FastAPI: `127.0.0.1:{APP_PORT}`",
        f"- PostgreSQL: `{DB_NAME}`",
        f"- Ollama proxy: `127.0.0.1:{PROXY_PORT}`",
        f"- REAL USER IDENTITY CAPACITY = `{result.get('real_user_identity_capacity')}`",
        f"- REAL MULTI-USER: `{result.get('real_multi_user_status')}`",
        "",
        "## Probes",
        "",
    ]
    for probe in result.get("identity_probes") or []:
        user = probe.get("backend_user") or {}
        lines.extend(
            [
                f"### {probe.get('label')}",
                "",
                f"- Runtime project: `{probe.get('runtime_project')}`",
                f"- Automation port: `{probe.get('automation_port')}`",
                f"- DevTools PID: `{probe.get('devtools_pid')}`",
                f"- Account marker: `{probe.get('wechat_account_marker')}`",
                f"- wx.login code: present=`{(probe.get('wx_login_code') or {}).get('present')}`, length=`{(probe.get('wx_login_code') or {}).get('length')}`, value_logged=`False`",
                f"- openid hash: `{user.get('openid_hash')}` length=`{user.get('openid_length')}`",
                f"- backend user_id: `{user.get('user_id')}`",
                f"- Status: `{probe.get('status')}`",
                "",
            ]
        )
    lines.extend(
        [
            "## Conclusion",
            "",
            result.get("identity_conclusion", ""),
            "",
            "No full wx.login code, openid, session_key, JWT, access token, AppSecret, or database password is written to this report.",
        ]
    )
    return "\n".join(lines) + "\n"


async def execute() -> tuple[int, Path]:
    preflight = wechat._preflight()
    runner = Level7Runner()
    runner.result.update(
        {
            "task": "real_wechat_identity_capacity_probe",
            "scope": "Step 1 only: wx.login -> code2session -> openid hash -> PostgreSQL user_id across A/B/C runtime projects",
            "preflight": preflight,
            "identity_probes": [],
            "dependencies": {
                "postgresql": "REAL",
                "http": "REAL",
                "auth": "REAL",
                "wechat_client": "REAL",
                "qwen": "NOT_EXECUTED",
                "piper": "NOT_EXECUTED",
            },
        }
    )
    failed = False

    try:
        print(f"[{iso_now()}] START isolated Level 7 backend setup", flush=True)
        await runner.setup()
        runner.result["initial_database_snapshot"] = runner.database.snapshot()
        for probe in PROBES:
            label = str(probe["label"])
            runtime_project = runner.run_dir / f"runtime-miniapp-{label}"
            print(
                f"[{iso_now()}] START identity probe {label} port={probe['port']}",
                flush=True,
            )
            try:
                probe_result = await _run_probe(runner, probe, runtime_project)
            except BaseException as exc:
                failed = True
                wechat._append_exception(runner.result, label, exc)
                probe_result = {
                    "label": label,
                    "runtime_project": str(runtime_project),
                    "automation_port": int(probe["port"]),
                    "status": "FAIL",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            runner.result["identity_probes"].append(probe_result)
            if probe_result.get("status") != "PASS":
                break

        users = _identity_rows(runner)
        unique_openids = {item["openid_hash"] for item in users}
        unique_user_ids = {item["user_id"] for item in users}
        passed_probes = [p for p in runner.result["identity_probes"] if p.get("status") == "PASS"]
        capacity = len(unique_openids)
        runner.result["final_identity_users"] = users
        runner.result["real_user_identity_capacity"] = capacity
        runner.result["real_multi_user_status"] = "PASS" if capacity >= 2 else "BLOCKED"
        if capacity >= 2:
            runner.result["identity_conclusion"] = (
                f"REAL USER IDENTITY CAPACITY >= {capacity}. "
                "At least two probes produced different real openid hashes and different backend user_id values."
            )
        else:
            runner.result["identity_conclusion"] = (
                f"REAL USER IDENTITY CAPACITY = {capacity}. REAL MULTI-USER BLOCKED: "
                "the completed DevTools runtime probes mapped to the same real WeChat identity, "
                "so this environment cannot honestly label 5/10/25/50/100 runs as real multi-user E2E. "
                "To expand, use additional real WeChat tester accounts or isolated DevTools profiles that produce distinct code2session openids."
            )
        runner.result["identity_checks"] = {
            "all_completed_probes_passed": len(passed_probes) == len(runner.result["identity_probes"]),
            "openid_hashes_unique_count": len(unique_openids),
            "backend_user_ids_unique_count": len(unique_user_ids),
            "secrets_logged": False,
        }
    except BaseException as exc:
        failed = True
        wechat._append_exception(runner.result, "identity_capacity_probe", exc)
        print(f"[{iso_now()}] FAIL {type(exc).__name__}: {exc}", flush=True)
    finally:
        try:
            runner.result["http_evidence"] = wechat._http_evidence(runner.run_dir / "uvicorn.log")
            write_json(runner.run_dir / "client-http-evidence.json", runner.result["http_evidence"])
        except Exception as exc:
            failed = True
            wechat._append_exception(runner.result, "http_evidence", exc)
        try:
            await runner.cleanup()
        except Exception as exc:
            failed = True
            wechat._append_exception(runner.result, "level7_cleanup", exc)
        cleanup = runner.result.get("cleanup") or {}
        cleanup_ok = (
            ((cleanup.get("ports_after_stop") or {}).get(str(APP_PORT)) is None)
            and ((cleanup.get("ports_after_stop") or {}).get(str(PROXY_PORT)) is None)
            and all(port_listener_pid(int(probe["port"])) is None for probe in PROBES)
        )
        runner.result["cleanup"]["identity_automation_ports_after_stop"] = {
            str(probe["port"]): port_listener_pid(int(probe["port"])) for probe in PROBES
        }
        runner.result["acceptance"] = {
            "real_wx_login": "PASS"
            if any(p.get("status") == "PASS" for p in runner.result.get("identity_probes", []))
            else "FAIL",
            "openid_hash_capacity_measured": "PASS"
            if "real_user_identity_capacity" in runner.result
            else "FAIL",
            "real_multi_user": runner.result.get("real_multi_user_status", "BLOCKED"),
            "environment_isolation": "PASS" if cleanup_ok else "FAIL",
            "card_crud_ai_tts_load": "NOT_EXECUTED_STEP1_ONLY",
        }
        runner.result["overall_status"] = "PASS" if not failed and cleanup_ok else "FAIL"
        runner.result["finished_at"] = iso_now()
        result = sanitize_value(runner.result)
        write_json(runner.run_dir / "result.json", result)
        (runner.run_dir / "REPORT.md").write_text(_render_report(result), encoding="utf-8")
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
