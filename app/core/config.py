"""
config.py 是“配置入口”，负责把密钥等配置读进来。
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    tencent_secret_id: str = os.getenv("TENCENT_SECRET_ID", "").strip()
    tencent_secret_key: str = os.getenv("TENCENT_SECRET_KEY", "").strip()
    tencent_tmt_region: str = os.getenv("TENCENT_TMT_REGION", "ap-guangzhou").strip() or "ap-guangzhou"
    hunyuan_api_key: str = os.getenv("HUNYUAN_API_KEY", "").strip()
    hunyuan_base_url: str = os.getenv("HUNYUAN_BASE_URL", "https://api.hunyuan.cloud.tencent.com/v1").strip().rstrip("/") or "https://api.hunyuan.cloud.tencent.com/v1"
    hunyuan_model: str = os.getenv("HUNYUAN_MODEL", "hunyuan-role-latest").strip() or "hunyuan-role-latest"
    wechat_appid: str = os.getenv("WECHAT_APPID", "").strip()
    wechat_secret: str = os.getenv("WECHAT_SECRET", "").strip()
    jwt_secret_key: str = os.getenv("JWT_SECRET_KEY", "").strip()
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "HS256").strip() or "HS256"
    jwt_expire_days: int = int(os.getenv("JWT_EXPIRE_DAYS", "30").strip() or "30")


settings = Settings()
