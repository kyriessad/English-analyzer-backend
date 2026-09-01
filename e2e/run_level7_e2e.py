"""Run the Level 7 real-dependency E2E experiment on isolated resources.

The runner deliberately uses a real Uvicorn process, real HTTP, a freshly
migrated PostgreSQL database, the configured Qwen model through Ollama, and
real Piper voices.  WeChat platform login is not faked: isolated users and JWTs
are bootstrapped out of band and the report labels Auth as PARTIAL.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import UUID, uuid4

import httpx
import jwt
import psycopg
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy.engine import URL, make_url


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
DB_NAME = "english_analyzer_phase1_e2e"
DB_NAME_PATTERN = re.compile(r"^english_analyzer_phase1_e2e$")
HOST = "127.0.0.1"
APP_PORT = 18000
PROXY_PORT = 18114
APP_BASE = f"http://{HOST}:{APP_PORT}"
PROXY_BASE = f"http://{HOST}:{PROXY_PORT}"
STATUS_KEYS = (200, 201, 204, 400, 401, 403, 404, 409, 422, 429, 500, 503)
GAUGE_NAMES = (
    "http_requests_in_progress",
    "db_pool_checked_out",
    "db_pool_overflow",
    "ai_active",
    "ai_waiting",
    "ai_inflight_followers",
    "tts_active",
    "tts_waiting",
)
COUNTER_NAMES = (
    "db_pool_timeout_total",
    "ai_slot_timeout_total",
    "ai_queue_full_reject_total",
    "ai_inflight_follower_reject_total",
    "tts_slot_timeout_total",
    "tts_queue_full_reject_total",
)

REVIEWED_ITEM_WITHOUT_LOG_SQL = """
    SELECT count(*)
    FROM review_session_items i
    LEFT JOIN review_logs legacy_log ON legacy_log.session_item_id=i.id
    LEFT JOIN review_answer_logs answer_log ON answer_log.session_item_id=i.id
    WHERE i.status IN ('reviewed', 'done')
      AND legacy_log.id IS NULL
      AND answer_log.id IS NULL
"""

REVIEW_LOG_WITHOUT_REVIEWED_ITEM_SQL = """
    SELECT count(*)
    FROM (
        SELECT legacy_log.id
        FROM review_logs legacy_log
        JOIN review_session_items i ON i.id=legacy_log.session_item_id
        WHERE i.status NOT IN ('reviewed', 'done') OR i.reviewed_at IS NULL
        UNION ALL
        SELECT answer_log.id
        FROM review_answer_logs answer_log
        JOIN review_session_items i ON i.id=answer_log.session_item_id
        WHERE i.status NOT IN ('reviewed', 'done') OR i.reviewed_at IS NULL
    ) inconsistent_logs
