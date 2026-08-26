"""Database wiring and non-secret runtime identity checks."""
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings
from app.observability.metrics import bind_db_pool_metrics


DATABASE_URL = settings.database_url
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is required. The application no longer falls back to SQLite."
    )

DATABASE_DIALECT = make_url(DATABASE_URL).get_backend_name()
_sqlite_test_allowed = (
    DATABASE_DIALECT == "sqlite"
    and settings.app_env == "test"
    and settings.allow_sqlite_for_tests
)
if DATABASE_DIALECT != settings.expected_database_dialect and not _sqlite_test_allowed:
    raise RuntimeError(
        "Refusing to start with database dialect "
        f"'{DATABASE_DIALECT}'; expected '{settings.expected_database_dialect}'. "
        "SQLite is allowed only when APP_ENV=test and ALLOW_SQLITE_FOR_TESTS=true."
    )

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

if DATABASE_DIALECT == "postgresql":
    engine = create_engine(
        DATABASE_URL,
        connect_args=connect_args,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_pre_ping=True,
    )
else:
    engine = create_engine(
        DATABASE_URL,
        connect_args=connect_args,
        pool_pre_ping=True,
    )
bind_db_pool_metrics(engine.pool)


if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        if DATABASE_URL != "sqlite:///:memory:":
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


@dataclass(frozen=True)
class DatabaseRuntimeInfo:
    dialect: str
    host: str
    port: str
    database: str
    schema: str
    current_user: str
    search_path: str
    alembic_revisions: tuple[str, ...]
    url_source: str


@dataclass(frozen=True)
class ExpectedAlembicRevision:
    revision: str
    source: str


def get_expected_alembic_revision() -> ExpectedAlembicRevision:
    """Resolve the explicit restore override or the unique Alembic code head."""
    override = settings.required_alembic_revision.strip()
    if override:
        return ExpectedAlembicRevision(
            revision=override,
            source="environment_override",
        )

    backend_root = Path(__file__).resolve().parents[1]
    config = AlembicConfig(str(backend_root / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    heads = tuple(sorted(script.get_heads()))
    if len(heads) != 1:
        rendered_heads = ",".join(heads) or "none"
        raise RuntimeError(
            "Alembic code head safety check failed: expected exactly one head, "
            f"found {len(heads)} ({rendered_heads})"
        )
    return ExpectedAlembicRevision(revision=heads[0], source="alembic_head")


def get_database_runtime_info() -> DatabaseRuntimeInfo:
    """Return a password-free description of the database actually reached."""
    url = make_url(DATABASE_URL)
    if DATABASE_DIALECT == "sqlite":
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            revisions = (
                tuple(
                    row[0]
                    for row in connection.execute(
                        text("SELECT version_num FROM alembic_version ORDER BY version_num")
                    )
                )
                if "alembic_version" in tables
                else ()
            )
        return DatabaseRuntimeInfo(
            dialect=DATABASE_DIALECT,
            host="local-file",
            port="n/a",
            database=url.database or ":memory:",
            schema="main",
            current_user="n/a",
            search_path="main",
            alembic_revisions=revisions,
            url_source=settings.database_url_source,
        )

    with engine.connect() as connection:
        revisions = tuple(
            row[0]
            for row in connection.execute(
                text("SELECT version_num FROM public.alembic_version ORDER BY version_num")
            )
        )
        return DatabaseRuntimeInfo(
            dialect=DATABASE_DIALECT,
            host=url.host or "(driver-default)",
            port=str(url.port or 5432),
            database=connection.execute(text("SELECT current_database()")).scalar_one(),
            schema=connection.execute(text("SELECT current_schema()")).scalar_one(),
            current_user=connection.execute(text("SELECT current_user")).scalar_one(),
            search_path=connection.execute(text("SHOW search_path")).scalar_one(),
            alembic_revisions=revisions,
            url_source=settings.database_url_source,
        )


def assert_expected_database(
    info: DatabaseRuntimeInfo,
    expected_revision: ExpectedAlembicRevision | None = None,
) -> None:
    """Abort non-test startup when the reached database is not the approved target."""
    if _sqlite_test_allowed:
        return
    expected_revision = expected_revision or get_expected_alembic_revision()
    problems: list[str] = []
    if info.dialect != settings.expected_database_dialect:
        problems.append(
            f"dialect is '{info.dialect}', expected '{settings.expected_database_dialect}'"
        )
    if settings.expected_database_name and info.database != settings.expected_database_name:
        problems.append(
            f"database is '{info.database}', expected '{settings.expected_database_name}'"
        )
    if info.schema != settings.expected_database_schema:
        problems.append(
            f"schema is '{info.schema}', expected '{settings.expected_database_schema}'"
        )
    if info.alembic_revisions != (expected_revision.revision,):
        problems.append(
            "Alembic revision is "
            f"{list(info.alembic_revisions) or ['missing']}, "
            f"expected ['{expected_revision.revision}'] "
            f"from {expected_revision.source}"
        )
    if problems:
        raise RuntimeError("Database startup safety check failed: " + "; ".join(problems))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
