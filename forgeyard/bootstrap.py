"""First run: create the owner from ADMIN_USERNAME / ADMIN_PASSWORD, or ask for /setup."""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from forgeyard import audit
from forgeyard.config import get_settings
from forgeyard.db import User, get_db
from forgeyard.security import passwords, secrets_store

log = logging.getLogger("forgeyard")


def bootstrap(base_url: str) -> None:
    db = next(get_db())
    try:
        if (db.scalar(select(func.count()).select_from(User)) or 0) > 0:
            secrets_store.discard_setup_token()  # an owner exists: the token is useless now
            return
        settings = get_settings()
        if settings.admin_username and settings.admin_password:
            username = settings.admin_username.strip().lower()
            problem = passwords.policy_error(settings.admin_password, username)
            if problem:
                raise RuntimeError(f"ADMIN_PASSWORD is not accepted: {problem}")
            owner = User(username=username, role="admin", status="active", is_owner=True,
                         password_hash=passwords.hash_password(settings.admin_password))
            db.add(owner)
            db.flush()
            audit.record(db, "setup.owner_from_env", actor=owner, target=username)
            db.commit()
            secrets_store.discard_setup_token()
            log.info("Created the owner account %r from ADMIN_USERNAME.", username)
            return
        token = secrets_store.setup_token()
        bar = "=" * 64
        log.warning(
            "\n%s\nForgeyard has no owner yet. Open %s/setup and enter this setup token:\n\n    %s\n\n"
            "(also stored in %s until setup is done)\n%s",
            bar, base_url, token, settings.secrets_path, bar,
        )
    finally:
        db.close()
