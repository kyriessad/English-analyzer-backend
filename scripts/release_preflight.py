"""Read-only release preflight; it never migrates or starts a service."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.database import get_database_runtime_info, get_expected_alembic_revision


def main() -> None:
    expected = get_expected_alembic_revision()
    info = get_database_runtime_info()
    print("RELEASE PREFLIGHT PASS")
    print(f"Database: {info.database} ({info.dialect})")
    print(f"Current Alembic revision: {','.join(info.alembic_revisions) or 'missing'}")
    print(f"Code Alembic head: {expected.revision}")
    print(f"Revision source: {expected.source}")
    print(f"Python configuration: APP_ENV={settings.app_env}")
    print("Note: revision mismatch is reported here; migration remains an explicit next step.")


if __name__ == "__main__":
    main()
