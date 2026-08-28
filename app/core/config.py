"""
config.py 是“配置入口”，负责把密钥等配置读进来。
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[2]
DATABASE_URL_WAS_IN_PROCESS_ENV = bool(os.environ.get("DATABASE_URL", "").strip())
REQUIRED_ALEMBIC_REVISION_PROCESS_OVERRIDE = os.environ.get(
    "REQUIRED_ALEMBIC_REVISION", ""
).strip()
load_dotenv(BASE_DIR / ".env")
DATABASE_URL_SOURCE = (
    "process environment"
    if DATABASE_URL_WAS_IN_PROCESS_ENV
    else (str(BASE_DIR / ".env") if os.environ.get("DATABASE_URL", "").strip() else "missing")
)


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value not in {"0", "1", "false", "true", "no", "yes", "off", "on"}:
        raise ValueError(f"{name} must be a boolean (true/false)")
    return value in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _env_csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in os.getenv(name, default).split(",")
        if item.strip()
    )


@dataclass(frozen=True)
class Settings:
    app_env: str = _env_str("APP_ENV", "development").lower() or "development"
    log_level: str = _env_str("LOG_LEVEL", "INFO") or "INFO"
    tracing_enabled: bool = _env_bool("TRACING_ENABLED", True)
    database_url: str = _env_str("DATABASE_URL")
    database_url_source: str = DATABASE_URL_SOURCE
    expected_database_dialect: str = (
        _env_str("EXPECTED_DATABASE_DIALECT", "postgresql").lower() or "postgresql"
    )
    expected_database_name: str = _env_str("EXPECTED_DATABASE_NAME", "english_analyzer")
    expected_database_schema: str = _env_str("EXPECTED_DATABASE_SCHEMA", "public") or "public"
    # Normal startup derives the expected revision from the Alembic chain.
    # Only an explicit process environment value acts as a restore/test override;
    # a legacy value left in .env cannot pin production to an old migration.
    required_alembic_revision: str = REQUIRED_ALEMBIC_REVISION_PROCESS_OVERRIDE
    allow_sqlite_for_tests: bool = _env_bool("ALLOW_SQLITE_FOR_TESTS", False)
    allowed_hosts: tuple[str, ...] = _env_csv(
        "ALLOWED_HOSTS",
        "127.0.0.1,localhost,testserver",
    )
    max_request_body_bytes: int = _env_int("MAX_REQUEST_BODY_BYTES", 1_048_576)
    http_limit_concurrency: int = _env_int("HTTP_LIMIT_CONCURRENCY", 30)
    harper_enabled: bool = _env_bool(
        "HARPER_ENABLED",
        _env_str("APP_ENV", "development").lower() == "development",
    )
    harper_base_url: str = (
        _env_str("HARPER_BASE_URL", "http://127.0.0.1:8082").rstrip("/")
        or "http://127.0.0.1:8082"
    )
    harper_timeout_seconds: float = _env_float("HARPER_TIMEOUT_SECONDS", 0.5)
    db_pool_size: int = _env_int("DB_POOL_SIZE", 5)
    db_max_overflow: int = _env_int("DB_MAX_OVERFLOW", 10)
    db_pool_timeout: int = _env_int("DB_POOL_TIMEOUT", 3)

    translation_provider: str = _env_str("TRANSLATION_PROVIDER", "argos").lower() or "argos"
    example_generator_provider: str = _env_str("EXAMPLE_GENERATOR_PROVIDER", "ollama").lower() or "ollama"
    ai_provider: str = _env_str("AI_PROVIDER", "ollama").lower() or "ollama"
    ai_base_url: str = _env_str("AI_BASE_URL", _env_str("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
    ai_api_key: str = _env_str("AI_API_KEY")
    ai_model: str = _env_str("AI_MODEL", _env_str("OLLAMA_MODEL", "qwen3:8b")) or "qwen3:8b"

    ollama_base_url: str = _env_str("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/") or "http://127.0.0.1:11434"
    ollama_model: str = _env_str("OLLAMA_MODEL", "qwen3:8b") or "qwen3:8b"
    ollama_timeout_seconds: int = _env_int("OLLAMA_TIMEOUT_SECONDS", 50)
    ollama_temperature: float = _env_float("OLLAMA_TEMPERATURE", 0.3)
    ollama_think: bool = _env_bool("OLLAMA_THINK", False)

    ecdict_db_path: str = _env_str("ECDICT_DB_PATH", str(BASE_DIR / "data" / "ecdict" / "ecdict.db"))

    piper_voice: str = _env_str("PIPER_VOICE", "en_US-lessac-medium") or "en_US-lessac-medium"
    piper_male_voice: str = _env_str("PIPER_MALE_VOICE", "en_US-hfc_male-medium") or "en_US-hfc_male-medium"
    piper_female_voice: str = _env_str("PIPER_FEMALE_VOICE", "en_US-lessac-medium") or "en_US-lessac-medium"
    piper_default_voice: str = _env_str("PIPER_DEFAULT_VOICE", "male") or "male"
    piper_data_dir: str = _env_str("PIPER_DATA_DIR", str(BASE_DIR / "data" / "piper"))
    piper_audio_cache_dir: str = _env_str("PIPER_AUDIO_CACHE_DIR", str(BASE_DIR / "data" / "audio-cache"))
    piper_max_text_chars: int = _env_int("PIPER_MAX_TEXT_CHARS", 300)
    piper_cache_max_bytes: int = _env_int("PIPER_CACHE_MAX_BYTES", 512 * 1024 * 1024)
    piper_cache_max_age_days: int = _env_int("PIPER_CACHE_MAX_AGE_DAYS", 30)

    enable_tencent_tmt: bool = _env_bool("ENABLE_TENCENT_TMT", False)
    enable_hunyuan: bool = _env_bool("ENABLE_HUNYUAN", False)

    tencent_secret_id: str = _env_str("TENCENT_SECRET_ID")
    tencent_secret_key: str = _env_str("TENCENT_SECRET_KEY")
    tencent_tmt_region: str = _env_str("TENCENT_TMT_REGION", "ap-guangzhou") or "ap-guangzhou"
    hunyuan_api_key: str = _env_str("HUNYUAN_API_KEY")
    hunyuan_base_url: str = _env_str("HUNYUAN_BASE_URL", "https://api.hunyuan.cloud.tencent.com/v1").rstrip("/") or "https://api.hunyuan.cloud.tencent.com/v1"
    hunyuan_model: str = _env_str("HUNYUAN_MODEL", "hunyuan-role-latest") or "hunyuan-role-latest"
    deepl_auth_key: str = _env_str("DEEPL_AUTH_KEY")

    wechat_appid: str = _env_str("WECHAT_APPID")
    wechat_secret: str = _env_str("WECHAT_SECRET")
    jwt_secret_key: str = _env_str("JWT_SECRET_KEY")
    jwt_algorithm: str = _env_str("JWT_ALGORITHM", "HS256") or "HS256"
    jwt_expire_days: int = _env_int("JWT_EXPIRE_DAYS", 3)

    ai_daily_quota: int = _env_int("AI_DAILY_QUOTA", 30)
    tts_daily_quota: int = _env_int("TTS_DAILY_QUOTA", 100)
    lexical_daily_quota: int = _env_int("LEXICAL_DAILY_QUOTA", 500)
    ai_global_concurrency: int = _env_int("AI_GLOBAL_CONCURRENCY", 1)
    ai_queue_waiting_capacity: int = _env_int("AI_QUEUE_WAITING_CAPACITY", 2)
    ai_inflight_follower_capacity: int = _env_int("AI_INFLIGHT_FOLLOWER_CAPACITY", 3)
    tts_global_concurrency: int = _env_int("TTS_GLOBAL_CONCURRENCY", 1)
    tts_queue_waiting_capacity: int = _env_int("TTS_QUEUE_WAITING_CAPACITY", 2)
    resource_queue_timeout_seconds: int = _env_int("RESOURCE_QUEUE_TIMEOUT_SECONDS", 3)
    ai_queue_timeout_seconds: int = _env_int("AI_QUEUE_TIMEOUT_SECONDS", 30)
    ai_total_timeout_seconds: int = _env_int("AI_TOTAL_TIMEOUT_SECONDS", 90)


settings = Settings()


def validate_settings(value: Settings = settings) -> None:
    """Fail early on unsafe or ambiguous runtime configuration."""
    errors: list[str] = []
    if not value.database_url:
        errors.append("DATABASE_URL is required")
    for name, current, minimum in (
        ("DB_POOL_SIZE", value.db_pool_size, 1),
        ("AI_GLOBAL_CONCURRENCY", value.ai_global_concurrency, 1),
        ("TTS_GLOBAL_CONCURRENCY", value.tts_global_concurrency, 1),
        ("HTTP_LIMIT_CONCURRENCY", value.http_limit_concurrency, 1),
        ("JWT_EXPIRE_DAYS", value.jwt_expire_days, 1),
    ):
        if current < minimum:
            errors.append(f"{name} must be >= {minimum}")
    for name, current in (
        ("DB_MAX_OVERFLOW", value.db_max_overflow),
        ("AI_QUEUE_WAITING_CAPACITY", value.ai_queue_waiting_capacity),
        ("AI_INFLIGHT_FOLLOWER_CAPACITY", value.ai_inflight_follower_capacity),
        ("TTS_QUEUE_WAITING_CAPACITY", value.tts_queue_waiting_capacity),
    ):
        if current < 0:
            errors.append(f"{name} must be >= 0")
    for name, current in (
        ("DB_POOL_TIMEOUT", value.db_pool_timeout),
        ("RESOURCE_QUEUE_TIMEOUT_SECONDS", value.resource_queue_timeout_seconds),
        ("AI_QUEUE_TIMEOUT_SECONDS", value.ai_queue_timeout_seconds),
        ("AI_TOTAL_TIMEOUT_SECONDS", value.ai_total_timeout_seconds),
    ):
        if current <= 0:
            errors.append(f"{name} must be > 0")
    if value.jwt_expire_days > 3:
        errors.append("JWT_EXPIRE_DAYS must be <= 3")
    if value.ai_provider not in {"ollama", "cloud"}:
        errors.append("AI_PROVIDER must be one of: ollama, cloud")
    if value.app_env.lower() != "test":
        for name, current in (
            ("JWT_SECRET_KEY", value.jwt_secret_key),
            ("WECHAT_APPID", value.wechat_appid),
            ("WECHAT_SECRET", value.wechat_secret),
        ):
            if not current:
                errors.append(f"{name} is required")
        if value.jwt_secret_key in {"change_me", "change_me_to_a_long_random_secret"}:
            errors.append("JWT_SECRET_KEY must not use the example placeholder")
    if errors:
        raise ValueError("Invalid application configuration: " + "; ".join(errors))


validate_settings()
