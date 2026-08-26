"""Read-only configuration preflight and safe effective-value summary."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings


def main() -> None:
    print("CONFIG PREFLIGHT PASS")
    print(f"DB pool: {settings.db_pool_size} + {settings.db_max_overflow}, timeout {settings.db_pool_timeout}s")
    print(f"HTTP concurrency: {settings.http_limit_concurrency}")
    print(f"AI concurrency: {settings.ai_global_concurrency}")
    print(f"AI waiting capacity: {settings.ai_queue_waiting_capacity}")
    print(f"AI follower capacity: {settings.ai_inflight_follower_capacity}")
    print(f"TTS concurrency: {settings.tts_global_concurrency}")
    print(f"TTS waiting capacity: {settings.tts_queue_waiting_capacity}")
    print(f"JWT expiry: {settings.jwt_expire_days} days")


if __name__ == "__main__":
    main()