"""


class E2EFailure(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, UUID, Path)):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Cannot encode {type(value).__name__}")


def redact_text(value: str) -> str:
    return re.sub(
        r"(?i)(postgresql(?:\+psycopg)?://[^:/@\s]+:)[^@\s]+@",
        r"\1<redacted>@",
        value,
    )


def sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: sanitize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_value(item) for item in value)
    return value


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(sanitize_value(value), ensure_ascii=False, indent=2, default=json_default),
        encoding="utf-8",
    )


def port_listener_pid(port: int) -> int | None:
    command = (
        f"$c=Get-NetTCPConnection -LocalPort {port} -State Listen "
        "-ErrorAction SilentlyContinue | Select-Object -First 1; "
        "if($c){$c.OwningProcess}"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    raw = completed.stdout.strip()
    return int(raw) if raw.isdigit() else None


def process_info(pid: int) -> dict[str, Any] | None:
    command = (
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' "
        "-ErrorAction SilentlyContinue; "
        "if($p){$p | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress}"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    raw = completed.stdout.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def stop_verified_listener(port: int, expected_marker: str) -> dict[str, Any]:
    """Force-stop only a listener proven to belong to this E2E process."""
    pid = port_listener_pid(port)
    if pid is None:
        return {"attempted": False, "port_closed": True}
    info = process_info(pid)
    command_line = str((info or {}).get("CommandLine") or "")
    if expected_marker not in command_line or str(port) not in command_line:
        return {
            "attempted": False,
            "port_closed": False,
            "refused_pid": pid,
            "reason": "listener identity did not match the E2E marker and port",
        }
    completed = subprocess.run(
        ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and port_listener_pid(port) is not None:
        time.sleep(0.1)
    return {
        "attempted": True,
        "pid": pid,
        "taskkill_exit_code": completed.returncode,
        "port_closed": port_listener_pid(port) is None,
    }


def require_port_free(port: int) -> None:
    pid = port_listener_pid(port)
    if pid is not None:
        raise E2EFailure(f"Refusing to start: port {port} is already owned by PID {pid}")


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


@dataclass
class RequestEvidence:
    timestamp: str
    stage: str
    user: str
    method: str
    path: str
    status: int
    latency_ms: float
    request_id: str | None
    error_code: str | None
    detail: str | None = None


@dataclass
class UserIdentity:
    label: str
    user_id: UUID
    token: str

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        result = {"Authorization": f"Bearer {self.token}"}
        if extra:
            result.update(extra)
        return result


@dataclass
class ProcessSample:
    timestamp: float
    cpu_seconds: float | None
    rss_bytes: int | None
    private_bytes: int | None
    gpu_utilization_pct: float | None
    gpu_memory_used_mib: float | None


class WindowsProcessSampler:
    """Sample one exact server PID plus whole-GPU data without psutil."""

    def __init__(self, pid: int, interval: float = 0.5) -> None:
        self.pid = pid
        self.interval = interval
        self.samples: list[ProcessSample] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _process_values(pid: int) -> tuple[float | None, int | None, int | None]:
        if os.name != "nt":
            return None, None, None

        class FILETIME(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_uint32),
                ("page_fault_count", ctypes.c_uint32),
                ("peak_working_set_size", ctypes.c_size_t),
                ("working_set_size", ctypes.c_size_t),
                ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                ("quota_paged_pool_usage", ctypes.c_size_t),
                ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                ("quota_non_paged_pool_usage", ctypes.c_size_t),
                ("pagefile_usage", ctypes.c_size_t),
                ("peak_pagefile_usage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        handle = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
        if not handle:
            return None, None, None
        try:
            creation, exit_time, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            cpu_seconds = None
            if kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                raw_kernel = (kernel.high << 32) | kernel.low
                raw_user = (user.high << 32) | user.low
                cpu_seconds = (raw_kernel + raw_user) / 10_000_000
            counters = PMC()
            counters.cb = ctypes.sizeof(PMC)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return cpu_seconds, int(counters.working_set_size), int(counters.pagefile_usage)
            return cpu_seconds, None, None
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def _gpu_values() -> tuple[float | None, float | None]:
        try:
            completed = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            first = completed.stdout.strip().splitlines()[0]
            util, memory = [float(part.strip()) for part in first.split(",")[:2]]
            return util, memory
        except Exception:
            return None, None

    def _run(self) -> None:
        gpu_tick = 0
        last_gpu = (None, None)
        while not self._stop.is_set():
            if gpu_tick % 2 == 0:
                last_gpu = self._gpu_values()
            cpu, rss, private = self._process_values(self.pid)
            self.samples.append(
                ProcessSample(time.time(), cpu, rss, private, last_gpu[0], last_gpu[1])
            )
            gpu_tick += 1
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="e2e-resource-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        cpu_values = [s.cpu_seconds for s in self.samples if s.cpu_seconds is not None]
        rss_values = [s.rss_bytes for s in self.samples if s.rss_bytes is not None]
        private_values = [s.private_bytes for s in self.samples if s.private_bytes is not None]
        gpu_values = [s.gpu_utilization_pct for s in self.samples if s.gpu_utilization_pct is not None]
        vram_values = [s.gpu_memory_used_mib for s in self.samples if s.gpu_memory_used_mib is not None]
        cpu_one_core = None
        cpu_host_capacity = None
        if len(cpu_values) >= 2 and len(self.samples) >= 2:
            wall = self.samples[-1].timestamp - self.samples[0].timestamp
            if wall > 0:
                cpu_one_core = max(0.0, (cpu_values[-1] - cpu_values[0]) / wall * 100)
                cpu_host_capacity = cpu_one_core / max(1, os.cpu_count() or 1)
        return {
            "sample_count": len(self.samples),
            "process_cpu_one_core_pct_avg": round(cpu_one_core, 3) if cpu_one_core is not None else None,
            "process_cpu_host_capacity_pct_avg": round(cpu_host_capacity, 3) if cpu_host_capacity is not None else None,
            "process_rss_mib_max": round(max(rss_values) / 1024 / 1024, 3) if rss_values else None,
            "process_private_mib_max": round(max(private_values) / 1024 / 1024, 3) if private_values else None,
            "gpu_utilization_pct_max_whole_gpu": max(gpu_values) if gpu_values else None,
            "gpu_memory_used_mib_max_whole_gpu": max(vram_values) if vram_values else None,
        }


class ManagedProcess:
    def __init__(self, name: str, command: list[str], cwd: Path, env: dict[str, str], log: Path) -> None:
        self.name = name
        self.command = command
        self.cwd = cwd
        self.env = env
        self.log_path = log
        self.process: subprocess.Popen[bytes] | None = None
        self._log_handle: Any = None

    def start(self) -> int:
        self._log_handle = self.log_path.open("wb")
        self.process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return self.process.pid

    def stop(self) -> dict[str, Any]:
        result = {"name": self.name, "pid": self.process.pid if self.process else None, "stopped": True}
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        if self.process:
            result["exit_code"] = self.process.returncode
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None
        return result


class E2EDatabase:
    def __init__(self, formal_url: URL, run_dir: Path) -> None:
        if not DB_NAME_PATTERN.fullmatch(DB_NAME):
            raise E2EFailure(f"Unsafe E2E database name: {DB_NAME}")
        if formal_url.get_backend_name() != "postgresql":
            raise E2EFailure("The configured formal DATABASE_URL is not PostgreSQL")
        if formal_url.database == DB_NAME:
            raise E2EFailure("Refusing to use the configured formal database as E2E target")
        self.formal_url = formal_url
        self.target_url = formal_url.set(database=DB_NAME)
        self.run_dir = run_dir
        self.created = False
        self.cleanup_attempt_required = False

    @property
    def target_dsn(self) -> str:
        return self.target_url.render_as_string(hide_password=False)

    @property
    def psycopg_dsn(self) -> str:
        return self.target_url.set(drivername="postgresql").render_as_string(hide_password=False)

    def _helper(self, *args: str, log_name: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["DATABASE_URL"] = self.formal_url.render_as_string(hide_password=False)
        completed = subprocess.run(
            [str(PYTHON), str(ROOT / "scripts" / "postgresql_test_database.py"), *args],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        (self.run_dir / log_name).write_text(
            completed.stdout + completed.stderr,
            encoding="utf-8",
        )
        return completed

    def create(self) -> dict[str, Any]:
        # A failed Alembic subprocess can leave the freshly created database
        # behind, so cleanup becomes mandatory before invoking the helper.
        self.cleanup_attempt_required = True
        completed = self._helper(
            "create",
            "--name",
            DB_NAME,
            "--recreate",
            "--migrate-head",
            log_name="database-create.log",
        )
        if completed.returncode != 0:
            raise E2EFailure("Fresh E2E database creation/migration failed")
        self.created = True
        seed_environment = os.environ.copy()
        seed_environment.update({
            "DATABASE_URL": self.target_dsn,
            "EXPECTED_DATABASE_DIALECT": "postgresql",
            "EXPECTED_DATABASE_NAME": DB_NAME,
            "ALLOW_SQLITE_FOR_TESTS": "false",
            "APP_ENV": "test",
        })
        seeded = subprocess.run(
            [str(PYTHON), str(ROOT / "scripts" / "seed_discovery_content.py"), "--word-limit", "500"],
            cwd=ROOT,
            env=seed_environment,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        (self.run_dir / "discovery-seed.log").write_text(seeded.stdout + seeded.stderr, encoding="utf-8")
        if seeded.returncode != 0:
            raise E2EFailure("Fresh E2E discovery content import failed")
        with psycopg.connect(self.psycopg_dsn) as connection:
            identity = connection.execute(
                "SELECT current_database(), current_schema(), version_num FROM alembic_version"
            ).fetchone()
        if not identity or identity[0] != DB_NAME:
            raise E2EFailure(f"Connected to unexpected database identity: {identity}")
        return {"database": identity[0], "schema": identity[1], "revision": identity[2]}

    def drop(self) -> dict[str, Any]:
        if not self.cleanup_attempt_required:
            return {"attempted": False, "dropped": False}
        completed = self._helper("drop", "--name", DB_NAME, log_name="database-drop.log")
        if completed.returncode == 0:
            self.cleanup_attempt_required = False
            self.created = False
        return {
            "attempted": True,
            "dropped": completed.returncode == 0,
            "exit_code": completed.returncode,
        }

    def seed_users(self, prefix: str, count: int, jwt_secret: str) -> list[UserIdentity]:
        identities: list[UserIdentity] = []
        now = utc_now()
        with psycopg.connect(self.psycopg_dsn) as connection:
            for index in range(count):
                user_id = uuid4()
                label = f"{prefix}-{index + 1:03d}"
                connection.execute(
                    """
                    INSERT INTO users (
                        id, wx_openid, timezone, account_status, role,
                        daily_goal, pronunciation_voice, token_version,
                        created_at, updated_at, last_login_at
                    ) VALUES (
                        %s, %s, 'Asia/Shanghai', 'active', 'user',
                        5, 'male', 0, %s, %s, %s
                    )
                    """,
                    (user_id, f"e2e:{prefix}:{index}:{uuid4().hex}", now, now, now),
                )
                token = jwt.encode(
                    {
                        "sub": str(user_id),
                        "ver": 0,
                        "iat": now,
                        "exp": now + timedelta(days=3),
                    },
                    jwt_secret,
                    algorithm="HS256",
                )
                identities.append(UserIdentity(label, user_id, token))
            connection.commit()
        return identities

    def snapshot(self) -> dict[str, Any]:
        tables = (
            "users",
            "cards",
            "review_sessions",
            "review_session_items",
            "review_records",
            "review_logs",
            "review_answer_logs",
            "review_mcq_questions",
            "card_fsrs_states",
            "client_actions",
            "resource_usage",
        )
        result: dict[str, Any] = {
            "captured_at": iso_now(),
            "counts": {},
            "status_counts": {},
            "violations": {},
        }
        with psycopg.connect(self.psycopg_dsn) as connection:
            for table in tables:
                row = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()
                result["counts"][table] = int(row[0]) if row else None
            result["violations"]["duplicate_client_actions"] = connection.execute(
                """
                SELECT count(*) FROM (
                    SELECT user_id, client_action_id FROM client_actions
                    GROUP BY user_id, client_action_id HAVING count(*) > 1
                ) duplicates
                """
            ).fetchone()[0]
            result["violations"]["duplicate_review_logs"] = connection.execute(
                """
                SELECT count(*) FROM (
                    SELECT session_item_id FROM review_logs
                    GROUP BY session_item_id HAVING count(*) > 1
                ) duplicates
                """
            ).fetchone()[0]
            result["violations"]["duplicate_review_answer_logs"] = connection.execute(
                """
                SELECT count(*) FROM (
                    SELECT session_item_id FROM review_answer_logs
                    GROUP BY session_item_id HAVING count(*) > 1
                ) duplicates
                """
            ).fetchone()[0]
            result["violations"]["duplicate_card_local_ids"] = connection.execute(
                """
                SELECT count(*) FROM (
                    SELECT user_id, local_temp_id FROM cards
                    WHERE local_temp_id IS NOT NULL
                    GROUP BY user_id, local_temp_id HAVING count(*) > 1
                ) duplicates
                """
            ).fetchone()[0]
            result["violations"]["review_item_cross_user"] = connection.execute(
                """
                SELECT count(*)
                FROM review_session_items i
                JOIN review_sessions s ON s.id=i.session_id
                JOIN cards c ON c.id=i.card_id
                WHERE s.user_id <> c.user_id
                """
            ).fetchone()[0]
            result["violations"]["review_log_cross_user"] = connection.execute(
                """
                SELECT count(*)
                FROM review_logs l
                JOIN review_sessions s ON s.id=l.session_id
                JOIN cards c ON c.id=l.card_id
                WHERE l.user_id <> s.user_id OR l.user_id <> c.user_id
                """
            ).fetchone()[0]
            result["violations"]["review_answer_log_cross_user"] = connection.execute(
                """
                SELECT count(*)
                FROM review_answer_logs l
                JOIN review_sessions s ON s.id=l.session_id
                JOIN review_mcq_questions q ON q.id=l.question_id
                WHERE l.user_id <> s.user_id OR l.user_id <> q.user_id
                """
            ).fetchone()[0]
            result["violations"]["processing_client_actions"] = connection.execute(
                "SELECT count(*) FROM client_actions WHERE status='processing'"
            ).fetchone()[0]
            result["violations"]["completed_session_progress_mismatch"] = connection.execute(
                """
                SELECT count(*) FROM review_sessions
                WHERE status='completed'
                  AND (reviewed_count <> total_count OR completed_at IS NULL)
                """
            ).fetchone()[0]
            result["violations"]["active_session_overcomplete"] = connection.execute(
                """
                SELECT count(*) FROM review_sessions
                WHERE status='active' AND total_count > 0 AND reviewed_count >= total_count
                """
            ).fetchone()[0]
            result["violations"]["reviewed_item_without_log"] = connection.execute(
                REVIEWED_ITEM_WITHOUT_LOG_SQL
            ).fetchone()[0]
            result["violations"]["review_log_without_reviewed_item"] = connection.execute(
                REVIEW_LOG_WITHOUT_REVIEWED_ITEM_SQL
            ).fetchone()[0]
            for label, query in (
                ("cards", "SELECT status, count(*) FROM cards GROUP BY status ORDER BY status"),
                (
                    "review_sessions",
                    "SELECT status, count(*) FROM review_sessions GROUP BY status ORDER BY status",
                ),
                (
                    "review_session_items",
                    "SELECT status, count(*) FROM review_session_items GROUP BY status ORDER BY status",
                ),
                (
                    "client_actions",
                    "SELECT status, count(*) FROM client_actions GROUP BY status ORDER BY status",
                ),
            ):
                result["status_counts"][label] = {
                    str(row[0]): int(row[1]) for row in connection.execute(query).fetchall()
                }
            result["resource_usage"] = [
                {"resource": row[0], "count": int(row[1]), "users": int(row[2])}
                for row in connection.execute(
                    "SELECT resource, sum(count), count(*) FROM resource_usage GROUP BY resource ORDER BY resource"
                ).fetchall()
            ]
            result["postgresql_sessions"] = [
                {"state": row[0] or "unknown", "count": int(row[1])}
                for row in connection.execute(
                    """
                    SELECT state, count(*) FROM pg_stat_activity
                    WHERE datname=current_database() AND pid <> pg_backend_pid()
                    GROUP BY state ORDER BY state
                    """
                ).fetchall()
            ]
        return result

    def user_state(self, user_id: UUID) -> dict[str, Any]:
        with psycopg.connect(self.psycopg_dsn) as connection:
            cards = connection.execute(
                """
                SELECT id, content, note, version, review_state, review_count,
                       last_review_result, status, deleted_at
                FROM cards WHERE user_id=%s ORDER BY created_at, id
                """,
                (user_id,),
            ).fetchall()
            sessions = connection.execute(
                "SELECT id, status, reviewed_count, total_count FROM review_sessions WHERE user_id=%s",
                (user_id,),
            ).fetchall()
            actions = connection.execute(
                "SELECT client_action_id, action_type, status FROM client_actions WHERE user_id=%s ORDER BY created_at",
                (user_id,),
            ).fetchall()
            usage = connection.execute(
                "SELECT resource, count FROM resource_usage WHERE user_id=%s ORDER BY resource",
                (user_id,),
            ).fetchall()
            return {
                "cards": [list(map(json_default_safe, row)) for row in cards],
                "sessions": [list(map(json_default_safe, row)) for row in sessions],
                "actions": [list(map(json_default_safe, row)) for row in actions],
                "resource_usage": [list(map(json_default_safe, row)) for row in usage],
            }


def json_default_safe(value: Any) -> Any:
    if isinstance(value, (datetime, UUID, Path)):
        return str(value)
    return value


class HttpRecorder:
    def __init__(self, base_url: str) -> None:
        limits = httpx.Limits(max_connections=240, max_keepalive_connections=120)
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(120.0, connect=5.0),
            limits=limits,
        )
        self.events: list[RequestEvidence] = []

    @staticmethod
    def _response_error(response: httpx.Response) -> tuple[str | None, str | None]:
        try:
            payload = response.json()
        except Exception:
            text = response.text[:300].strip() if response.content else None
            return None, text
        if not isinstance(payload, dict):
            return None, str(payload)[:300]
        code = payload.get("code")
        detail = payload.get("detail") or payload.get("error")
        if isinstance(detail, dict):
            code = code or detail.get("code")
            detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
        return str(code) if code else None, str(detail)[:500] if detail is not None else None

    def _record(
        self,
        *,
        stage: str,
        user: str,
        method: str,
        path: str,
        status: int,
        latency_ms: float,
        request_id: str | None,
        error_code: str | None,
        detail: str | None,
    ) -> None:
        self.events.append(
            RequestEvidence(
                timestamp=iso_now(),
                stage=stage,
                user=user,
                method=method,
                path=path.split("?", 1)[0],
                status=status,
                latency_ms=round(latency_ms, 3),
                request_id=request_id,
                error_code=error_code,
                detail=detail,
            )
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        stage: str,
        user: str,
        **kwargs: Any,
    ) -> httpx.Response:
        started = time.perf_counter()
        try:
            response = await self.client.request(method, path, **kwargs)
        except Exception as exc:
            self._record(
                stage=stage,
                user=user,
                method=method,
                path=path,
                status=0,
                latency_ms=(time.perf_counter() - started) * 1000,
                request_id=None,
                error_code=type(exc).__name__,
                detail=str(exc)[:500],
            )
            raise
        code, detail = self._response_error(response) if response.status_code >= 400 else (None, None)
        self._record(
            stage=stage,
            user=user,
            method=method,
            path=path,
            status=response.status_code,
            latency_ms=(time.perf_counter() - started) * 1000,
            request_id=response.headers.get("x-request-id"),
            error_code=code,
            detail=detail,
        )
        return response

    async def analyze_stream(
        self,
        identity: UserIdentity,
        text: str,
        *,
        stage: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        path = "/api/analyze-english/stream"
        started = time.perf_counter()
        response_status = 0
        request_id = None
        events: list[dict[str, Any]] = []
        arrivals: list[float] = []
        error_code = None
        detail = None
        headers = identity.headers(
            {"Idempotency-Key": idempotency_key, "Accept": "application/x-ndjson"}
        )
        try:
            async with self.client.stream(
                "POST",
                path,
                headers=headers,
                json={"text": text, "cardType": "auto", "targetLang": "zh", "forceRefresh": True},
            ) as response:
                response_status = response.status_code
                request_id = response.headers.get("x-request-id")
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    arrivals.append(time.perf_counter() - started)
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        value = {"type": "invalid_ndjson", "raw": line[:300]}
                    events.append(value)
                if response_status >= 400:
                    joined = "\n".join(
                        json.dumps(item, ensure_ascii=False) for item in events
                    )
                    try:
                        payload = json.loads(joined)
                    except Exception:
                        payload = {}
                    if isinstance(payload, dict):
                        error_code = payload.get("code")
                        detail = str(payload.get("detail") or payload.get("error") or joined)[:500]
                    else:
                        detail = joined[:500]
        except Exception as exc:
            error_code = type(exc).__name__
            detail = str(exc)[:500]
            self._record(
                stage=stage,
                user=identity.label,
                method="POST",
                path=path,
                status=0,
                latency_ms=(time.perf_counter() - started) * 1000,
                request_id=request_id,
                error_code=error_code,
                detail=detail,
            )
            raise
        total = time.perf_counter() - started
        self._record(
            stage=stage,
            user=identity.label,
            method="POST",
            path=path,
            status=response_status,
            latency_ms=total * 1000,
            request_id=request_id,
            error_code=str(error_code) if error_code else None,
            detail=detail,
        )
        final = next((item.get("data") for item in events if item.get("type") == "final"), None)
        done = next((item for item in events if item.get("type") == "done"), None)
        return {
            "status": response_status,
            "request_id": request_id,
            "event_types": [item.get("type") for item in events],
            "event_count": len(events),
            "ttfe_ms": round(arrivals[0] * 1000, 3) if arrivals else None,
            "last_event_ms": round(arrivals[-1] * 1000, 3) if arrivals else None,
            "arrival_span_ms": round((arrivals[-1] - arrivals[0]) * 1000, 3) if len(arrivals) >= 2 else 0,
            "final": final,
            "done": done,
            "error_code": error_code,
            "detail": detail,
        }

    async def close(self) -> None:
        await self.client.aclose()


def summarize_requests(events: list[RequestEvidence], stage: str, duration_seconds: float) -> dict[str, Any]:
    selected = [event for event in events if event.stage == stage]
    latencies = [event.latency_ms for event in selected]
    statuses = Counter(event.status for event in selected)
    return {
        "duration_seconds": round(duration_seconds, 3),
        "total_requests": len(selected),
        "success_count": sum(count for status, count in statuses.items() if 200 <= status < 300),
        "failure_count": sum(count for status, count in statuses.items() if not 200 <= status < 300),
        "status_counts": {str(key): statuses.get(key, 0) for key in STATUS_KEYS if statuses.get(key, 0)},
        "other_status_counts": {
            str(key): value for key, value in sorted(statuses.items()) if key not in STATUS_KEYS
        },
        "latency_ms": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
            "max": round(max(latencies), 3) if latencies else None,
        },
        "rps": round(len(selected) / duration_seconds, 3) if duration_seconds > 0 else None,
        "errors": dict(Counter(event.error_code or f"HTTP_{event.status}" for event in selected if event.status >= 400 or event.status == 0)),
    }


def parse_metrics(text: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            labels = ",".join(f"{key}={value}" for key, value in sorted(sample.labels.items()))
            key = sample.name + ("{" + labels + "}" if labels else "")
            result[key] = float(sample.value)
    return result


def metric_value(snapshot: dict[str, float], name: str) -> float:
    return sum(value for key, value in snapshot.items() if key == name or key.startswith(name + "{"))


async def fetch_metrics(client: httpx.AsyncClient) -> dict[str, float]:
    response = await client.get("/metrics")
    response.raise_for_status()
    return parse_metrics(response.text)


async def sample_metrics_during(
    client: httpx.AsyncClient,
    work: Awaitable[Any],
    *,
    interval: float = 0.15,
) -> tuple[Any, dict[str, Any]]:
    samples: list[dict[str, float]] = []
    finished = asyncio.Event()

    async def sampler() -> None:
        while not finished.is_set():
            try:
                samples.append(await fetch_metrics(client))
            except Exception:
                pass
            try:
                await asyncio.wait_for(finished.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(sampler())
    try:
        value = await work
    finally:
        finished.set()
        await task
        try:
            samples.append(await fetch_metrics(client))
        except Exception:
            pass
    maxima = {
        name: max((metric_value(sample, name) for sample in samples), default=None)
        for name in GAUGE_NAMES
    }
    return value, {"sample_count": len(samples), "max_gauges": maxima}


async def wait_for_gauge_baseline(client: httpx.AsyncClient, timeout: float = 20.0) -> dict[str, float]:
    deadline = time.monotonic() + timeout
    last: dict[str, float] = {}
    while time.monotonic() < deadline:
        last = await fetch_metrics(client)
        if all(metric_value(last, name) == 0 for name in ("ai_active", "ai_waiting", "ai_inflight_followers", "tts_active", "tts_waiting", "db_pool_checked_out")):
            return last
        await asyncio.sleep(0.2)
    raise E2EFailure(
        "Resource gauges did not return to baseline: "
        + json.dumps({name: metric_value(last, name) for name in GAUGE_NAMES})
    )


def counter_delta(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    return {
        name: metric_value(after, name) - metric_value(before, name)
        for name in COUNTER_NAMES
    }


def require_response(response: httpx.Response, expected: int, label: str) -> dict[str, Any]:
    if response.status_code != expected:
        raise E2EFailure(f"{label}: expected HTTP {expected}, got {response.status_code}: {response.text[:500]}")
    try:
        value = response.json()
    except Exception as exc:
        raise E2EFailure(f"{label}: response is not JSON") from exc
    if not isinstance(value, dict):
        raise E2EFailure(f"{label}: expected JSON object")
    return value


class Level7Runner:
    def __init__(self) -> None:
        raw_database_url = os.environ.get("DATABASE_URL", "").strip()
        if not raw_database_url:
            raise E2EFailure("DATABASE_URL must be supplied in the process environment")
        formal_url = make_url(raw_database_url)
        run_id = utc_now().strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
        self.run_dir = ROOT / ".e2e-artifacts" / run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.database = E2EDatabase(formal_url, self.run_dir)
        self.jwt_secret = secrets.token_urlsafe(48)
        self.control_token = secrets.token_urlsafe(32)
        self.failure_switch = self.run_dir / "ollama-unavailable.switch"
        self.piper_models = self.run_dir / "piper-models"
        self.tts_cache = self.run_dir / "tts-cache"
        self.piper_models.mkdir()
        self.tts_cache.mkdir()
        self.proxy: ManagedProcess | None = None
        self.server: ManagedProcess | None = None
        self.server_listener_pid: int | None = None
        self.recorder: HttpRecorder | None = None
        self.result: dict[str, Any] = {
            "run_id": run_id,
            "started_at": iso_now(),
            "artifact_dir": str(self.run_dir),
            "environment": {},
            "dependencies": {
                "postgresql": "REAL",
                "http": "REAL",
                "auth": "PARTIAL",
                "qwen": "REAL",
                "piper": "REAL",
                "wechat_client": "NOT_COVERED",
            },
            "stages": {},
            "exceptions": [],
            "cleanup": {},
        }

    def _prepare_piper_assets(self) -> list[dict[str, Any]]:
        source = ROOT / "data" / "piper"
        names = (
            "en_US-hfc_male-medium.onnx",
            "en_US-hfc_male-medium.onnx.json",
            "en_US-lessac-medium.onnx",
            "en_US-lessac-medium.onnx.json",
        )
        evidence: list[dict[str, Any]] = []
        for name in names:
            source_path = source / name
            target_path = self.piper_models / name
            if not source_path.is_file():
                raise E2EFailure(f"Required Piper asset is missing: {source_path}")
            os.link(source_path, target_path)
            evidence.append(
                {
                    "name": name,
                    "bytes": source_path.stat().st_size,
                    "source": str(source_path),
                    "isolated_link": str(target_path),
                }
            )
        return evidence

    @staticmethod
    async def _wait_http(url: str, timeout: float = 120.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_error = "not attempted"
        async with httpx.AsyncClient(timeout=3.0) as client:
            while time.monotonic() < deadline:
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        return response.json()
                    last_error = f"HTTP {response.status_code}"
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.25)
        raise E2EFailure(f"Timed out waiting for {url}: {last_error}")

    async def setup(self) -> None:
        require_port_free(APP_PORT)
        require_port_free(PROXY_PORT)
        db_identity = self.database.create()
        piper_assets = self._prepare_piper_assets()

        proxy_env = os.environ.copy()
        proxy_env.update(
            {
                "E2E_OLLAMA_UPSTREAM": "http://127.0.0.1:11434",
                "E2E_OLLAMA_FAILURE_SWITCH": str(self.failure_switch),
            }
        )
        self.proxy = ManagedProcess(
            "ollama-proxy",
            [
                str(PYTHON),
                "-m",
                "uvicorn",
                "e2e.ollama_proxy:app",
                "--host",
                HOST,
                "--port",
                str(PROXY_PORT),
                "--workers",
                "1",
            ],
            ROOT,
            proxy_env,
            self.run_dir / "ollama-proxy.log",
        )
        proxy_pid = self.proxy.start()
        proxy_health = await self._wait_http(f"{PROXY_BASE}/__e2e/proxy-health", timeout=30)
        async with httpx.AsyncClient(timeout=5.0) as probe:
            tags_response = await probe.get(f"{PROXY_BASE}/api/tags")
            tags_response.raise_for_status()
            model_names = [item.get("name") for item in tags_response.json().get("models", [])]
        if "qwen3:8b" not in model_names:
            raise E2EFailure(f"Real Ollama does not expose qwen3:8b: {model_names}")

        server_env = os.environ.copy()
        server_env.pop("REQUIRED_ALEMBIC_REVISION", None)
        server_env.update(
            {
                "APP_ENV": "e2e",
                "DATABASE_URL": self.database.target_dsn,
                "EXPECTED_DATABASE_NAME": DB_NAME,
                "EXPECTED_DATABASE_DIALECT": "postgresql",
                "EXPECTED_DATABASE_SCHEMA": "public",
                "ALLOW_SQLITE_FOR_TESTS": "false",
                "JWT_SECRET_KEY": self.jwt_secret,
                "JWT_ALGORITHM": "HS256",
                "OLLAMA_BASE_URL": PROXY_BASE,
                "OLLAMA_MODEL": "qwen3:8b",
                "PIPER_DATA_DIR": str(self.piper_models),
                "PIPER_AUDIO_CACHE_DIR": str(self.tts_cache),
                "ALLOWED_HOSTS": "127.0.0.1,localhost",
                "HTTP_LIMIT_CONCURRENCY": "30",
                "AI_GLOBAL_CONCURRENCY": "1",
                "AI_QUEUE_WAITING_CAPACITY": "2",
                "AI_INFLIGHT_FOLLOWER_CAPACITY": "3",
                "TTS_GLOBAL_CONCURRENCY": "1",
                "TTS_QUEUE_WAITING_CAPACITY": "2",
                "DB_POOL_SIZE": "5",
                "DB_MAX_OVERFLOW": "10",
                "DB_POOL_TIMEOUT": "3",
                "E2E_CONTROL_TOKEN": self.control_token,
                "LOG_LEVEL": "INFO",
            }
        )
        self.server = ManagedProcess(
            "e2e-fastapi",
            [
                str(PYTHON),
                "-m",
                "uvicorn",
                "e2e.lab_app:app",
                "--host",
                HOST,
                "--port",
                str(APP_PORT),
                "--workers",
                "1",
                "--limit-concurrency",
                "30",
            ],
            ROOT,
            server_env,
            self.run_dir / "uvicorn.log",
        )
        server_pid = self.server.start()
        health = await self._wait_http(f"{APP_BASE}/health", timeout=120)
        listener_pid = port_listener_pid(APP_PORT)
        listener_info = process_info(listener_pid) if listener_pid is not None else None
        listener_command = str((listener_info or {}).get("CommandLine") or "")
        if (
            listener_pid is None
            or "e2e.lab_app:app" not in listener_command
            or str(APP_PORT) not in listener_command
        ):
            raise E2EFailure(
                f"E2E port identity mismatch: launcher PID {server_pid}, listener PID {listener_pid}, "
                f"listener={listener_info}"
            )
        self.server_listener_pid = listener_pid
        self.recorder = HttpRecorder(APP_BASE)
        initial_metrics = await fetch_metrics(self.recorder.client)
        self.result["environment"] = {
            "app_base": APP_BASE,
            "app_port": APP_PORT,
            "app_pid": server_pid,
            "listener_pid": listener_pid,
            "listener_parent_pid": (listener_info or {}).get("ParentProcessId"),
            "listener_name": (listener_info or {}).get("Name"),
            "formal_port_8000_listener": port_listener_pid(8000),
            "proxy_base": PROXY_BASE,
            "proxy_pid": proxy_pid,
            "proxy_health": proxy_health,
            "database": db_identity,
            "database_formal_name": self.database.formal_url.database,
            "piper_assets": piper_assets,
            "tts_cache": str(self.tts_cache),
            "health": health,
            "ollama_models": model_names,
            "initial_gauges": {name: metric_value(initial_metrics, name) for name in GAUGE_NAMES},
            "wechat_automation": {
                "developer_cli_present": True,
                "service_port_enabled": False,
                "reason": "Developer Tools CLI reported that IDE Service Port is disabled; it was not enabled implicitly.",
            },
        }

    @property
    def http(self) -> HttpRecorder:
        if self.recorder is None:
            raise E2EFailure("HTTP recorder is not initialized")
        return self.recorder

    async def single_user(self) -> dict[str, Any]:
        stage = "single_user"
        started = time.perf_counter()
        baseline = await wait_for_gauge_baseline(self.http.client)
        identity = self.database.seed_users("single", 1, self.jwt_secret)[0]
        before_state = self.database.user_state(identity.user_id)

        me = require_response(
            await self.http.request("GET", "/api/auth/me", stage=stage, user=identity.label, headers=identity.headers()),
            200,
            "authenticated /me",
        )
        if me.get("id") != str(identity.user_id):
            raise E2EFailure("Authenticated user identity does not match the isolated bootstrap user")

        create_payload = {
            "content": "meticulous",
            "card_type": "word",
            "translation": "一丝不苟的",
            "understanding": "showing great attention to detail",
            "analysis_status": "done",
            "analysis_level": "pass",
            "analysis_messages": ["level7-e2e"],
            "understanding_source": "ai",
            "where_encountered": "Level 7 E2E",
        }
        created = require_response(
            await self.http.request(
                "POST", "/api/cards", stage=stage, user=identity.label, headers=identity.headers(), json=create_payload
            ),
            200,
            "create card",
        )
        card_id = created["id"]
        fetched = require_response(
            await self.http.request(
                "GET", f"/api/cards/{card_id}", stage=stage, user=identity.label, headers=identity.headers()
            ),
            200,
            "read card",
        )
        updated = require_response(
            await self.http.request(
                "PATCH",
                f"/api/cards/{card_id}",
                stage=stage,
                user=identity.label,
                headers=identity.headers(),
                json={"base_version": fetched["version"], "note": "single-user-updated"},
            ),
            200,
            "update card",
        )
        sync_action = f"single-sync-{uuid4()}"
        sync_payload = {
            "client_action_id": sync_action,
            "operation": "UPDATE",
            "local_id": "single-local-card",
            "card_id": card_id,
            "base_version": updated["version"],
            "payload": {"note": "single-user-synced"},
        }
        sync_first = require_response(
            await self.http.request(
                "POST", "/api/cards/sync", stage=stage, user=identity.label, headers=identity.headers(), json=sync_payload
            ),
            200,
            "sync card",
        )
        sync_replay = require_response(
            await self.http.request(
                "POST", "/api/cards/sync", stage=stage, user=identity.label, headers=identity.headers(), json=sync_payload
            ),
            200,
            "sync replay",
        )
        if not sync_replay.get("replayed") or sync_replay["card"]["id"] != card_id:
            raise E2EFailure("Sync replay did not return the original successful result")

        stream = await self.http.analyze_stream(
            identity,
            "meticulous",
            stage=stage,
            idempotency_key=f"single-ai-{uuid4()}",
        )
        if stream["status"] != 200:
            raise E2EFailure(f"Real Qwen streaming failed: {stream}")
        if "final" not in stream["event_types"] or "done" not in stream["event_types"]:
            raise E2EFailure(f"Streaming did not emit final and done: {stream['event_types']}")
        if stream["event_count"] < 3 or stream["arrival_span_ms"] <= 0:
            raise E2EFailure("Streaming response was not observed as multiple time-separated NDJSON events")
        final_ai = stream.get("final") or {}
        qwen_proof = {
            "analysis_model": final_ai.get("analysisModel"),
            "analysis_source": final_ai.get("analysisSource"),
            "example_source": final_ai.get("exampleSource"),
            "done": stream.get("done"),
        }

        tts_response = await self.http.request(
            "GET",
            "/api/pronunciation/audio",
            stage=stage,
            user=identity.label,
            headers=identity.headers(),
            params={"text": "meticulous level seven", "voice": "male"},
        )
        if tts_response.status_code != 200 or not tts_response.content.startswith(b"RIFF") or len(tts_response.content) <= 44:
            raise E2EFailure(
                f"Real Piper audio invalid: status={tts_response.status_code}, bytes={len(tts_response.content)}"
            )
        tts_proof = {
            "content_type": tts_response.headers.get("content-type"),
            "bytes": len(tts_response.content),
            "sha256": hashlib.sha256(tts_response.content).hexdigest(),
            "riff": True,
        }

        session = require_response(
            await self.http.request(
                "POST",
                "/api/review-sessions",
                stage=stage,
                user=identity.label,
                headers=identity.headers(),
                json={"session_type": "daily_suggested", "limit": 1, "restart": True},
            ),
            200,
            "create review session",
        )
        if not session.get("session_id") or len(session.get("items") or []) != 1:
            raise E2EFailure(f"Review session did not contain exactly one item: {session}")
        item = session["items"][0]
        feedback_action = f"single-review-{uuid4()}"
        feedback = require_response(
            await self.http.request(
                "POST",
                "/api/reviews/feedback",
                stage=stage,
                user=identity.label,
                headers=identity.headers(),
                json={
                    "client_action_id": feedback_action,
                    "session_id": session["session_id"],
                    "session_item_id": item["session_item_id"],
                    "card_id": item["card_id"],
                    "question_id": item["question_id"],
                    "selected_option_id": next(
                        option["option_id"]
                        for option in item["options"]
                        if option.get("option_id") == "correct"
                    ),
                    "result": "got_it",
                },
            ),
            200,
            "submit review feedback",
        )
        summary = require_response(
            await self.http.request(
                "GET",
                f"/api/reviews/sessions/{session['session_id']}/summary",
                stage=stage,
                user=identity.label,
                headers=identity.headers(),
            ),
            200,
            "review summary",
        )
        final_card = require_response(
            await self.http.request(
                "GET", f"/api/cards/{card_id}", stage=stage, user=identity.label, headers=identity.headers()
            ),
            200,
            "final card read",
        )
        if final_card.get("note") != "single-user-synced":
            raise E2EFailure("Final card content does not match the sync update")
        if summary.get("status") != "completed" or not feedback.get("done"):
            raise E2EFailure(f"Review did not reach completed state: feedback={feedback}, summary={summary}")

        logout = require_response(
            await self.http.request("POST", "/api/auth/logout", stage=stage, user=identity.label, headers=identity.headers()),
            200,
            "logout",
        )
        revoked = await self.http.request(
            "GET", "/api/auth/me", stage=stage, user=identity.label, headers=identity.headers()
        )
        if revoked.status_code != 401:
            raise E2EFailure(f"Old token remained valid after logout: HTTP {revoked.status_code}")

        final_metrics = await wait_for_gauge_baseline(self.http.client)
        after_state = self.database.user_state(identity.user_id)
        usage = {row[0]: row[1] for row in after_state["resource_usage"]}
        if usage.get("ai") != 1 or usage.get("tts") != 1:
            raise E2EFailure(f"Unexpected single-user quota state: {usage}")
        duration = time.perf_counter() - started
        return {
            "status": "PASS",
            "auth_boundary": "User/JWT bootstrap was out-of-band; bearer verification and logout revocation were real HTTP + PostgreSQL.",
            "user_id": str(identity.user_id),
            "card_id": card_id,
            "sync_action": sync_action,
            "feedback_action": feedback_action,
            "logout": logout,
            "stream": {key: value for key, value in stream.items() if key != "final"},
            "qwen_proof": qwen_proof,
            "piper_proof": tts_proof,
            "review_summary": summary,
            "state_before": before_state,
            "state_after": after_state,
            "baseline_gauges": {name: metric_value(baseline, name) for name in GAUGE_NAMES},
            "final_gauges": {name: metric_value(final_metrics, name) for name in GAUGE_NAMES},
            "requests": summarize_requests(self.http.events, stage, duration),
        }

    async def _correctness_user(self, identity: UserIdentity, index: int, stage: str) -> dict[str, Any]:
        content = f"isolation-word-{index}-{secrets.token_hex(2)}"
        created = require_response(
            await self.http.request(
                "POST",
                "/api/cards",
                stage=stage,
                user=identity.label,
                headers=identity.headers(),
                json={
                    "content": content,
                    "card_type": "word",
                    "translation": f"隔离词 {index}",
                    "understanding": f"owned only by {identity.label}",
                    "analysis_status": "done",
                    "analysis_level": "pass",
                    "understanding_source": "user",
                },
            ),
            200,
            f"{identity.label} create",
        )
        card_id = created["id"]
        fetched = require_response(
            await self.http.request(
                "GET", f"/api/cards/{card_id}", stage=stage, user=identity.label, headers=identity.headers()
            ),
            200,
            f"{identity.label} read",
        )
        updated = require_response(
            await self.http.request(
                "PATCH",
                f"/api/cards/{card_id}",
                stage=stage,
                user=identity.label,
                headers=identity.headers(),
                json={"base_version": fetched["version"], "note": f"owner-note-{index}"},
            ),
            200,
            f"{identity.label} update",
        )
        action_id = f"isolation-sync-{index}-{uuid4()}"
        sync_payload = {
            "client_action_id": action_id,
            "operation": "UPDATE",
            "local_id": f"isolation-local-{index}",
            "card_id": card_id,
            "base_version": updated["version"],
            "payload": {"note": f"owner-synced-{index}"},
        }
        sync = require_response(
            await self.http.request(
                "POST", "/api/cards/sync", stage=stage, user=identity.label, headers=identity.headers(), json=sync_payload
            ),
            200,
            f"{identity.label} sync",
        )
        replay = require_response(
            await self.http.request(
                "POST", "/api/cards/sync", stage=stage, user=identity.label, headers=identity.headers(), json=sync_payload
            ),
            200,
            f"{identity.label} replay",
        )
        if not replay.get("replayed"):
            raise E2EFailure(f"{identity.label} sync replay was not idempotent")
        session = require_response(
            await self.http.request(
                "POST",
                "/api/review-sessions",
                stage=stage,
                user=identity.label,
                headers=identity.headers(),
                json={"session_type": "daily_suggested", "limit": 1, "restart": True},
            ),
            200,
            f"{identity.label} session",
        )
        item = (session.get("items") or [None])[0]
        if not item:
            raise E2EFailure(f"{identity.label} review session has no item")
        review_action = f"isolation-review-{index}-{uuid4()}"
        require_response(
            await self.http.request(
                "POST",
                "/api/reviews/feedback",
                stage=stage,
                user=identity.label,
                headers=identity.headers(),
                json={
                    "client_action_id": review_action,
                    "session_id": session["session_id"],
                    "session_item_id": item["session_item_id"],
                    "card_id": item["card_id"],
                    "question_id": item["question_id"],
                    "selected_option_id": next(
                        option["option_id"]
                        for option in item["options"]
                        if option.get("option_id") == "correct"
                    ),
                    "result": "got_it",
                },
            ),
            200,
            f"{identity.label} feedback",
        )
        return {
            "identity": identity,
            "card_id": card_id,
            "card_version": sync["card"]["version"],
            "content": content,
            "note": f"owner-synced-{index}",
            "sync_action": action_id,
            "sync_payload": sync_payload,
            "session_id": session["session_id"],
            "session_item_id": item["session_item_id"],
            "review_action": review_action,
        }

    async def multi_user_isolation(self) -> dict[str, Any]:
        stage = "multi_user_isolation"
        started = time.perf_counter()
        identities = self.database.seed_users("isolation", 5, self.jwt_secret)
        flows = await asyncio.gather(
            *(self._correctness_user(identity, index, stage) for index, identity in enumerate(identities, 1))
        )

        expensive: list[dict[str, Any]] = []
        for index, identity in enumerate(identities, 1):
            stream = await self.http.analyze_stream(
                identity,
                f"resilient isolation example {index}",
                stage=stage,
                idempotency_key=f"isolation-ai-{index}-{uuid4()}",
            )
            if stream["status"] != 200 or "done" not in stream["event_types"]:
                raise E2EFailure(f"{identity.label} real AI failed: {stream}")
            audio = await self.http.request(
                "GET",
                "/api/pronunciation/audio",
                stage=stage,
                user=identity.label,
                headers=identity.headers(),
                params={"text": f"isolated pronunciation {index}", "voice": "female"},
            )
            if audio.status_code != 200 or not audio.content.startswith(b"RIFF"):
                raise E2EFailure(f"{identity.label} real TTS failed: HTTP {audio.status_code}")
            expensive.append(
                {
                    "user": identity.label,
                    "ai_request_id": stream["request_id"],
                    "ai_events": stream["event_types"],
                    "tts_bytes": len(audio.content),
                }
            )

        attacker = flows[0]
        victim = flows[1]
        attacker_identity: UserIdentity = attacker["identity"]
        victim_identity: UserIdentity = victim["identity"]
        victim_before = self.database.user_state(victim_identity.user_id)
        attacks: list[dict[str, Any]] = []

        async def attack(method: str, path: str, **kwargs: Any) -> httpx.Response:
            response = await self.http.request(
                method,
                path,
                stage=stage,
                user=f"{attacker_identity.label}->victim",
                headers=attacker_identity.headers(),
                **kwargs,
            )
            attacks.append(
                {
                    "method": method,
                    "path": path,
                    "status": response.status_code,
                    "request_id": response.headers.get("x-request-id"),
                }
            )
            if 200 <= response.status_code < 300 or response.status_code >= 500:
                raise E2EFailure(
                    f"P0 isolation result for {method} {path}: HTTP {response.status_code}"
                )
            return response

        await attack("GET", f"/api/cards/{victim['card_id']}")
        await attack(
            "PATCH",
            f"/api/cards/{victim['card_id']}",
            json={"base_version": victim["card_version"], "note": "attacker-write"},
        )
        await attack(
            "DELETE",
            f"/api/cards/{victim['card_id']}",
            params={"base_version": victim["card_version"]},
        )
        hostile_sync = dict(victim["sync_payload"])
        hostile_sync["base_version"] = victim["card_version"]
        hostile_sync["payload"] = {"note": "attacker-sync"}
        await attack("POST", "/api/cards/sync", json=hostile_sync)
        await attack(
            "POST",
            "/api/reviews/feedback",
            json={
                "client_action_id": victim["review_action"],
                "session_id": victim["session_id"],
                "session_item_id": victim["session_item_id"],
                "card_id": victim["card_id"],
                "result": "fluent",
            },
        )

        victim_after = self.database.user_state(victim_identity.user_id)
        if victim_before != victim_after:
            raise E2EFailure("P0: victim PostgreSQL state changed after cross-user attacks")
        snapshot = self.database.snapshot()
        cross_user_violations = {
            key: value
            for key, value in snapshot["violations"].items()
            if "cross_user" in key and value
        }
        if cross_user_violations:
            raise E2EFailure(f"P0: cross-user rows found in PostgreSQL: {cross_user_violations}")
        final_metrics = await wait_for_gauge_baseline(self.http.client)
        duration = time.perf_counter() - started
        return {
            "status": "PASS",
            "users": 5,
            "normal_flows": [
                {key: json_default_safe(value) for key, value in flow.items() if key not in {"identity", "sync_payload"}}
                for flow in flows
            ],
            "real_ai_tts": expensive,
            "attacks": attacks,
            "victim_unchanged": True,
            "database_snapshot": snapshot,
            "final_gauges": {name: metric_value(final_metrics, name) for name in GAUGE_NAMES},
            "requests": summarize_requests(self.http.events, stage, duration),
        }

    async def _load_user(
        self,
        identity: UserIdentity,
        index: int,
        users: int,
        stage: str,
        ai_indices: set[int],
        tts_indices: set[int],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"user": identity.label, "completed": []}
        rng = random.Random(users * 10_000 + index)
        try:
            me = await self.http.request(
                "GET", "/api/auth/me", stage=stage, user=identity.label, headers=identity.headers()
            )
            if me.status_code == 200:
                result["completed"].append("auth")
            await asyncio.sleep(rng.uniform(0.02, 0.18))

            create = await self.http.request(
                "POST",
                "/api/cards",
                stage=stage,
                user=identity.label,
                headers=identity.headers(),
                json={
                    "content": f"load-{users}-{index}-{secrets.token_hex(2)}",
                    "card_type": "word",
                    "translation": f"负载词 {index}",
                    "understanding": f"owned by {identity.label}",
                    "analysis_status": "done",
                    "analysis_level": "pass",
                    "understanding_source": "user",
                },
            )
            card: dict[str, Any] | None = create.json() if create.status_code == 200 else None
            if card:
                result["completed"].append("create")
                await asyncio.sleep(rng.uniform(0.02, 0.12))
                read = await self.http.request(
                    "GET",
                    f"/api/cards/{card['id']}",
                    stage=stage,
                    user=identity.label,
                    headers=identity.headers(),
                )
                if read.status_code == 200:
                    result["completed"].append("read")
                    patched = await self.http.request(
                        "PATCH",
                        f"/api/cards/{card['id']}",
                        stage=stage,
                        user=identity.label,
                        headers=identity.headers(),
                        json={"base_version": card["version"], "note": f"load-note-{users}-{index}"},
                    )
                    if patched.status_code == 200:
                        result["completed"].append("update")
                        card = patched.json()

                if index % 2 == 0 and card:
                    sync = await self.http.request(
                        "POST",
                        "/api/cards/sync",
                        stage=stage,
                        user=identity.label,
                        headers=identity.headers(),
                        json={
                            "client_action_id": f"load-sync-{users}-{index}-{uuid4()}",
                            "operation": "UPDATE",
                            "local_id": f"load-local-{users}-{index}",
                            "card_id": card["id"],
                            "base_version": card["version"],
                            "payload": {"where_encountered": f"mixed-load-{users}"},
                        },
                    )
                    if sync.status_code == 200:
                        result["completed"].append("sync")

                if index % 3 == 0:
                    session = await self.http.request(
                        "POST",
                        "/api/review-sessions",
                        stage=stage,
                        user=identity.label,
                        headers=identity.headers(),
                        json={"session_type": "daily_suggested", "limit": 1, "restart": True},
                    )
                    if session.status_code == 200:
                        session_data = session.json()
                        items = session_data.get("items") or []
                        if items:
                            item = items[0]
                            feedback = await self.http.request(
                                "POST",
                                "/api/reviews/feedback",
                                stage=stage,
                                user=identity.label,
                                headers=identity.headers(),
                                json={
                                    "client_action_id": f"load-review-{users}-{index}-{uuid4()}",
                                    "session_id": session_data["session_id"],
                                    "session_item_id": item["session_item_id"],
                                    "card_id": item["card_id"],
                                    "question_id": item["question_id"],
                                    "selected_option_id": next(
                                        option["option_id"]
                                        for option in item["options"]
                                        if option.get("option_id") == "correct"
                                    ),
                                    "result": "got_it",
                                },
                            )
                            if feedback.status_code == 200:
                                result["completed"].append("review")

            await asyncio.sleep(rng.uniform(0.02, 0.15))
            if index in ai_indices:
                stream = await self.http.analyze_stream(
                    identity,
                    f"mixed workload phrase {users} user {index}",
                    stage=stage,
                    idempotency_key=f"load-ai-{users}-{index}-{uuid4()}",
                )
                result["ai_status"] = stream["status"]
                if stream["status"] == 200:
                    result["completed"].append("ai")
            if index in tts_indices:
                audio = await self.http.request(
                    "GET",
                    "/api/pronunciation/audio",
                    stage=stage,
                    user=identity.label,
                    headers=identity.headers(),
                    params={"text": f"mixed load pronunciation {users} {index}", "voice": "male"},
                )
                result["tts_status"] = audio.status_code
                if audio.status_code == 200 and audio.content.startswith(b"RIFF"):
                    result["completed"].append("tts")
        except Exception as exc:
            result["exception"] = f"{type(exc).__name__}: {exc}"
        return result

    async def load_level(self, users: int) -> dict[str, Any]:
        stage = f"load_{users}"
        identities = self.database.seed_users(stage, users, self.jwt_secret)
        ai_count = {5: 1, 10: 1, 30: 2, 100: 5}[users]
        tts_count = {5: 1, 10: 1, 30: 2, 100: 5}[users]
        ai_indices = set(range(1, ai_count + 1))
        tts_indices = set(range(ai_count + 1, ai_count + tts_count + 1))
        before_metrics = await wait_for_gauge_baseline(self.http.client)
        if self.server_listener_pid is None:
            raise E2EFailure("FastAPI listener PID is unavailable for resource sampling")
        sampler = WindowsProcessSampler(self.server_listener_pid)
        sampler.start()
        started = time.perf_counter()
        work = asyncio.gather(
            *(
                self._load_user(identity, index, users, stage, ai_indices, tts_indices)
                for index, identity in enumerate(identities, 1)
            )
        )
        outcomes, metric_samples = await sample_metrics_during(self.http.client, work)
        duration = time.perf_counter() - started
        resource_samples = sampler.stop()
        after_metrics = await wait_for_gauge_baseline(self.http.client, timeout=40)
        recovery = await self.http.request(
            "GET",
            "/api/auth/me",
            stage=stage,
            user=f"{stage}-recovery",
            headers=identities[0].headers(),
        )
        snapshot = self.database.snapshot()
        summary = summarize_requests(self.http.events, stage, duration)
        stage_events = [event for event in self.http.events if event.stage == stage]
        serious = []
        if any(event.status == 500 or event.status == 0 for event in stage_events):
            serious.append("HTTP_500_OR_TRANSPORT_ERROR")
        if recovery.status_code != 200:
            serious.append("NO_RECOVERY")
        if snapshot["violations"]["review_item_cross_user"] or snapshot["violations"]["review_log_cross_user"]:
            serious.append("CROSS_USER_DATABASE_POLLUTION")
        if snapshot["violations"]["processing_client_actions"]:
            serious.append("PROCESSING_CLIENT_ACTION_LEAK")
        if any(
            snapshot["violations"][name]
            for name in (
                "completed_session_progress_mismatch",
                "active_session_overcomplete",
                "reviewed_item_without_log",
                "review_log_without_reviewed_item",
            )
        ):
            serious.append("REVIEW_TRANSACTION_STATE_MISMATCH")
        return {
            "status": "PASS" if not serious else "FAIL",
            "users": users,
            "behavior_model": {
                "all_users": ["auth", "create card", "read card", "update card"],
                "every_second_user": "sync update",
                "every_third_user": "review session + feedback",
                "ai_requests": ai_count,
                "tts_requests": tts_count,
                "think_time_seconds": "deterministic random 0.02-0.18",
                "reason": "Read/CRUD dominate; review/sync are medium-frequency; AI/TTS are sparse and expensive.",
            },
            "requests": summary,
            "user_outcomes": outcomes,
            "metrics": {
                **metric_samples,
                "counter_delta": counter_delta(before_metrics, after_metrics),
                "final_gauges": {name: metric_value(after_metrics, name) for name in GAUGE_NAMES},
            },
            "resources": resource_samples,
            "recovery_http_status": recovery.status_code,
            "database_snapshot": snapshot,
            "serious_conditions": serious,
        }

    async def http_burst(self) -> dict[str, Any]:
        stage = "burst_http"
        before = await wait_for_gauge_baseline(self.http.client)
        started = time.perf_counter()
        control_headers = {"X-E2E-Control-Token": self.control_token}
        tasks = [
            self.http.request(
                "GET",
                "/__e2e/http-hold",
                stage=stage,
                user=f"http-burst-{index:03d}",
                headers=control_headers,
                params={"seconds": 1.5},
            )
            for index in range(1, 101)
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        duration = time.perf_counter() - started
        after = await wait_for_gauge_baseline(self.http.client)
        statuses = Counter(
            response.status_code if isinstance(response, httpx.Response) else 0 for response in responses
        )
        if statuses[503] == 0 or statuses[500] or statuses[0]:
            raise E2EFailure(f"HTTP burst did not fail fast and safely: {dict(statuses)}")
        if statuses[200] > 30:
            raise E2EFailure(f"Uvicorn accepted more than configured concurrency 30: {dict(statuses)}")
        recovery = await self.http.request(
            "GET", "/health", stage=stage, user="http-burst-recovery"
        )
        if recovery.status_code != 200:
            raise E2EFailure("HTTP burst recovery probe failed")
        return {
            "status": "PASS",
            "requests": summarize_requests(self.http.events, stage, duration),
            "status_counts": dict(statuses),
            "accepted_max_assertion": "HTTP 200 count <= 30",
            "db_gauge_before": metric_value(before, "db_pool_checked_out"),
            "db_gauge_after": metric_value(after, "db_pool_checked_out"),
            "recovery_http_status": recovery.status_code,
            "boundary": "The hold route is E2E-only and touches no DB; the Uvicorn transport and rejection path are real.",
        }

    async def ai_burst(self) -> dict[str, Any]:
        stage = "burst_ai"
        identities = self.database.seed_users("burst-ai", 9, self.jwt_secret)
        before = await wait_for_gauge_baseline(self.http.client)
        started = time.perf_counter()
        independent_work = asyncio.gather(
            *(
                self.http.analyze_stream(
                    identity,
                    f"independent AI burst meaning {index} {secrets.token_hex(2)}",
                    stage=stage,
                    idempotency_key=f"burst-independent-{index}-{uuid4()}",
                )
                for index, identity in enumerate(identities[:8], 1)
            )
        )
        independent, independent_samples = await sample_metrics_during(
            self.http.client, independent_work, interval=0.08
        )
        middle = await wait_for_gauge_baseline(self.http.client, timeout=60)
        independent_statuses = Counter(item["status"] for item in independent)
        independent_delta = counter_delta(before, middle)
        if independent_statuses[200] < 1 or independent_statuses[503] < 1:
            raise E2EFailure(f"Independent AI burst did not exercise accept and reject paths: {dict(independent_statuses)}")
        if independent_samples["max_gauges"]["ai_active"] > 1 or independent_samples["max_gauges"]["ai_waiting"] > 2:
            raise E2EFailure(f"AI running/waiting capacity exceeded: {independent_samples}")
        if independent_delta["ai_queue_full_reject_total"] < 1:
            raise E2EFailure(f"AI Queue Full counter did not grow: {independent_delta}")
        rejection_details = {
            item.get("detail") for item in independent if item.get("status") == 503
        }
        expected_rejection_details = {
            "AI 当前繁忙，请稍后重试",
            "服务繁忙，请稍后再试",
        }
        if "AI 当前繁忙，请稍后重试" not in rejection_details:
            raise E2EFailure(
                f"AI Queue Full did not return the readable Chinese detail: {rejection_details}"
            )
        if not rejection_details.issubset(expected_rejection_details):
            raise E2EFailure(f"Unexpected AI rejection detail: {rejection_details}")

        follower_identity = identities[8]
        follower_key = f"real-semantic-follower-{uuid4()}"
        follower_text = f"genuine duplicate follower request {secrets.token_hex(3)}"
        owner = asyncio.create_task(
            self.http.analyze_stream(
                follower_identity,
                follower_text,
                stage=stage,
                idempotency_key=follower_key,
            )
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            snapshot = await fetch_metrics(self.http.client)
            if metric_value(snapshot, "ai_active") >= 1:
                break
            await asyncio.sleep(0.05)
        follower_before = await fetch_metrics(self.http.client)
        follower_work = asyncio.gather(
            owner,
            *(
                self.http.analyze_stream(
                    follower_identity,
                    follower_text,
                    stage=stage,
                    idempotency_key=follower_key,
                )
                for _ in range(5)
            ),
        )
        follower_results, follower_samples = await sample_metrics_during(
            self.http.client, follower_work, interval=0.05
        )
        final = await wait_for_gauge_baseline(self.http.client, timeout=60)
        follower_statuses = Counter(item["status"] for item in follower_results)
        follower_delta = counter_delta(follower_before, final)
        if follower_samples["max_gauges"]["ai_inflight_followers"] > 3:
            raise E2EFailure(f"Follower capacity exceeded 3: {follower_samples}")
        if follower_delta["ai_inflight_follower_reject_total"] < 1 or follower_statuses[503] < 1:
            raise E2EFailure(
                f"Genuine duplicate follower rejection was not observed: statuses={dict(follower_statuses)}, delta={follower_delta}"
            )
        duration = time.perf_counter() - started
        return {
            "status": "PASS",
            "independent": {
                "status_counts": dict(independent_statuses),
                "metrics": independent_samples,
                "counter_delta": independent_delta,
            },
            "genuine_followers": {
                "same_user": follower_identity.label,
                "same_idempotency_key": follower_key,
                "same_payload": follower_text,
                "status_counts": dict(follower_statuses),
                "metrics": follower_samples,
                "counter_delta": follower_delta,
            },
            "final_gauges": {name: metric_value(final, name) for name in GAUGE_NAMES},
            "requests": summarize_requests(self.http.events, stage, duration),
        }

    async def tts_burst(self) -> dict[str, Any]:
        stage = "burst_tts"
        identities = self.database.seed_users("burst-tts", 8, self.jwt_secret)
        before = await wait_for_gauge_baseline(self.http.client)
        started = time.perf_counter()
        work = asyncio.gather(
            *(
                self.http.request(
                    "GET",
                    "/api/pronunciation/audio",
                    stage=stage,
                    user=identity.label,
                    headers=identity.headers(),
                    params={"text": f"unique TTS burst {index} {secrets.token_hex(3)}", "voice": "female"},
                )
                for index, identity in enumerate(identities, 1)
            )
        )
        responses, samples = await sample_metrics_during(self.http.client, work, interval=0.04)
        duration = time.perf_counter() - started
        final = await wait_for_gauge_baseline(self.http.client)
        statuses = Counter(response.status_code for response in responses)
        delta = counter_delta(before, final)
        if statuses[200] < 1 or statuses[503] < 1:
            raise E2EFailure(f"TTS burst did not exercise accept and reject paths: {dict(statuses)}")
        if samples["max_gauges"]["tts_active"] > 1 or samples["max_gauges"]["tts_waiting"] > 2:
            raise E2EFailure(f"TTS capacity exceeded: {samples}")
        if delta["tts_queue_full_reject_total"] < 1:
            raise E2EFailure(f"TTS Queue Full counter did not grow: {delta}")
        if any(response.status_code == 200 and not response.content.startswith(b"RIFF") for response in responses):
            raise E2EFailure("A successful TTS burst response was not a WAV")
        return {
            "status": "PASS",
            "status_counts": dict(statuses),
            "metrics": samples,
            "counter_delta": delta,
            "final_gauges": {name: metric_value(final, name) for name in GAUGE_NAMES},
            "requests": summarize_requests(self.http.events, stage, duration),
        }

    async def ollama_fault(self) -> dict[str, Any]:
        stage = "fault_ollama"
        identity = self.database.seed_users("fault-ollama", 1, self.jwt_secret)[0]
        create = require_response(
            await self.http.request(
                "POST",
                "/api/cards",
                stage=stage,
                user=identity.label,
                headers=identity.headers(),
                json={"content": "ollama fault card", "card_type": "phrase"},
            ),
            200,
            "Ollama fault setup card",
        )
        self.failure_switch.write_text("E2E transport fault", encoding="utf-8")
        started = time.perf_counter()
        try:
            ai_task = self.http.analyze_stream(
                identity,
                f"ollama unavailable {secrets.token_hex(4)}",
                stage=stage,
                idempotency_key=f"fault-ollama-{uuid4()}",
            )
            card_task = self.http.request(
                "GET",
                f"/api/cards/{create['id']}",
                stage=stage,
                user=identity.label,
                headers=identity.headers(),
            )
            review_task = self.http.request(
                "GET",
                "/api/reviews/overview",
                stage=stage,
                user=identity.label,
                headers=identity.headers(),
            )
            tts_task = self.http.request(
                "GET",
                "/api/pronunciation/audio",
                stage=stage,
                user=identity.label,
                headers=identity.headers(),
                params={"text": f"tts survives ollama {secrets.token_hex(3)}", "voice": "male"},
            )
            ai, card, review, tts = await asyncio.gather(ai_task, card_task, review_task, tts_task)
        finally:
            self.failure_switch.unlink(missing_ok=True)
        failure_payload = ai.get("final") if isinstance(ai.get("final"), dict) else {}
        explicit_business_failure = (
            ai["status"] == 200
            and failure_payload.get("ok") is False
            and failure_payload.get("level") == "failed"
            and bool(failure_payload.get("errors"))
        )
        if ai["status"] < 400 and not explicit_business_failure:
            raise E2EFailure(
                f"AI did not expose an HTTP or explicit business failure while Ollama was unavailable: {ai}"
            )
        if card.status_code != 200 or review.status_code != 200:
            raise E2EFailure(
                f"Non-AI business failed during Ollama outage: card={card.status_code}, review={review.status_code}"
            )
        if tts.status_code != 200 or not tts.content.startswith(b"RIFF"):
            raise E2EFailure(f"TTS failed during Ollama outage: HTTP {tts.status_code}")
        recovery = await self.http.analyze_stream(
            identity,
            f"ollama recovered {secrets.token_hex(4)}",
            stage=stage,
            idempotency_key=f"fault-ollama-recovery-{uuid4()}",
        )
        if recovery["status"] != 200 or "done" not in recovery["event_types"]:
            raise E2EFailure(f"AI did not recover after Ollama transport restoration: {recovery}")
        final = await wait_for_gauge_baseline(self.http.client, timeout=40)
        duration = time.perf_counter() - started
        return {
            "status": "PASS",
            "outage_ai": {key: value for key, value in ai.items() if key != "final"},
            "outage_failure_semantics": {
                "http_status": ai["status"],
                "business_ok": failure_payload.get("ok"),
                "business_level": failure_payload.get("level"),
                "errors": failure_payload.get("errors"),
                "explicit_business_failure": explicit_business_failure,
            },
            "unaffected": {
                "card": card.status_code,
                "review": review.status_code,
                "tts": tts.status_code,
            },
            "recovery_ai": {key: value for key, value in recovery.items() if key != "final"},
            "final_gauges": {name: metric_value(final, name) for name in GAUGE_NAMES},
            "requests": summarize_requests(self.http.events, stage, duration),
            "fault_scope": "Only the E2E transparent proxy path was disabled; shared Ollama was not stopped.",
        }

    async def piper_fault(self) -> dict[str, Any]:
        stage = "fault_piper"
        identity = self.database.seed_users("fault-piper", 1, self.jwt_secret)[0]
        control_headers = {"X-E2E-Control-Token": self.control_token}
        offline: list[tuple[Path, Path]] = []
        for path in self.piper_models.iterdir():
            if path.is_file() and (path.name.endswith(".onnx") or path.name.endswith(".onnx.json")):
                target = path.with_name(path.name + ".offline")
                path.replace(target)
                offline.append((target, path))
        if len(offline) != 4:
            raise E2EFailure(f"Expected four isolated Piper assets to move offline, got {len(offline)}")
        started = time.perf_counter()
        try:
            cleared = await self.http.request(
                "POST",
                "/__e2e/piper-cache/clear",
                stage=stage,
                user="piper-fault-control",
                headers=control_headers,
            )
            if cleared.status_code != 200:
                raise E2EFailure(f"Could not clear isolated Piper voice cache: HTTP {cleared.status_code}")
            tts_task = self.http.request(
                "GET",
                "/api/pronunciation/audio",
                stage=stage,
                user=identity.label,
                headers=identity.headers(),
                params={"text": f"piper unavailable {secrets.token_hex(3)}", "voice": "female"},
            )
            me_task = self.http.request(
                "GET", "/api/auth/me", stage=stage, user=identity.label, headers=identity.headers()
            )
            review_task = self.http.request(
                "GET",
                "/api/reviews/overview",
                stage=stage,
                user=identity.label,
                headers=identity.headers(),
            )
            ai_task = self.http.analyze_stream(
                identity,
                f"AI survives Piper outage {secrets.token_hex(3)}",
                stage=stage,
                idempotency_key=f"fault-piper-ai-{uuid4()}",
            )
            tts, me, review, ai = await asyncio.gather(tts_task, me_task, review_task, ai_task)
        finally:
            for source, target in offline:
                if source.exists():
                    source.replace(target)
        if tts.status_code != 503:
            raise E2EFailure(f"TTS did not fail clearly with Piper unavailable: HTTP {tts.status_code}")
        if me.status_code != 200 or review.status_code != 200 or ai["status"] != 200:
            raise E2EFailure(
                f"Other business failed during Piper outage: me={me.status_code}, review={review.status_code}, ai={ai['status']}"
            )
        await self.http.request(
            "POST",
            "/__e2e/piper-cache/clear",
            stage=stage,
            user="piper-recovery-control",
            headers=control_headers,
        )
        recovery = await self.http.request(
            "GET",
            "/api/pronunciation/audio",
            stage=stage,
            user=identity.label,
            headers=identity.headers(),
            params={"text": f"piper recovered {secrets.token_hex(3)}", "voice": "female"},
        )
        if recovery.status_code != 200 or not recovery.content.startswith(b"RIFF"):
            raise E2EFailure(f"Piper did not recover after isolated assets returned: HTTP {recovery.status_code}")
        final = await wait_for_gauge_baseline(self.http.client, timeout=40)
        duration = time.perf_counter() - started
        return {
            "status": "PASS",
            "outage_tts_status": tts.status_code,
            "unaffected": {"auth": me.status_code, "review": review.status_code, "ai": ai["status"]},
            "recovery_tts_status": recovery.status_code,
            "recovery_wav_bytes": len(recovery.content),
            "final_gauges": {name: metric_value(final, name) for name in GAUGE_NAMES},
            "requests": summarize_requests(self.http.events, stage, duration),
            "fault_scope": "Only hard links inside this run's Piper model directory were moved; formal model files were unchanged.",
        }

    async def db_exhaustion_fault(self) -> dict[str, Any]:
        stage = "fault_db_pool"
        identity = self.database.seed_users("fault-db", 1, self.jwt_secret)[0]
        before = await wait_for_gauge_baseline(self.http.client)
        control_headers = {"X-E2E-Control-Token": self.control_token}
        started = time.perf_counter()
        holders = [
            asyncio.create_task(
                self.http.request(
                    "GET",
                    "/__e2e/db-hold",
                    stage=stage,
                    user=f"db-holder-{index:02d}",
                    headers=control_headers,
                    params={"seconds": 7.0},
                )
            )
            for index in range(1, 16)
        ]
        deadline = time.monotonic() + 5
        peak_checked_out = 0.0
        while time.monotonic() < deadline:
            metrics = await fetch_metrics(self.http.client)
            peak_checked_out = max(peak_checked_out, metric_value(metrics, "db_pool_checked_out"))
            if peak_checked_out >= 15:
                break
            await asyncio.sleep(0.05)
        if peak_checked_out != 15:
            await asyncio.gather(*holders, return_exceptions=True)
            raise E2EFailure(f"Could not deterministically occupy all 15 SQLAlchemy connections: {peak_checked_out}")
        timeout_started = time.perf_counter()
        rejected = await self.http.request(
            "GET", "/api/auth/me", stage=stage, user=identity.label, headers=identity.headers()
        )
        timeout_seconds = time.perf_counter() - timeout_started
        holder_results = await asyncio.gather(*holders, return_exceptions=True)
        if rejected.status_code != 503:
            raise E2EFailure(f"Pool exhaustion did not return HTTP 503: {rejected.status_code}")
        try:
            rejected_json = rejected.json()
        except Exception:
            rejected_json = {}
        if rejected_json.get("code") != "DB_POOL_TIMEOUT":
            raise E2EFailure(f"Pool exhaustion returned wrong error semantics: {rejected.text[:500]}")
        if not 2.5 <= timeout_seconds <= 4.5:
            raise E2EFailure(f"DB pool rejection was not near the configured 3 seconds: {timeout_seconds:.3f}s")
        if any(not isinstance(item, httpx.Response) or item.status_code != 200 for item in holder_results):
            raise E2EFailure("One or more controlled DB holders did not release normally")
        final = await wait_for_gauge_baseline(self.http.client, timeout=20)
        delta = counter_delta(before, final)
        if delta["db_pool_timeout_total"] < 1:
            raise E2EFailure(f"db_pool_timeout_total did not grow: {delta}")
        recovery = await self.http.request(
            "GET", "/api/auth/me", stage=stage, user=f"{identity.label}-recovery", headers=identity.headers()
        )
        if recovery.status_code != 200:
            raise E2EFailure(f"DB-backed endpoint did not recover: HTTP {recovery.status_code}")
        snapshot = self.database.snapshot()
        if any(item["state"] == "idle in transaction" and item["count"] for item in snapshot["postgresql_sessions"]):
            raise E2EFailure(f"Idle-in-transaction leak after DB fault: {snapshot['postgresql_sessions']}")
        duration = time.perf_counter() - started
        return {
            "status": "PASS",
            "pool_capacity": 15,
            "peak_checked_out": peak_checked_out,
            "rejection_status": rejected.status_code,
            "rejection_code": rejected_json.get("code"),
            "rejection_seconds": round(timeout_seconds, 3),
            "counter_delta": delta,
            "recovery_status": recovery.status_code,
            "final_gauges": {name: metric_value(final, name) for name in GAUGE_NAMES},
            "database_snapshot": snapshot,
            "requests": summarize_requests(self.http.events, stage, duration),
            "boundary": "15 real pool checkouts were held only inside the isolated E2E app/database.",
        }

    async def cleanup(self) -> None:
        cleanup: dict[str, Any] = {"started_at": iso_now()}
        try:
            self.failure_switch.unlink(missing_ok=True)
        except Exception as exc:
            cleanup["failure_switch_error"] = f"{type(exc).__name__}: {exc}"
        if self.recorder is not None:
            try:
                await self.recorder.close()
                cleanup["http_client_closed"] = True
            except Exception as exc:
                cleanup["http_client_close_error"] = f"{type(exc).__name__}: {exc}"
        if self.server is not None:
            try:
                cleanup["server"] = self.server.stop()
            except Exception as exc:
                cleanup["server_error"] = f"{type(exc).__name__}: {exc}"
        if self.proxy is not None:
            try:
                cleanup["proxy"] = self.proxy.stop()
            except Exception as exc:
                cleanup["proxy_error"] = f"{type(exc).__name__}: {exc}"
        for key, port, marker in (
            ("server_listener_cleanup", APP_PORT, "e2e.lab_app:app"),
            ("proxy_listener_cleanup", PROXY_PORT, "e2e.ollama_proxy:app"),
        ):
            try:
                cleanup[key] = stop_verified_listener(port, marker)
            except Exception as exc:
                cleanup[f"{key}_error"] = f"{type(exc).__name__}: {exc}"
        ports_after_stop: dict[str, Any] = {}
        for port in (APP_PORT, PROXY_PORT, 8000):
            try:
                ports_after_stop[str(port)] = port_listener_pid(port)
            except Exception as exc:
                ports_after_stop[str(port)] = f"unknown: {type(exc).__name__}: {exc}"
        cleanup["ports_after_stop"] = ports_after_stop
        try:
            cleanup["database"] = self.database.drop()
        except Exception as exc:
            cleanup["database"] = {"dropped": False, "error": f"{type(exc).__name__}: {exc}"}
        for target in (self.tts_cache, self.piper_models):
            try:
                resolved = target.resolve()
                if resolved.parent != self.run_dir.resolve():
                    raise E2EFailure(f"Refusing to clean path outside run directory: {resolved}")
                if target.exists():
                    shutil.rmtree(target)
                cleanup.setdefault("runtime_directories_removed", []).append(str(target))
            except Exception as exc:
                cleanup.setdefault("runtime_cleanup_errors", []).append(
                    f"{target}: {type(exc).__name__}: {exc}"
                )
        cleanup["finished_at"] = iso_now()
        self.result["cleanup"] = cleanup

    def save_evidence(self) -> None:
        self.result["finished_at"] = iso_now()
        if self.recorder is not None:
            write_json(self.run_dir / "requests.json", [asdict(event) for event in self.recorder.events])
        write_json(self.run_dir / "result.json", self.result)
        (self.run_dir / "REPORT.md").write_text(
            render_report(sanitize_value(self.result)), encoding="utf-8"
        )


def _request_line(stage: dict[str, Any]) -> str:
    request = stage.get("requests") or {}
    latency = request.get("latency_ms") or {}
    return (
        f"total={request.get('total_requests', '-')}, success={request.get('success_count', '-')}, "
        f"failure={request.get('failure_count', '-')}, statuses={request.get('status_counts', {})}, "
        f"p50/p95/p99={latency.get('p50')}/{latency.get('p95')}/{latency.get('p99')} ms, "
        f"RPS={request.get('rps', '-')}"
    )


def render_report(result: dict[str, Any]) -> str:
    environment = result.get("environment") or {}
    dependencies = result.get("dependencies") or {}
    stages = result.get("stages") or {}
    lines = [
        "# Level 7 E2E 实验报告",
        "",
        f"- Run: `{result.get('run_id', '-')}`",
        f"- Overall: **{result.get('overall_status', 'INCOMPLETE')}**",
        f"- Started: `{result.get('started_at', '-')}`",
        f"- Finished: `{result.get('finished_at', '-')}`",
        "",
        "## A. 实际建立了什么",
        "",
        f"- Uvicorn/FastAPI: `{environment.get('app_base', APP_BASE)}`，PID `{environment.get('app_pid', '-')}`，`--limit-concurrency 30`",
        f"- PostgreSQL: `{(environment.get('database') or {}).get('database', DB_NAME)}`，revision `{(environment.get('database') or {}).get('revision', '-')}`",
        f"- Ollama transport: `{environment.get('proxy_base', PROXY_BASE)}` -> real `127.0.0.1:11434`",
        f"- Piper: isolated model links + isolated cache `{environment.get('tts_cache', '-')}`",
        "- 测试工具: Python/httpx 并发真实 HTTP runner、Prometheus 采样、Windows 进程/GPU 采样、PostgreSQL 最终状态查询",
        "- 微信客户端自动化: NOT COVERED；开发者工具存在，但 CLI Service Port 关闭，本轮未隐式修改该安全设置。",
        "",
        "## B. 哪些依赖是真的",
        "",
        "| Dependency | Result |",
        "|---|---|",
    ]
    for name in ("postgresql", "http", "auth", "qwen", "piper", "wechat_client"):
        lines.append(f"| {name} | {dependencies.get(name, 'UNKNOWN')} |")
    lines.extend(
        [
            "",
            "Auth=PARTIAL：测试用户和 JWT 在隔离库外置引导；Bearer 校验、用户读取、token_version 撤销和 logout 均走真实 HTTP/业务代码/PostgreSQL。没有 Mock 微信，也不声称覆盖真实 `wx.login -> code2session`。",
            "",
            "## C. 单用户 E2E",
            "",
        ]
    )
    single = stages.get("single_user") or {}
    lines.append(f"- Result: **{single.get('status', 'NOT RUN')}**")
    if single:
        lines.append(f"- {_request_line(single)}")
        lines.append(f"- Stream: `{single.get('stream', {})}`")
        lines.append(f"- Piper: `{single.get('piper_proof', {})}`")
        lines.append(f"- Review final: `{single.get('review_summary', {})}`")
    lines.extend(["", "## D. 多用户隔离", ""])
    isolation = stages.get("multi_user_isolation") or {}
    lines.append(f"- Result: **{isolation.get('status', 'NOT RUN')}**")
    if isolation:
        lines.append(f"- 攻击结果: `{isolation.get('attacks', [])}`")
        lines.append(f"- Victim unchanged: `{isolation.get('victim_unchanged', False)}`")
        lines.append(f"- {_request_line(isolation)}")
    lines.extend(["", "## E. 负载实验", ""])
    for users in (5, 10, 30, 100):
        stage = stages.get(f"load_{users}") or {}
        lines.append(f"- **{users} users**: {stage.get('status', 'NOT RUN')}; {_request_line(stage) if stage else 'no data'}")
        if stage:
            lines.append(f"  - resources: `{stage.get('resources', {})}`")
            lines.append(f"  - metric maxima/delta: `{stage.get('metrics', {})}`")
            lines.append(f"  - recovery: HTTP `{stage.get('recovery_http_status')}`; serious=`{stage.get('serious_conditions', [])}`")
    lines.extend(["", "## F. Burst", ""])
    for key, label in (("burst_http", "HTTP"), ("burst_ai", "AI"), ("burst_tts", "TTS")):
        stage = stages.get(key) or {}
        lines.append(f"- **{label}**: {stage.get('status', 'NOT RUN')}; {_request_line(stage) if stage else 'no data'}")
        if stage:
            lines.append(f"  - evidence: `{stage}`")
    lines.extend(["", "## G. 故障实验", ""])
    for key, label in (("fault_ollama", "Ollama"), ("fault_piper", "Piper"), ("fault_db_pool", "DB")):
        stage = stages.get(key) or {}
        lines.append(f"- **{label}**: {stage.get('status', 'NOT RUN')}; {_request_line(stage) if stage else 'no data'}")
        if stage:
            lines.append(f"  - recovery/fault evidence: `{stage}`")
    lines.extend(["", "## H. 所有异常", ""])
    exceptions = result.get("exceptions") or []
    if exceptions:
        for item in exceptions:
            lines.append(f"- `{item.get('stage')}`: {item.get('type')}: {item.get('message')}")
    else:
        lines.append("- Runner 未记录未处理异常；受控 409/429/503 仍按各阶段语义单独统计。")
    lines.extend(["", "## I. 数据库最终验证", ""])
    final_db = result.get("final_database_snapshot") or {}
    lines.append(f"- Counts: `{final_db.get('counts', {})}`")
    lines.append(f"- Violations: `{final_db.get('violations', {})}`")
    lines.append(f"- Resource usage: `{final_db.get('resource_usage', [])}`")
    lines.append(f"- PostgreSQL sessions before shutdown: `{final_db.get('postgresql_sessions', [])}`")
    cleanup = result.get("cleanup") or {}
    lines.append(f"- Cleanup: `{cleanup}`")
    lines.extend(["", "## J. 测试体系可信度结论", ""])
    if result.get("overall_status") == "PASS":
        lines.append(
            "本轮通过的范围可证明：隔离 Uvicorn + PostgreSQL + 当前业务代码 + Qwen + Piper 的真实 HTTP 链路、用户隔离、容量拒绝与故障恢复符合本次观测。"
        )
    else:
        lines.append(
            "本轮没有获得全链路绿色结论；已完成阶段及失败证据见上文。失败后的更高阶段按门禁停止，不能外推为通过。"
        )
    lines.append(
        "仍未证明：真实微信设备/开发者工具中的 `wx.login`、微信网络域名/代理/分包流式行为，以及未在本轮成功执行的任何后续阶段。"
    )
    return "\n".join(lines) + "\n"


STAGE_ORDER = [
    "single_user",
    "multi_user_isolation",
    "load_5",
    "load_10",
    "load_30",
    "load_100",
    "burst_http",
    "burst_ai",
    "burst_tts",
    "fault_ollama",
    "fault_piper",
    "fault_db_pool",
]


async def execute(through: str) -> tuple[int, Path]:
    runner = Level7Runner()
    required_index = len(STAGE_ORDER) - 1 if through == "all" else STAGE_ORDER.index(through)
    completed: list[str] = []
    gate_failed = False
    try:
        await runner.setup()
        runner.result["initial_database_snapshot"] = runner.database.snapshot()
        stage_calls: list[tuple[str, Callable[[], Awaitable[dict[str, Any]]]]] = [
            ("single_user", runner.single_user),
            ("multi_user_isolation", runner.multi_user_isolation),
            ("load_5", lambda: runner.load_level(5)),
            ("load_10", lambda: runner.load_level(10)),
            ("load_30", lambda: runner.load_level(30)),
            ("load_100", lambda: runner.load_level(100)),
            ("burst_http", runner.http_burst),
            ("burst_ai", runner.ai_burst),
            ("burst_tts", runner.tts_burst),
            ("fault_ollama", runner.ollama_fault),
            ("fault_piper", runner.piper_fault),
            ("fault_db_pool", runner.db_exhaustion_fault),
        ]
        for index, (name, call) in enumerate(stage_calls):
            if index > required_index:
                break
            print(f"[{iso_now()}] START {name}", flush=True)
            try:
                stage_result = await call()
                runner.result["stages"][name] = stage_result
                write_json(runner.run_dir / f"{name}.json", stage_result)
                print(f"[{iso_now()}] {stage_result.get('status', 'UNKNOWN')} {name}", flush=True)
                if stage_result.get("status") != "PASS":
                    gate_failed = True
                    break
                completed.append(name)
            except BaseException as exc:
                gate_failed = True
                evidence = {
                    "stage": name,
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                }
                runner.result["exceptions"].append(evidence)
                runner.result["stages"][name] = {"status": "FAIL", "exception": evidence}
                write_json(runner.run_dir / f"{name}.json", runner.result["stages"][name])
                print(
                    f"[{iso_now()}] FAIL {name}: {type(exc).__name__}: {redact_text(str(exc))}",
                    flush=True,
                )
                break
    except BaseException as exc:
        gate_failed = True
        runner.result["exceptions"].append(
            {
                "stage": "setup_or_orchestration",
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        print(
            f"[{iso_now()}] FAIL setup_or_orchestration: {type(exc).__name__}: {redact_text(str(exc))}",
            flush=True,
        )
    finally:
        if runner.database.created:
            try:
                runner.result["final_database_snapshot"] = runner.database.snapshot()
            except Exception as exc:
                runner.result["exceptions"].append(
                    {
                        "stage": "final_database_snapshot",
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    }
                )
                gate_failed = True
        if runner.recorder is not None:
            write_json(runner.run_dir / "requests-before-cleanup.json", [asdict(event) for event in runner.recorder.events])
        await runner.cleanup()
        requested = STAGE_ORDER[: required_index + 1]
        runner.result["completed_stages"] = completed
        runner.result["requested_stages"] = requested
        database_cleanup = (runner.result.get("cleanup") or {}).get("database", {})
        database_cleanup_ok = (
            database_cleanup.get("dropped") is True
            or database_cleanup.get("attempted") is False
        )
        cleanup_ok = (
            database_cleanup_ok
            and (runner.result.get("cleanup") or {}).get("ports_after_stop", {}).get(str(APP_PORT)) is None
            and (runner.result.get("cleanup") or {}).get("ports_after_stop", {}).get(str(PROXY_PORT)) is None
        )
        runner.result["overall_status"] = (
            "PASS" if not gate_failed and completed == requested and cleanup_ok else "FAIL"
        )
        if not cleanup_ok:
            runner.result["exceptions"].append(
                {
                    "stage": "cleanup",
                    "type": "CleanupFailure",
                    "message": "Database drop or E2E port cleanup was not proven",
                }
            )
        runner.save_evidence()
        print(f"[{iso_now()}] ARTIFACT_DIR {runner.run_dir}", flush=True)
        print(f"[{iso_now()}] LOG_PATH {runner.run_dir / 'uvicorn.log'}", flush=True)
        print(f"[{iso_now()}] RESULT_PATH {runner.run_dir / 'result.json'}", flush=True)
        print(f"[{iso_now()}] REPORT_PATH {runner.run_dir / 'REPORT.md'}", flush=True)
        print(f"[{iso_now()}] OVERALL {runner.result['overall_status']}", flush=True)
    return (0 if runner.result["overall_status"] == "PASS" else 1), runner.run_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--through",
        choices=[*STAGE_ORDER, "all"],
        default="all",
        help="Run ordered gates through this stage (default: all).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    exit_code, _ = asyncio.run(execute(args.through))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
