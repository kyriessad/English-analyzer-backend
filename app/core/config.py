"""
config.py 是“配置入口”，负责把密钥等配置读进来。
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[2]
DATABASE_URL_WAS_IN_PROCESS_ENV = bool(os.environ.get("DATABASE_URL", "").strip())
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
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


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
    required_alembic_revision: str = _env_str(
        "REQUIRED_ALEMBIC_REVISION",
        "e4f5a6b7c8d9",
    )
    allow_sqlite_for_tests: bool = _env_bool("ALLOW_SQLITE_FOR_TESTS", False)
    allowed_hosts: tuple[str, ...] = _env_csv(
        "ALLOWED_HOSTS",
        "127.0.0.1,localhost,testserver",
    )
    max_request_body_bytes: int = _env_int("MAX_REQUEST_BODY_BYTES", 1_048_576)

    translation_provider: str = _env_str("TRANSLATION_PROVIDER", "argos").lower() or "argos"
    example_generator_provider: str = _env_str("EXAMPLE_GENERATOR_PROVIDER", "ollama").lower() or "ollama"

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
    jwt_expire_days: int = _env_int("JWT_EXPIRE_DAYS", 30)

    ai_daily_quota: int = _env_int("AI_DAILY_QUOTA", 30)
    tts_daily_quota: int = _env_int("TTS_DAILY_QUOTA", 100)
    lexical_daily_quota: int = _env_int("LEXICAL_DAILY_QUOTA", 500)
    ai_global_concurrency: int = _env_int("AI_GLOBAL_CONCURRENCY", 1)
    tts_global_concurrency: int = _env_int("TTS_GLOBAL_CONCURRENCY", 2)
    resource_queue_timeout_seconds: int = _env_int("RESOURCE_QUEUE_TIMEOUT_SECONDS", 3)
    ai_queue_timeout_seconds: int = _env_int("AI_QUEUE_TIMEOUT_SECONDS", 30)
    ai_total_timeout_seconds: int = _env_int("AI_TOTAL_TIMEOUT_SECONDS", 90)


settings = Settings()
