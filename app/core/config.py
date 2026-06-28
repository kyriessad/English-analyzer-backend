"""
config.py 是“配置入口”，负责把密钥等配置读进来。
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")


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


@dataclass(frozen=True)
class Settings:
    translation_provider: str = _env_str("TRANSLATION_PROVIDER", "argos").lower() or "argos"
    example_generator_provider: str = _env_str("EXAMPLE_GENERATOR_PROVIDER", "ollama").lower() or "ollama"

    ollama_base_url: str = _env_str("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/") or "http://127.0.0.1:11434"
    ollama_model: str = _env_str("OLLAMA_MODEL", "qwen3:8b") or "qwen3:8b"
    ollama_timeout_seconds: int = _env_int("OLLAMA_TIMEOUT_SECONDS", 50)
    ollama_temperature: float = _env_float("OLLAMA_TEMPERATURE", 0.3)
    ollama_think: bool = _env_bool("OLLAMA_THINK", False)

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


settings = Settings()
