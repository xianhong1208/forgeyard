"""Database engine and models.

SQLAlchemy 2 with synchronous sessions: FastAPI runs sync endpoints in its thread
pool, and M0's traffic (sign-in, user admin) is small. Schema changes go through
Alembic (forgeyard/migrations); `create_all` is only used by tests.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from forgeyard.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # None until the person sets one (admin-created accounts get a set-password link).
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="user")          # admin | operator | user
    status: Mapped[str] = mapped_column(String(16), default="active")      # active | pending | disabled
    # The first account. It cannot be demoted, disabled or deleted, so an admin can never
    # lock everyone out of administration.
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    sessions: Mapped[list["Session"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Session(Base):
    """A signed-in browser. The cookie carries a random token; only its SHA-256 is stored."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)           # sha256(token) hex
    # What the UI shows and uses to sign one session out; never the token or its hash.
    public_id: Mapped[str] = mapped_column(String(36), unique=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class PasswordLink(Base):
    """A one-time link to set a password (new account or admin reset). Only the hash is stored."""

    __tablename__ = "password_links"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)           # sha256(token) hex
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    action: Mapped[str] = mapped_column(String(48), index=True)
    actor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    actor_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    target: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (Index("ix_audit_actor_at", "actor_id", "at"),)


class JoinToken(Base):
    """A one-time token an admin hands to a new machine. Only its SHA-256 is stored."""

    __tablename__ = "join_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)           # sha256(token) hex
    # What the UI shows and uses to cancel a token; never the token or its hash.
    public_id: Mapped[str] = mapped_column(String(36), unique=True, default=lambda: str(uuid.uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    node_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # the machine it admitted


class Node(Base):
    """A machine, represented by its agent."""

    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    hostname: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # sha256 of the machine's credential. NULL once revoked: nothing can match it again.
    credential_hash: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="offline")    # online | offline | revoked
    labels: Mapped[dict] = mapped_column(JSON, default=dict)
    runtimes: Mapped[list] = mapped_column(JSON, default=list)
    platform: Mapped[dict] = mapped_column(JSON, default=dict)              # os, kernel, arch, python
    # Per collector: {"nvidia": {"ok": false, "reason": "..."}, ...}
    collectors: Mapped[dict] = mapped_column(JSON, default=dict)
    # The agent was started with a fixture instead of real hardware (development and tests only).
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    agent_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    protocol: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    connected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Which accelerators Forgeyard may assign, by key. NULL: all of them. A key that disappears
    # from the inventory (card swapped) is simply not allowed any more: it fails closed.
    gpu_allowlist: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # What the agent can run with ({"process": {...}, "container": {...}}), from its hello.
    runtime_info: Mapped[dict] = mapped_column(JSON, default=dict)
    # Protocol features the agent declared ("workloads", "models"). An M1 agent declares none.
    features: Mapped[list] = mapped_column(JSON, default=list)
    # The agent's model cache (M3): {"ok", "free_bytes", "cached": [cache keys]}.
    model_cache: Mapped[dict] = mapped_column(JSON, default=dict)

    accelerators: Mapped[list["Accelerator"]] = relationship(
        back_populates="node", cascade="all, delete-orphan", order_by="Accelerator.position"
    )


class Accelerator(Base):
    """One card (or the CPU, so CPU-only machines go through the same scheduling later)."""

    __tablename__ = "accelerators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    # Stable per machine: "<vendor>:<uuid or index>"; heartbeats refer to cards by it.
    key: Mapped[str] = mapped_column(String(160))
    position: Mapped[int] = mapped_column(Integer, default=0)
    vendor: Mapped[str] = mapped_column(String(16))                        # nvidia | amd | cpu
    index: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str] = mapped_column(String(160))
    # CUDA compute capability ("8.0") or the AMD gfx target ("gfx1151"); the CPU's machine arch.
    arch: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    uuid: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Bytes. For the CPU row: system RAM. For a unified-memory APU: the GPU-usable pool.
    vram_total: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    vram_free: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    vram_used: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    unified_memory: Mapped[bool] = mapped_column(Boolean, default=False)
    driver: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    utilization: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # percent
    temperature: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # degrees C
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    node: Mapped[Node] = relationship(back_populates="accelerators")

    __table_args__ = (Index("ix_accelerators_node_key", "node_id", "key", unique=True),)


class Workload(Base):
    """Something to run: a service or a task. Its spec never changes after creation."""

    __tablename__ = "workloads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(48), unique=True)
    kind: Mapped[str] = mapped_column(String(16))                         # service | task
    runtime: Mapped[str] = mapped_column(String(16))                      # container | process | model
    spec: Mapped[dict] = mapped_column(JSON)
    desired_state: Mapped[str] = mapped_column(String(16), default="running")  # running | stopped
    # Bumped on every (re)start: the agent replaces a replica whose generation differs.
    generation: Mapped[int] = mapped_column(Integer, default=1)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    replicas: Mapped[list["Replica"]] = relationship(
        back_populates="workload", cascade="all, delete-orphan", order_by="Replica.ordinal"
    )


