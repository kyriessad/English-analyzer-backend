"""Read-only, password-free runtime database and Alembic preflight."""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import (
    assert_expected_database,
    get_database_runtime_info,
    get_expected_alembic_revision,
)


def main() -> None:
    expected_revision = get_expected_alembic_revision()
    info = get_database_runtime_info()
    print(f"Database dialect: {info.dialect}")
    print(f"Database host: {info.host}")
    print(f"Database port: {info.port}")
    print(f"Database name: {info.database}")
    print(f"Database schema: {info.schema}")
    print(f"Database current user: {info.current_user}")
    print(f"Database URL source: {info.url_source}")
    print(
        "Actual Alembic revision: "
        + (",".join(info.alembic_revisions) or "missing")
    )
    print(f"Expected Alembic revision: {expected_revision.revision}")
    print(f"Expected revision source: {expected_revision.source}")
    assert_expected_database(info, expected_revision)
    print("Database target preflight: passed")


if __name__ == "__main__":
    main()
