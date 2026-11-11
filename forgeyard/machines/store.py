"""Database side of the agent protocol: join, authenticate, apply hello / heartbeat / inventory."""

from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from forgeyard import audit
from forgeyard.db import Accelerator, JoinToken, Node, Replica, as_utc, utcnow
from forgeyard.machines.messages import HelloIn, InventoryIn, MetricIn
from forgeyard.security.sessions import token_hash

log = logging.getLogger("forgeyard.machines")

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
def carry_over_keys(db: DbSession, node: Node, inventory: InventoryIn, existing: dict[str, Accelerator]) -> dict:
    """A card the agent now reports under a new key keeps its row, its allowlist entry and its
    replicas' claims, when the agent names the old key in detail.aliases (an AMD card that an
    agent up to 1.1.0 keyed by a UUID shared by every card of its model, now keyed by its PCI
    address). Only a key that disappears from this inventory, of the same vendor, claimed by
    exactly one card is carried over; anything ambiguous is left alone, so the allowlist fails
    closed as for a swapped card. Returns {old key: new key}."""
    reported = {item.key for item in inventory.accelerators}
    claims: dict[str, list] = {}
    for item in inventory.accelerators:
        aliases = (item.detail or {}).get("aliases")
        if item.key in existing or not isinstance(aliases, list):
            continue
        for alias in aliases[:16]:
            row = existing.get(alias) if isinstance(alias, str) else None
            if row is not None and alias not in reported and row.vendor == item.vendor:
                claims.setdefault(alias, []).append(item.key)
    renamed: dict[str, str] = {}
    for old, new in claims.items():
        if len(set(new)) == 1 and new[0] not in renamed.values():
            renamed[old] = new[0]
    if not renamed:
        return renamed
    for old, new in renamed.items():
        row = existing.pop(old)
        row.key = new
        existing[new] = row
    if node.gpu_allowlist is not None:
        node.gpu_allowlist = list(dict.fromkeys(renamed.get(k, k) for k in node.gpu_allowlist))
    for r in db.scalars(select(Replica).where(Replica.node_id == node.id, Replica.final.is_(False))).all():
        if any(g.get("key") in renamed for g in r.gpus or []):
            r.gpus = [dict(g, key=renamed.get(g.get("key"), g.get("key"))) for g in r.gpus]
    audit.record(db, "machine.gpu_keys_changed", actor_name="agent", target=node.name, machine_id=node.id,
                 keys=renamed)
    log.info("%s reports %d card(s) under a new key; kept their allowlist entries: %s", node.name, len(renamed),
             ", ".join(f"{o} -> {n}" for o, n in renamed.items()))
    return renamed


def apply_inventory(db: DbSession, node: Node, inventory: InventoryIn) -> None:
    now = utcnow()
    existing = {a.key: a for a in node.accelerators}
    carry_over_keys(db, node, inventory, existing)
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
    node.features = list(hello.features)
    node.runtime_info = dict(hello.runtime_info)
    node.model_cache = dict(hello.model_cache or {})
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
