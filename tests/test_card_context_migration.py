import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "d3e4f5a6b7c8_add_card_context_fields.py"
)
MIGRATION_SPEC = importlib.util.spec_from_file_location("card_context_migration", MIGRATION_PATH)
assert MIGRATION_SPEC is not None and MIGRATION_SPEC.loader is not None
migration = importlib.util.module_from_spec(MIGRATION_SPEC)
MIGRATION_SPEC.loader.exec_module(migration)


def test_card_context_migration_upgrades_and_downgrades_sqlite(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    Table("cards", metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)

        migration.upgrade()

        columns = {column["name"]: column for column in inspect(connection).get_columns("cards")}
        assert columns["source_context"]["nullable"] is True
        assert columns["source_url"]["type"].length == 1000
        assert columns["example_sentence"]["nullable"] is True
        assert columns["example_translation"]["nullable"] is True

        migration.downgrade()

        remaining_columns = {
            column["name"] for column in inspect(connection).get_columns("cards")
        }
        assert remaining_columns == {"id"}
