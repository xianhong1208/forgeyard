"""Database side of the agent protocol: join, authenticate, apply hello / heartbeat / inventory."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from forgeyard.db import Accelerator, JoinToken, Node, as_utc, utcnow
from forgeyard.machines.messages import HelloIn, InventoryIn, MetricIn
from forgeyard.security.sessions import token_hash

JOIN_TOKEN_TTL = timedelta(minutes=15)
JOIN_TOKEN_PREFIX = "fyj_"
CREDENTIAL_PREFIX = "fym_"
_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


# ------------------------------------------------------------------ join tokens
def issue_join_token(db: DbSession, created_by: str) -> tuple[str, JoinToken]:
    token = JOIN_TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = JoinToken(id=token_hash(token), created_by=created_by, expires_at=utcnow() + JOIN_TOKEN_TTL)
    db.add(row)
    db.flush()
    return token, row


def live_join_tokens(db: DbSession) -> list[JoinToken]:
    now = utcnow()
    rows = db.scalars(select(JoinToken).where(JoinToken.used_at.is_(None)).order_by(JoinToken.created_at.desc())).all()
    return [r for r in rows if now < as_utc(r.expires_at)]


def redeem_join_token(db: DbSession, token: Optional[str], now: Optional[datetime] = None) -> Optional[JoinToken]:
    """Use a join token up. None if it is unknown, used or expired. Single use even under races:
    the UPDATE only succeeds for the request that flips used_at from NULL."""
    if not token:
        return None
    now = now or utcnow()
    row = db.get(JoinToken, token_hash(token.strip()))
    if row is None or row.used_at is not None or now >= as_utc(row.expires_at):
        return None
    flipped = db.execute(
        update(JoinToken).where(JoinToken.id == row.id, JoinToken.used_at.is_(None)).values(used_at=now)
    ).rowcount
    if flipped != 1:
        return None
    db.refresh(row)
    return row


# ------------------------------------------------------------------ machines
def clean_name(raw: Optional[str]) -> str:
    name = _NAME_RE.sub("-", (raw or "").strip()).strip("-.")[:64]
    return name or "machine"


def unique_name(db: DbSession, raw: Optional[str]) -> str:
    base = clean_name(raw)
    taken = set(db.scalars(select(Node.name)).all())
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def create_node(db: DbSession, *, name: Optional[str], hostname: Optional[str], agent_version: Optional[str],
                protocol: int, ip: Optional[str]) -> tuple[str, Node]:
    credential = CREDENTIAL_PREFIX + secrets.token_urlsafe(32)
    node = Node(name=unique_name(db, name or hostname), hostname=(hostname or "")[:255] or None,
                credential_hash=token_hash(credential), status="offline", agent_version=(agent_version or "")[:32] or None,
                protocol=protocol, ip=ip, labels={}, runtimes=[], platform={}, collectors={})
    db.add(node)
    db.flush()
    return credential, node


def authenticate(db: DbSession, credential: Optional[str]) -> Optional[Node]:
    """The machine this credential belongs to, or None (unknown, revoked or removed)."""
    if not credential:
        return None
    node = db.scalar(select(Node).where(Node.credential_hash == token_hash(credential.strip())))
    if node is None or node.revoked_at is not None or node.status == "revoked":
        return None
    return node


def revoke(db: DbSession, node: Node, by: str) -> None:
    node.credential_hash = None
    node.revoked_at = utcnow()
    node.revoked_by = by
    node.status = "revoked"
    db.flush()


# ------------------------------------------------------------------ inventory
def apply_inventory(db: DbSession, node: Node, inventory: InventoryIn) -> None:
    now = utcnow()
    existing = {a.key: a for a in node.accelerators}
    seen: set[str] = set()
    for position, item in enumerate(inventory.accelerators):
        if item.key in seen:
            continue  # a duplicate key would break the unique index; keep the first
        seen.add(item.key)
        row = existing.get(item.key)
        if row is None:
            row = Accelerator(node_id=node.id, key=item.key)
            node.accelerators.append(row)
        row.position = position
        row.vendor = item.vendor
        row.index = item.index
        row.model = item.model or "Unknown"
        row.arch = item.arch
        row.uuid = item.uuid
        row.vram_total = item.vram_total
        row.vram_free = item.vram_free
        row.vram_used = item.vram_used
        row.unified_memory = item.unified_memory
        row.driver = item.driver
        row.utilization = item.utilization
        row.temperature = item.temperature
        row.detail = item.detail
        row.updated_at = now
    for key, row in existing.items():
        if key not in seen:
            node.accelerators.remove(row)
    node.collectors = {k: v.model_dump(exclude_none=True) for k, v in inventory.collectors.items()}
    db.flush()


def apply_hello(db: DbSession, node: Node, hello: HelloIn, ip: Optional[str]) -> None:
    now = utcnow()
    node.hostname = hello.hostname or node.hostname
    node.agent_version = hello.agent_version
    node.protocol = hello.v
    node.labels = dict(hello.labels)
    node.platform = dict(hello.platform)
    node.runtimes = list(hello.runtimes)
    node.simulated = hello.simulated
    node.ip = ip
    node.connected_at = now
    node.last_heartbeat_at = now
    node.status = "online"
    apply_inventory(db, node, hello.inventory)


def apply_heartbeat(db: DbSession, node: Node, metrics: list[MetricIn], ip: Optional[str]) -> None:
    now = utcnow()
    node.last_heartbeat_at = now
    node.status = "online"
    node.ip = ip
    by_key = {a.key: a for a in node.accelerators}
    for m in metrics:
        row = by_key.get(m.key)
        if row is None:
            continue
        for field in ("vram_free", "vram_used", "utilization", "temperature"):
            value = getattr(m, field)
            if value is not None:
                setattr(row, field, value)
        row.updated_at = now
    db.flush()
