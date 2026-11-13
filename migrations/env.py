"""Alembic environment. The database URL comes from the application settings."""

from alembic import context

from salesagent import models  # noqa: F401  (registers the tables)
from salesagent.config import get_settings
from salesagent.db import Base, make_engine

target_metadata = Base.metadata


def run_migrations() -> None:
    url = context.config.get_main_option("sqlalchemy.url") or get_settings().database_url
    engine = make_engine(url)
    with engine.connect() as connection:
        # render_as_batch keeps ALTER TABLE working on SQLite.
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations()
