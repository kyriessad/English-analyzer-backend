import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def run_config(extra: dict[str, str | None]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "test",
            "DATABASE_URL": "sqlite:///:memory:",
            "JWT_EXPIRE_DAYS": "3",
        }
    )
    for key, value in extra.items():
        if value is None:
            env[key] = ""
        else:
            env[key] = value
    return subprocess.run(
        [PYTHON, "-c", "import app.core.config"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_safe_defaults_are_explicit():
    result = subprocess.run(
        [PYTHON, "scripts/check_config.py"],
        cwd=ROOT,
        env={
            **os.environ,
            "APP_ENV": "test",
            "DATABASE_URL": "sqlite:///:memory:",
            "JWT_EXPIRE_DAYS": "3",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "DB pool: 5 + 10, timeout 3s" in result.stdout
    assert "HTTP concurrency: 30" in result.stdout
    assert "AI concurrency: 1" in result.stdout
    assert "TTS waiting capacity: 2" in result.stdout
    assert "JWT expiry: 3 days" in result.stdout


def test_environment_overrides_default():
    result = run_config({"AI_GLOBAL_CONCURRENCY": "2", "DB_POOL_SIZE": "7"})
    assert result.returncode == 0, result.stderr


def test_invalid_integer_fails_instead_of_using_default():
    result = run_config({"AI_GLOBAL_CONCURRENCY": "abc"})
    assert result.returncode != 0
    assert "AI_GLOBAL_CONCURRENCY must be an integer" in result.stderr


def test_invalid_range_fails():
    result = run_config({"AI_GLOBAL_CONCURRENCY": "0"})
    assert result.returncode != 0
    assert "AI_GLOBAL_CONCURRENCY must be >= 1" in result.stderr


def test_jwt_expiry_over_three_days_fails():
    result = run_config({"JWT_EXPIRE_DAYS": "30"})
    assert result.returncode != 0
    assert "JWT_EXPIRE_DAYS must be <= 3" in result.stderr


def test_required_production_credentials_fail_when_missing():
    result = run_config(
        {
            "APP_ENV": "production",
            "DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1/db",
            "JWT_SECRET_KEY": None,
            "WECHAT_APPID": None,
            "WECHAT_SECRET": None,
        }
    )
    assert result.returncode != 0
    assert "JWT_SECRET_KEY is required" in result.stderr
