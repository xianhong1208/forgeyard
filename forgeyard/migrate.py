"""Run Alembic migrations from code (startup runs `upgrade head`)."""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

from forgeyard.db import get_engine
from forgeyard.security import crypto

log = logging.getLogger("forgeyard")


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    return cfg


def upgrade() -> None:
    cfg = alembic_config()
    with get_engine().begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    if crypto.scrub_after_migration:
        scrub()


def scrub() -> None:
    """Rewrite the tables that held plain-text secrets, so no old copy of them stays on disk: SQLite
    keeps old pages in its write-ahead log and free space, PostgreSQL keeps dead row versions.
    Runs once, right after migration 0008 encrypted them (outside a transaction, as VACUUM must)."""
    engine = get_engine()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        if engine.dialect.name == "sqlite":
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.exec_driver_sql("VACUUM")
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        elif engine.dialect.name == "postgresql":
            conn.exec_driver_sql("VACUUM FULL deployments, replicas, workloads")
    crypto.scrub_after_migration = False
    log.info("Rewrote the database so the secrets stored before 1.1 leave no plain-text copy behind.")