class Replica(Base):
    """One copy of a workload, placed on one machine, with the state its agent reports."""

    __tablename__ = "replicas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # What the agent knows it by, and the label on its container. Random, so a reinstalled server
    # never collides with what an agent still runs from before.
    uid: Mapped[str] = mapped_column(String(32), unique=True, default=lambda: uuid.uuid4().hex)
    workload_id: Mapped[int] = mapped_column(ForeignKey("workloads.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    node_id: Mapped[Optional[int]] = mapped_column(ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True, index=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    # [{"key", "vendor", "index", "uuid", "pci_bus_id", "model"}] — assigned exclusively.
    gpus: Mapped[list] = mapped_column(JSON, default=list)
    # pending | starting | running | healthy | unhealthy | backoff | exited | failed | stopping | stopped
    state: Mapped[str] = mapped_column(String(16), default="pending")
    reason: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    restarts: Mapped[int] = mapped_column(Integer, default=0)
    # The agent will not act on it again (a task that ended, a service that may not restart):
    # it leaves the desired state and its resources are free.
    final: Mapped[bool] = mapped_column(Boolean, default=False)
    pid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    container: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # [{"name", "port", "host_port", "address"}]
    ports: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # The last lines of output the agent sent when the replica ended (shown once it is gone).
    log_tail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Model deployments (M3): the spec rendered for the machine and recipe variant it was placed
    # with (sent instead of the workload's), the model to fetch, a process engine to fetch, the
    # VRAM reserved on each of its GPUs (or RAM for a CPU replica), and download progress.
    spec: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    model: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    engine: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    vram: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    download: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # When it last became ready (healthy, or running without a health check). Until the agent's
    # free-memory numbers are newer than this, its reservation still counts as not yet in use.
    ready_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    workload: Mapped[Workload] = relationship(back_populates="replicas")
    node: Mapped[Optional[Node]] = relationship()


class WorkloadEvent(Base):
    """What happened to a workload: created, placed, started, exited, stopped… (its detail page)."""

    __tablename__ = "workload_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workload_id: Mapped[int] = mapped_column(ForeignKey("workloads.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    replica: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # ordinal
    kind: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(String(500))


class CatalogModel(Base):
    """A model in the catalog: where it comes from (pinned), what it is, and what the estimator
    needs (from inspecting it once, when it was added)."""

    __tablename__ = "models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    source: Mapped[str] = mapped_column(String(1024))                     # hf://org/name | https://… | local://…
    kind: Mapped[str] = mapped_column(String(8))                          # hf | url | local
    ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)       # what was asked: main, a tag
    revision: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)   # the commit it is pinned to
    file: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)      # the GGUF file
    format: Mapped[str] = mapped_column(String(16))                       # safetensors | gguf | ctranslate2
    task: Mapped[str] = mapped_column(String(16))                         # chat | embed | rerank | asr
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    # Extra engine arguments per recipe (e.g. Qwen3-Reranker's --hf-overrides for vllm-pooling).
    engine_args: Mapped[dict] = mapped_column(JSON, default=dict)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CustomRecipe(Base):
    """A recipe an admin added (the built-in ones ship as files with Forgeyard)."""

    __tablename__ = "recipes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True)
    yaml: Mapped[str] = mapped_column(Text)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Deployment(Base):
    """A model served with a recipe: expands into one service workload and one gateway route."""

    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(48), unique=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("models.id", ondelete="RESTRICT"), index=True)
    recipe: Mapped[str] = mapped_column(String(40))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    served_name: Mapped[str] = mapped_column(String(128))
    workload_id: Mapped[int] = mapped_column(ForeignKey("workloads.id", ondelete="CASCADE"), unique=True)
    # The estimate when it was created: {"method", "device", "per_gpu", "ram", "parts", …}.
    estimate: Mapped[dict] = mapped_column(JSON, default=dict)
    # A Hugging Face token for gated repositories. Only ever sent to the agent that downloads;
    # never returned by the API, logged or audited.
    hf_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    model: Mapped[CatalogModel] = relationship()
    workload: Mapped[Workload] = relationship()


class GatewayRoute(Base):
    """What the gateway (M4) needs: a model name, the protocol, and the service that serves it."""

    __tablename__ = "gateway_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    served_name: Mapped[str] = mapped_column(String(128), unique=True)
    api: Mapped[str] = mapped_column(String(32))            # openai-chat | openai-embeddings | rerank | openai-audio
    # The `model` value the engine expects (the served name, or the Hugging Face id for speaches).
    upstream_model: Mapped[str] = mapped_column(String(256))
    deployment_id: Mapped[int] = mapped_column(ForeignKey("deployments.id", ondelete="CASCADE"), unique=True)
    workload_id: Mapped[int] = mapped_column(ForeignKey("workloads.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ServerSetting(Base):
    """Settings admins change at run time (model source allowlists)."""

    __tablename__ = "server_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


_engine = None
_SessionLocal = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        url = settings.db_url
        kwargs = {}
        if url.startswith("sqlite"):
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(conn, _record):
                cur = conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.close()
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    """Forget the engine (tests switch databases between cases)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def get_db() -> Iterator:
    get_engine()
    db = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


def new_session():
    """A database session outside a request (WebSocket handlers, background tasks). Close it."""
    get_engine()
    return _SessionLocal()


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite returns naive datetimes even for timezone=True columns: treat them as UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)
