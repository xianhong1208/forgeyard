"""Alembic environment. The URL comes from Forgeyard's settings, never from alembic.ini."""

from alembic import context

from forgeyard.db import Base, get_engine

target_metadata = Base.metadata


def run_migrations_online() -> None:
    connectable = context.config.attributes.get("connection")
    if connectable is not None:
        _run(connectable)
        return
    with get_engine().connect() as connection:
        _run(connection)


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",  # SQLite cannot ALTER most things
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations_online()
