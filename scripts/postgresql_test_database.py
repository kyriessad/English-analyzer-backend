"""Create or remove strictly named, isolated PostgreSQL test databases."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url


SAFE_NAME = re.compile(r"^english_analyzer_phase1_(?:pytest|e2e|migration_[a-z0-9_]+)$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("create", "drop"))
    parser.add_argument("--name", required=True)
    parser.add_argument("--recreate", action="store_true")
    parser.add_argument("--migrate-head", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not SAFE_NAME.fullmatch(args.name):
        raise RuntimeError(
            "Refusing unsafe test database name. Expected an "
            "english_analyzer_phase1_* test name."
        )
    raw_url = os.environ.get("DATABASE_URL", "").strip()
    if not raw_url:
        raise RuntimeError("DATABASE_URL is required")
    base_url = make_url(raw_url)
    if base_url.get_backend_name() != "postgresql":
        raise RuntimeError("PostgreSQL is required")
    if args.name == base_url.database:
        raise RuntimeError("Refusing to operate on the configured formal database")

    admin_url = base_url.set(
        database="postgres",
        drivername="postgresql",
    ).render_as_string(hide_password=False)
    with psycopg.connect(admin_url, autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s",
            (args.name,),
        ).fetchone()
        if args.action == "drop":
            if exists:
                connection.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname=%s AND pid <> pg_backend_pid()",
                    (args.name,),
                )
                connection.execute(
                    sql.SQL("DROP DATABASE {}").format(
                        sql.Identifier(args.name)
                    )
                )
            print(f"isolated_test_database_dropped={args.name}")
            return

        if exists and not args.recreate:
            raise RuntimeError(
                f"Test database '{args.name}' already exists; pass --recreate"
            )
        if exists:
            connection.execute(
                "SELECT pg_terminate_backend(pid) "
                "FROM pg_stat_activity "
                "WHERE datname=%s AND pid <> pg_backend_pid()",
                (args.name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE {}").format(
                    sql.Identifier(args.name)
                )
            )
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(args.name))
        )
    print(f"isolated_test_database_created={args.name}")

    if args.migrate_head:
        project_root = Path(__file__).resolve().parents[1]
        test_url = base_url.set(database=args.name).render_as_string(
            hide_password=False
        )
        environment = os.environ.copy()
        environment["DATABASE_URL"] = test_url
        environment["EXPECTED_DATABASE_NAME"] = args.name
        environment["APP_ENV"] = "test"
        environment["ALLOW_SQLITE_FOR_TESTS"] = "false"
        subprocess.run(
            [
                str(project_root / ".venv" / "Scripts" / "python.exe"),
                "-m",
                "alembic",
                "upgrade",
                "head",
            ],
            cwd=project_root,
            env=environment,
            check=True,
        )
        print(f"isolated_test_database_migrated_to_head={args.name}")


if __name__ == "__main__":
    main()
