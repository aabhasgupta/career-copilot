from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session

from copilot.config import DB_PATH
from copilot.db.models import Base


def get_engine(db_path: Path | None = None) -> Engine:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{path}")


def _ensure_columns(engine: Engine) -> None:
    """Additive micro-migration: create_all never alters existing tables, and
    this project deliberately has no Alembic (single user, additive-only
    schema growth). Add any model column missing from an existing table."""
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.tables.values():
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in existing:
                    ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column.type.compile(engine.dialect)}"
                    conn.execute(text(ddl))


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    _ensure_columns(engine)


def get_session(engine: Engine) -> Session:
    return Session(engine)
