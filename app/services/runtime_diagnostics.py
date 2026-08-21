"""Bounded, password-free runtime diagnostics for public development."""
from __future__ import annotations

import json
import os
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.config import settings


CLIENT_LOG_LIMIT = 200
REQUEST_LOG_LIMIT = 500
PERSISTED_LOG_MAX_BYTES = 50 * 1024 * 1024
PERSISTED_LOG_RETENTION_DAYS = 7

_client_logs: deque[dict[str, Any]] = deque(maxlen=CLIENT_LOG_LIMIT)
_request_logs: deque[dict[str, Any]] = deque(maxlen=REQUEST_LOG_LIMIT)
_lock = threading.RLock()
_restored = False

_backend_root = Path(__file__).resolve().parents[2]
_configured_log_path = os.getenv("RUNTIME_DIAGNOSTIC_LOG_PATH", "").strip()
RUNTIME_DIAGNOSTIC_LOG_PATH = (
    Path(_configured_log_path).expanduser().resolve()
    if _configured_log_path
    else (_backend_root / "logs" / "runtime-diagnostics.jsonl").resolve()
)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _persistence_enabled() -> bool:
    return settings.app_env != "test"


def _restore_locked() -> None:
    global _restored
    if _restored:
        return
    _restored = True
    if not _persistence_enabled() or not RUNTIME_DIAGNOSTIC_LOG_PATH.is_file():
        return
    try:
        lines = RUNTIME_DIAGNOSTIC_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return
    for line in lines[-(CLIENT_LOG_LIMIT + REQUEST_LOG_LIMIT) :]:
        try:
            envelope = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        record = envelope.get("record")
        if not isinstance(record, dict):
            continue
        if envelope.get("kind") == "client":
            _client_logs.append(record)
        elif envelope.get("kind") == "request":
            _request_logs.append(record)


def _append_persisted_locked(kind: str, record: dict[str, Any]) -> None:
    if not _persistence_enabled():
        return
    try:
        RUNTIME_DIAGNOSTIC_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if (
            RUNTIME_DIAGNOSTIC_LOG_PATH.is_file()
            and RUNTIME_DIAGNOSTIC_LOG_PATH.stat().st_size >= PERSISTED_LOG_MAX_BYTES
        ):
            rotated = RUNTIME_DIAGNOSTIC_LOG_PATH.with_name(
                f"{RUNTIME_DIAGNOSTIC_LOG_PATH.name}."
                f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}"
            )
            RUNTIME_DIAGNOSTIC_LOG_PATH.replace(rotated)
        cutoff = datetime.now(timezone.utc) - timedelta(days=PERSISTED_LOG_RETENTION_DAYS)
        prefix = f"{RUNTIME_DIAGNOSTIC_LOG_PATH.name}."
        for candidate in RUNTIME_DIAGNOSTIC_LOG_PATH.parent.glob(f"{RUNTIME_DIAGNOSTIC_LOG_PATH.name}.*"):
            if candidate.name.startswith(prefix):
                try:
                    modified = datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc)
                    if modified < cutoff:
                        candidate.unlink()
                except OSError:
                    continue
        envelope = {"kind": kind, "record": record}
        with RUNTIME_DIAGNOSTIC_LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")
    except OSError:
        # Runtime observation must never become an application failure.
        return


def add_client_log(record: dict[str, Any]) -> None:
    with _lock:
        _restore_locked()
        safe_record = dict(record)
        _client_logs.append(safe_record)
        _append_persisted_locked("client", safe_record)


def add_request_log(record: dict[str, Any]) -> None:
    with _lock:
        _restore_locked()
        safe_record = dict(record)
        _request_logs.append(safe_record)
        _append_persisted_locked("request", safe_record)


def recent_client_logs(limit: int = CLIENT_LOG_LIMIT) -> list[dict[str, Any]]:
    with _lock:
        _restore_locked()
        return [dict(record) for record in list(_client_logs)[-max(0, limit) :]]


def recent_request_logs(limit: int = REQUEST_LOG_LIMIT) -> list[dict[str, Any]]:
    with _lock:
        _restore_locked()
        return [dict(record) for record in list(_request_logs)[-max(0, limit) :]]


def reset_runtime_diagnostics_for_tests() -> None:
    with _lock:
        _client_logs.clear()
        _request_logs.clear()
