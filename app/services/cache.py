"""
缓存文件,同一个英文内容短时间内重复分析时，不要每次都调用 DeepL
"""
import hashlib
from typing import Any

from cachetools import TTLCache


THIRTY_DAYS_IN_SECONDS = 30 * 24 * 60 * 60

_cache: TTLCache[str, dict[str, Any]] = TTLCache(
    maxsize=5000,
    ttl=THIRTY_DAYS_IN_SECONDS,
)


def make_cache_key(text: str, target_lang: str) -> str:
    source = f"{str(text or '').lower().strip()}::{str(target_lang or '').lower().strip()}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def get_cache(key: str) -> dict[str, Any] | None:
    value = _cache.get(key)
    if value is None:
        return None
    return dict(value)


def set_cache(key: str, value: dict[str, Any]) -> None:
    _cache[key] = dict(value)


def delete_cache(key: str) -> None:
    _cache.pop(key, None)
