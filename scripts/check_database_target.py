"""Read-only, password-free runtime database and Alembic preflight."""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import assert_expected_database, get_database_runtime_info


def main() -> None:
    info = get_database_runtime_info()
    print(f"Database dialect: {info.dialect}")
    print(f"Database host: {info.host}")
    print(f"Database port: {info.port}")
    print(f"Database name: {info.database}")
    print(f"Database schema: {info.schema}")
    print(f"Database current user: {info.current_user}")
    print(f"Database URL source: {info.url_source}")
    print(
        "Alembic revision: "
        + (",".join(info.alembic_revisions) or "missing")
    )
    assert_expected_database(info)
    print("Database target preflight: passed")


if __name__ == "__main__":
    main()
