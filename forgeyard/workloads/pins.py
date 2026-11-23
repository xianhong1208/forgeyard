"""The recorded ids of engine copies' images (1.5.2, issue #95): the `image_pins` table, and the copy of it every
lookup reads (forgeyard/workloads/images.py entry_for, after the committed lock file).

"Try this version" records a row per image it pins (forgeyard/models/enginetry.py), shared by every copy that names
the same reference. The rows are held in the server's memory (one process: the agents' connections live there too),
loaded when the server starts and reloaded after every change made here; a row that no custom recipe names any more
is removed when a recipe is changed or deleted (`prune`).
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from forgeyard.db import CustomRecipe, ImagePin, utcnow
from forgeyard.workloads import images

log = logging.getLogger("forgeyard")


def refresh(db: DbSession) -> None:
    """Load every row into the lookup (the server's start, and after each change)."""
    rows = db.scalars(select(ImagePin)).all()
    images.set_pinned({r.ref: dict(r.entry) for r in rows if isinstance(r.entry, dict)})


def load_at_start() -> None:
    from forgeyard.db import new_session

    db = new_session()
    try:
        refresh(db)
    except SQLAlchemyError as e:   # the server still starts: copies' images are then pulled directly
        log.warning("Could not read the recorded ids of engine copies' images: %s", e)
    finally:
        db.close()


def reset() -> None:
    """Forget the loaded rows (tests: one server's memory)."""
    images.set_pinned({})


def get(db: DbSession, ref: str) -> Optional[ImagePin]:
    return db.get(ImagePin, ref)


def signatures(db: DbSession, refs: list[str]) -> dict[str, dict]:
    """{ref: what was verified about it} for the refs that have a row."""
    refs = [r for r in refs if r]
    if not refs:
        return {}
    rows = db.scalars(select(ImagePin).where(ImagePin.ref.in_(refs))).all()
    return {r.ref: {"signature": r.signature, "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None,
                    "recorded_by": r.recorded_by} for r in rows}


def record(db: DbSession, ref: str, entry: dict, signature: Optional[dict], by: str) -> None:
    """Add (or replace) the row of one image. The caller commits, then calls refresh()."""
    row = db.get(ImagePin, ref)
    if row is None:
        row = ImagePin(ref=ref)
        db.add(row)
    row.entry = entry
    row.signature = signature
    row.recorded_by = by
    row.recorded_at = utcnow()


def named_by_custom(db: DbSession) -> set[str]:
    """Every pinned image reference a custom recipe names (whether or not it still validates)."""
    from forgeyard.models.recipes import IMAGE_LINE_RE

    out: set[str] = set()
    for text in db.scalars(select(CustomRecipe.yaml)).all():
        for m in IMAGE_LINE_RE.finditer(text or ""):
            out.add(m.group(1))
    return out


def prune(db: DbSession) -> list[str]:
    """Remove the rows no custom recipe names any more; the refs removed. The caller commits, then refresh()."""
    keep = named_by_custom(db)
    gone = [r for r in db.scalars(select(ImagePin.ref)).all() if r not in keep]
    if gone:
        db.execute(delete(ImagePin).where(ImagePin.ref.in_(gone)))
    return gone
