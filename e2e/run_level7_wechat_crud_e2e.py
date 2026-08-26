"""Step 2: real WeChat Mini Program Card CRUD E2E."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg

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


def _crud_database_audit(runner: Level7Runner, rounds: list[dict[str, Any]]) -> dict[str, Any]:
    texts = [item["updated_text"] for item in rounds]
    with psycopg.connect(runner.database.psycopg_dsn) as connection:
        users = connection.execute(
            "SELECT id, length(wx_openid)>0 FROM users ORDER BY created_at"
        ).fetchall()
        cards = connection.execute(
            """
            SELECT id, user_id, content, understanding, where_encountered,
                   status, version, deleted_at IS NOT NULL
            FROM cards
            WHERE content = ANY(%s)
            ORDER BY created_at, id
            """,
            (texts,),
        ).fetchall()
        all_cards_count = connection.execute("SELECT count(*) FROM cards").fetchone()[0]
        active_cards_count = connection.execute(
            "SELECT count(*) FROM cards WHERE deleted_at IS NULL"
        ).fetchone()[0]

    by_content = {
        row[2]: {
            "id": str(row[0]),
            "user_id": str(row[1]),
            "content": row[2],
            "understanding": row[3],
            "where_encountered": row[4],
            "status": row[5],
            "version": int(row[6]),
            "deleted": bool(row[7]),
        }
        for row in cards
    }
    expected_user_id = str(users[0][0]) if users else ""
    checks = {
        "exactly_one_real_user": len(users) == 1 and bool(users[0][1]),
        "three_crud_cards_in_database": len(cards) == 3,
        "all_crud_cards_soft_deleted": len(cards) == 3 and all(item["deleted"] for item in by_content.values()),
        "no_active_cards_after_delete": int(active_cards_count) == 0,
        "no_duplicate_records_for_rounds": len(cards) == len(set(texts)),
        "all_cards_owned_by_real_user": bool(expected_user_id)
        and all(item["user_id"] == expected_user_id for item in by_content.values()),
        "database_contains_only_round_cards": int(all_cards_count) == 3,
    }
    return {
        "users": [
            {"id": str(row[0]), "openid_present": bool(row[1])}
            for row in users
        ],
        "cards": list(by_content.values()),
        "all_cards_count": int(all_cards_count),
        "active_cards_count": int(active_cards_count),
        "checks": checks,
        "status": "PASS" if all(checks.values()) else "FAIL",
    }


def _crud_http_audit(runner: Level7Runner) -> dict[str, Any]:
    evidence = wechat._http_evidence(runner.run_dir / "uvicorn.log")
    status_counts: dict[str, int] = {}
    method_counts: dict[str, int] = {}
    for item in evidence.get("requests") or []:
        if not str(item.get("path") or "").startswith("/api/cards"):
            continue
        status = str(item.get("status_code"))
        method = str(item.get("method") or "")
        status_counts[status] = status_counts.get(status, 0) + 1
        method_counts[method] = method_counts.get(method, 0) + 1
    checks = {
        "card_post_seen": method_counts.get("POST", 0) >= 3,
        "card_patch_seen": method_counts.get("PATCH", 0) >= 3,
        "card_delete_seen": method_counts.get("DELETE", 0) >= 3,
        "no_card_5xx": not any(
            str(item.get("status_code") or "").startswith("5")
            for item in evidence.get("requests") or []
            if str(item.get("path") or "").startswith("/api/cards")
        ),
        "request_ids_present": evidence.get("request_ids_present", 0) > 0,
    }
    return {
        "status_counts": status_counts,
        "method_counts": method_counts,
        "checks": checks,
        "status": "PASS" if all(checks.values()) else "FAIL",
        "raw": evidence,
    }


def _render_report(result: dict[str, Any]) -> str:
    acceptance = result.get("acceptance") or {}
    lines = [
        "# Level 7 Real WeChat Client Card CRUD E2E",
        "",
        f"- Overall: `{result.get('overall_status')}`",
        f"- FastAPI: `127.0.0.1:{APP_PORT}`",
        f"- PostgreSQL: `{DB_NAME}`",
        f"- Ollama proxy: `127.0.0.1:{PROXY_PORT}`",
        f"- REAL CLIENT CARD CRUD: `{acceptance.get('real_client_card_crud')}`",
        f"- Environment isolation: `{acceptance.get('environment_isolation')}`",
        "",
        "## Evidence",
        "",
        "- Create/Read/Update/Delete were produced by Mini Program UI tap/input/confirm actions.",
        "- FastAPI evidence came from request logs with request-id/trace fields.",
        "- PostgreSQL evidence was read after client actions and before cleanup.",
        "- Full wx.login code, openid, session_key, JWT and AppSecret were not written to artifacts.",
        "",
        "## Rounds",
        "",
    ]
    for item in ((result.get("client") or {}).get("crud") or {}).get("rounds") or []:
        lines.extend(
            [
                f"- Round {item.get('round')}: create `{item.get('created_text')}`, update `{item.get('updated_text')}`, delete confirmed by UI.",
            ]
        )
    lines.extend(
        [
            "",
            "## Checks",
            "",
        ]
    )
    for key, value in ((result.get("database_audit") or {}).get("checks") or {}).items():
        lines.append(f"- DB {key}: `{value}`")
    for key, value in ((result.get("http_audit") or {}).get("checks") or {}).items():
        lines.append(f"- HTTP {key}: `{value}`")
    return "\n".join(lines) + "\n"


async def execute() -> tuple[int, Path]:
    preflight = wechat._preflight()
    runner = Level7Runner()
    runtime_project = runner.run_dir / "runtime-miniapp-crud"
    runner.result.update(
        {
            "task": "real_wechat_client_card_crud_e2e",
            "scope": "Step 2 only: 3 rounds of Create -> Read -> Update -> Delete through real Mini Program UI",
            "preflight": preflight,
            "client": {},
        }
    )
    failed = False
    client: dict[str, Any] | None = None
    database: dict[str, Any] | None = None
    http_audit: dict[str, Any] | None = None

    try:
        print(f"[{iso_now()}] START isolated Level 7 backend setup", flush=True)
        await runner.setup()
        runner.result["initial_database_snapshot"] = runner.database.snapshot()
        print(f"[{iso_now()}] START real WeChat client CRUD", flush=True)
        client = await asyncio.to_thread(
            wechat._run_client,
            runner,
            runtime_project,
            "crud",
            420.0,
            "single-identity-crud",
        )
        runner.result["client"]["crud"] = client
        database = _crud_database_audit(runner, client.get("rounds") or [])
        http_audit = _crud_http_audit(runner)
        runner.result["database_audit"] = database
        runner.result["http_audit"] = http_audit
        write_json(runner.run_dir / "crud-postgresql-audit.json", database)
        write_json(runner.run_dir / "crud-http-audit.json", http_audit)
        if database.get("status") != "PASS":
            raise E2EFailure(f"CRUD database audit failed: {database.get('checks')}")
        if http_audit.get("status") != "PASS":
            raise E2EFailure(f"CRUD HTTP audit failed: {http_audit.get('checks')}")
    except BaseException as exc:
        failed = True
        wechat._append_exception(runner.result, "wechat_card_crud_e2e", exc)
        print(f"[{iso_now()}] FAIL {type(exc).__name__}: {exc}", flush=True)
    finally:
        automation_cleanup: dict[str, Any] = {"started_at": iso_now()}
        if port_listener_pid(wechat.WECHAT_AUTOMATION_PORT) is not None:
            try:
                automation_cleanup["client_cleanup"] = await asyncio.to_thread(
                    wechat._run_client,
                    runner,
                    runtime_project,
                    "cleanup",
                    45.0,
                    "single-identity-crud",
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
                automation_cleanup["cli_close_returncode"] = completed.returncode
                time.sleep(2)
            except Exception as exc:
                automation_cleanup["cli_close_error"] = f"{type(exc).__name__}: {exc}"
        automation_cleanup["automation_port_after_close"] = port_listener_pid(wechat.WECHAT_AUTOMATION_PORT)
        if runtime_project.exists():
            try:
                automation_cleanup.update(wechat._remove_runtime_project(runtime_project, runner.run_dir))
            except Exception as exc:
                automation_cleanup["runtime_project_removed"] = False
                automation_cleanup["runtime_project_remove_error"] = f"{type(exc).__name__}: {exc}"
        else:
            automation_cleanup["runtime_project_removed"] = True
        automation_cleanup["finished_at"] = iso_now()

        try:
            if database is None and runner.database.created:
                rounds = ((client or {}).get("rounds") or [])
                database = _crud_database_audit(runner, rounds)
                runner.result["database_audit"] = database
                write_json(runner.run_dir / "crud-postgresql-audit.json", database)
        except Exception as exc:
            failed = True
            wechat._append_exception(runner.result, "crud_database_audit", exc)
        try:
            if http_audit is None:
                http_audit = _crud_http_audit(runner)
                runner.result["http_audit"] = http_audit
                write_json(runner.run_dir / "crud-http-audit.json", http_audit)
        except Exception as exc:
            failed = True
            wechat._append_exception(runner.result, "crud_http_audit", exc)
        try:
            await runner.cleanup()
        except Exception as exc:
            failed = True
            wechat._append_exception(runner.result, "level7_cleanup", exc)
        runner.result["cleanup"]["wechat_automation"] = automation_cleanup
        cleanup = runner.result.get("cleanup") or {}
        database_cleanup = cleanup.get("database") or {}
        cleanup_ok = (
            (database_cleanup.get("dropped") is True or database_cleanup.get("attempted") is False)
            and ((cleanup.get("ports_after_stop") or {}).get(str(APP_PORT)) is None)
            and ((cleanup.get("ports_after_stop") or {}).get(str(PROXY_PORT)) is None)
            and automation_cleanup.get("automation_port_after_close") is None
            and automation_cleanup.get("runtime_project_removed") is True
        )
        client_ok = bool(client and client.get("status") == "PASS" and len(client.get("rounds") or []) == 3)
        db_ok = bool(database and database.get("status") == "PASS")
        http_ok = bool(http_audit and http_audit.get("status") == "PASS")
        runner.result["acceptance"] = {
            "real_wx_login": "PASS" if client_ok else "FAIL",
            "real_client_card_crud": "PASS" if client_ok and db_ok and http_ok else "FAIL",
            "ui_evidence": "PASS" if client_ok else "FAIL",
            "api_evidence": "PASS" if http_ok else "FAIL",
            "postgresql_evidence": "PASS" if db_ok else "FAIL",
            "environment_isolation": "PASS" if cleanup_ok else "FAIL",
            "ai_tts_review": "NOT TESTED",
        }
        runner.result["overall_status"] = (
            "PASS" if not failed and cleanup_ok and client_ok and db_ok and http_ok else "FAIL"
        )
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
