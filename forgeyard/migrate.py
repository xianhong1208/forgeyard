"""Run Alembic migrations from code (startup runs `upgrade head`)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from forgeyard.db import get_engine


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    return cfg


def upgrade() -> None:
    cfg = alembic_config()
    with get_engine().begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
