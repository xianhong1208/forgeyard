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
from forgeyard.security.crypto import EncryptedText, SpecJSON


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
    # Small per-person settings of the web UI ({"onboarding_hidden": true, …}): see
    # forgeyard/api/onboarding.py for the keys that are accepted.
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)
    # What this person's requests may cost at cloud providers per UTC calendar month, in micro-USD,
    # over all their keys and the playground (1.2; forgeyard/gateway/budgets.py). NULL: no budget.
    # Only admins set it.
    budget_micros: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

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
    # Environment values are stored encrypted (forgeyard/security/crypto.py); code sees plain values.
    spec: Mapped[dict] = mapped_column(SpecJSON("workloads.spec"))
    desired_state: Mapped[str] = mapped_column(String(16), default="running")  # running | stopped
    # Bumped on every (re)start: the agent replaces a replica whose generation differs.
    generation: Mapped[int] = mapped_column(Integer, default=1)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 1.3: exposed behind the gateway at /x/<name> (forgeyard/gateway/passthrough.py). Kept beside the
    # spec, never in it (agents validate the spec with extra="forbid"), and changeable while it runs.
    # NULL: not exposed.
    gateway: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

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
    # The replica's internal key (M4): generated when a model replica is placed, so a new placement
    # (a restart from the UI, a move to another machine) gets a new one. The agent hands it to the
    # engine through the environment and the gateway sends it upstream; it never leaves the server
    # otherwise (not in the API, the spec, events or logs). Stored encrypted.
    secret: Mapped[Optional[str]] = mapped_column(EncryptedText("replicas.secret"), nullable=True)

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
    format: Mapped[str] = mapped_column(String(16))                       # safetensors | gguf | ctranslate2 | onnx | diffusers
    task: Mapped[str] = mapped_column(String(16))                         # chat | embed | rerank | asr | tts | image
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
    # never returned by the API, logged or audited. Stored encrypted.
    hf_token: Mapped[Optional[str]] = mapped_column(EncryptedText("deployments.hf_token"), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    model: Mapped[CatalogModel] = relationship()
    workload: Mapped[Workload] = relationship()


class GatewayRoute(Base):
    """A model name the gateway serves (`model` in a /v1 request) and how: its protocol, its targets
    in tiers (RouteTarget), and when a request may move on to the next tier (1.2).

    A deployment creates its own route (`deployment_id`, deleted with it); an admin can add tiers to
    it (a cloud model to spill to) or create routes without one (a cloud-only model)."""

    __tablename__ = "gateway_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    served_name: Mapped[str] = mapped_column(String(128), unique=True)
    api: Mapped[str] = mapped_column(String(32))            # openai-chat | openai-embeddings | rerank | openai-audio | openai-speech | openai-images
    # The deployment this route was created with (NULL: a route an admin made, e.g. cloud only).
    deployment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("deployments.id", ondelete="CASCADE"),
                                                         unique=True, nullable=True)
    # Never leaves your network: cloud targets are not used, whatever a key allows.
    local_only: Mapped[bool] = mapped_column(Boolean, default=False)
    # Spill to the next tier when the prompt (estimated) does not fit a local deployment's context.
    spill_on_long_prompt: Mapped[bool] = mapped_column(Boolean, default=False)
    # How long a request waits for a free local slot before it spills because local is busy.
    wait_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    targets: Mapped[list["RouteTarget"]] = relationship(
        back_populates="route", cascade="all, delete-orphan", order_by="[RouteTarget.tier, RouteTarget.id]")


class RouteTarget(Base):
    """Who answers for a route: a local deployment (its ready replicas) or a provider's model.
    Tier 0 is tried first; a request moves to the next tier only before the first byte."""

    __tablename__ = "route_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    route_id: Mapped[int] = mapped_column(ForeignKey("gateway_routes.id", ondelete="CASCADE"), index=True)
    tier: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16))           # local | provider
    deployment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("deployments.id", ondelete="CASCADE"),
                                                         nullable=True, index=True)
    provider_model_id: Mapped[Optional[int]] = mapped_column(ForeignKey("provider_models.id", ondelete="CASCADE"),
                                                             nullable=True, index=True)
    # The `model` a local engine expects (the served name, or the Hugging Face id for speaches).
    # Provider targets use their ProviderModel's upstream name.
    upstream_model: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    route: Mapped[GatewayRoute] = relationship(back_populates="targets")
    deployment: Mapped[Optional[Deployment]] = relationship()
    provider_model: Mapped[Optional["ProviderModel"]] = relationship()


class Provider(Base):
    """A model API outside Forgeyard, spoken to through its OpenAI-compatible endpoint (1.2): OpenAI,
    Anthropic, Gemini, Azure OpenAI, OpenRouter, or any OpenAI-compatible URL (a vLLM elsewhere)."""

    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(24))           # forgeyard/gateway/quirks.py: KINDS
    base_url: Mapped[str] = mapped_column(String(512))
    # Write-only: never returned, logged, audited or copied; the API shows its last 4 characters.
    api_key: Mapped[Optional[str]] = mapped_column(EncryptedText("providers.api_key"), nullable=True)
    key_last4: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    # Extra request headers that are not secret (anthropic-workspace-id, OpenRouter's X-Title).
    headers: Mapped[dict] = mapped_column(JSON, default=dict)
    # Outside your network: counts as cloud for a key's `cloud` setting. False: a server of your
    # own (a vLLM on your VPN), used like a local target.
    external: Mapped[bool] = mapped_column(Boolean, default=True)
    # Plain http and private addresses, for a provider inside your network only (external False).
    allow_http: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Seconds to wait for an answer (headers, or the next piece of a stream); NULL: the server's.
    timeout_s: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # What requests to this provider may cost per UTC calendar month, micro-USD (forgeyard/gateway/
    # budgets.py); NULL: no budget. Admins only.
    budget_micros: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    models: Mapped[list["ProviderModel"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan", order_by="ProviderModel.upstream_model")


class ProviderModel(Base):
    """One model of a provider that routes may use. Prices are for cost tracking (forgeyard/gateway/
    pricing.py) and budgets: micro-USD per million tokens, NULL while unknown."""

    __tablename__ = "provider_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), index=True)
    # The provider's model name (for Azure: the deployment name).
    upstream_model: Mapped[str] = mapped_column(String(256))
    api: Mapped[str] = mapped_column(String(32))            # openai-chat | openai-embeddings
    display_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    context_length: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Request parameters to remove on top of the provider kind's own list.
    drop_params: Mapped[list] = mapped_column(JSON, default=list)
    price_input: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    price_output: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    price_cache_read: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    price_cache_write: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    price_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)   # manual | openrouter
    price_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # The OpenRouter model id whose prices this model takes, once an admin confirmed the match.
    price_ref: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    provider: Mapped[Provider] = relationship(back_populates="models")

    __table_args__ = (Index("ix_provider_models_name", "provider_id", "upstream_model", "api", unique=True),)


class ApiKey(Base):
    """A key for the gateway (/v1). Only the SHA-256 of the key is stored; the prefix is what
    people see to tell their keys apart."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # What the UI and the API use to refer to a key; never the key or its hash.
    public_id: Mapped[str] = mapped_column(String(36), unique=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16))                       # "fyk_" + 8 characters
    hash: Mapped[str] = mapped_column(String(64), unique=True)            # sha256(key) hex
    # Served model names this key may call; NULL: every deployment its owner may use.
    models: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # Requests and tokens per minute; NULL: the server's default (admins set defaults and caps).
    rpm: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tpm: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The admin who set that limit on someone else's key (username); NULL: the owner may change it.
    rpm_locked_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tpm_locked_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # May requests with this key go to cloud providers (1.2)? off: never (every key from before 1.2);
    # fallback: only when a route spills from local; on: cloud-only models too.
    cloud: Mapped[str] = mapped_column(String(8), default="off", server_default="off")
    cloud_locked_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # What this key's requests may cost at cloud providers per UTC calendar month, micro-USD
    # (forgeyard/gateway/budgets.py); NULL: no budget. Its owner may set it unless an admin did.
    budget_micros: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    budget_locked_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Which exposed workloads (/x/<name>, 1.3) this key may call: none (every key from before 1.3),
    # listed (the names in workload_list) or all.
    workloads: Mapped[str] = mapped_column(String(8), default="none", server_default="none")
    workload_list: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    workloads_locked_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship()


class UsageDaily(Base):
    """Gateway traffic per day, source, key, user and model: the Usage page and a deployment's
    numbers. Counted in memory and written every few seconds. Rows outlive keys, users and
    deployments (no foreign keys), so history stays readable."""

    __tablename__ = "usage_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[str] = mapped_column(String(10))                          # YYYY-MM-DD, UTC
    source: Mapped[str] = mapped_column(String(8))                        # api | console
    key_id: Mapped[int] = mapped_column(Integer, default=0)               # 0: the console
    user_id: Mapped[int] = mapped_column(Integer, default=0)
    username: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128))                       # the served name asked for
    # Who answered: "local" (a deployment's replica) or "pm:<provider model id>" (1.2), so cost can
    # be priced per provider model later.
    target: Mapped[str] = mapped_column(String(32), default="local", server_default="local")
    deployment_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)               # answered with a 4xx/5xx
    limited: Mapped[int] = mapped_column(Integer, default=0)              # of which 429 from the key's limits
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    # Of the prompt tokens, read from the provider's prompt cache; of the completion, reasoning.
    cached_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    reasoning_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    # Requests that moved on from the first tier (local busy, unavailable or refusing).
    spilled: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # What the requests cost (forgeyard/gateway/pricing.py), micro-USD: 0 for local targets; the
    # requests whose cost is unknown are counted in `unpriced` and not in cost_micros. Of the priced
    # ones, `cost_reported` took the upstream's own figure and `cost_estimated` assumed what was not
    # reported (cached tokens, a cache price).
    cost_micros: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    unpriced: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_reported: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_estimated: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Units of the model kinds added in 1.3 (migration 0012), 0 on rows from before: media items sent
    # in chat (images, audio clips, videos), characters turned into speech, images generated and their
    # size in kilopixels, audio or video produced in milliseconds, request and answer bytes, and the
    # milliseconds a replica was busy with the requests.
    media_items: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    characters: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    images: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    kilopixels: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    media_ms: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    bytes_in: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    bytes_out: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    busy_ms: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    latency_ms_sum: Mapped[int] = mapped_column(BigInteger, default=0)
    # Request counts per latency bucket (forgeyard/gateway/usage.py: BUCKETS_MS), for p50 / p95.
    latency_buckets: Mapped[list] = mapped_column(JSON, default=list)

    __table_args__ = (
        Index("ix_usage_daily_slot", "day", "source", "key_id", "user_id", "model", "target", unique=True),
        Index("ix_usage_daily_user_day", "user_id", "day"),
        Index("ix_usage_daily_deployment_day", "deployment_id", "day"),
    )


class BudgetAlert(Base):
    """A budget passed 80% or 100% in a month (forgeyard/gateway/budgets.py). One row per scope,
    month and threshold, so each alert is written to the audit log once."""

    __tablename__ = "budget_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(16))                        # key | person | provider
    scope_id: Mapped[int] = mapped_column(Integer)
    month: Mapped[str] = mapped_column(String(7))                         # YYYY-MM, UTC
    threshold: Mapped[int] = mapped_column(Integer)                       # 80 | 100
    spent_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    budget_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_budget_alerts_once", "scope", "scope_id", "month", "threshold", unique=True),)


class ServerSetting(Base):
    """Settings admins change at run time (model source allowlists, API key limits)."""

    __tablename__ = "server_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)



class VideoJob(Base):
    """A video being made (1.3, /v1/videos): asked through the gateway, made by one replica, which holds
    the job and its file (vLLM-Omni keeps both on the replica). The gateway relays its status and its
    content from that replica; a replica that is gone or restarted takes its jobs with it, and the job
    then reports `failed` (or, made already, its content is gone). Results expire after the
    deployment's `keep_minutes` and are deleted on the replica; the row goes a week later.

    Who may see it: the API key that asked (source "api", key_id) or, from the Playground, the person
    (source "console", user_id). `public_id` is the opaque id clients use; another key's id answers 404.
    The prompt is not stored."""

    __tablename__ = "video_jobs"
    __table_args__ = (Index("ix_video_jobs_owner", "source", "key_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(40), unique=True)       # video_<32 hex>, random
    source: Mapped[str] = mapped_column(String(8))                        # api | console
    key_id: Mapped[int] = mapped_column(Integer, default=0)               # 0: the console
    user_id: Mapped[int] = mapped_column(Integer, default=0)
    username: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128))                       # the served name asked for
    deployment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("deployments.id", ondelete="SET NULL"),
                                                         nullable=True, index=True)
    replica_uid: Mapped[str] = mapped_column(String(32))                  # the replica that holds it
    replica_epoch: Mapped[str] = mapped_column(String(32))                # its generation:restarts then
    upstream_id: Mapped[str] = mapped_column(String(128))                 # the engine's own job id
    status: Mapped[str] = mapped_column(String(16), index=True)           # queued | in_progress | completed | failed
    progress: Mapped[int] = mapped_column(Integer, default=0)
    size: Mapped[str] = mapped_column(String(16))                         # WIDTHxHEIGHT asked
    seconds: Mapped[int] = mapped_column(Integer)                         # seconds asked
    frames: Mapped[int] = mapped_column(Integer)                          # frames asked of the engine
    fps: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # what was made, once made
    gpus: Mapped[int] = mapped_column(Integer, default=1)
    error_code: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # A job at work past this is stopped and reported failed (the deployment's timeout when asked).
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    keep_s: Mapped[int] = mapped_column(Integer)                          # how long a result is kept, when asked
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    # Its file is gone from the replica: expired and deleted there, or lost with the replica.
    removed: Mapped[bool] = mapped_column(Boolean, default=False)
    counted: Mapped[bool] = mapped_column(Boolean, default=False)         # its usage is recorded


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
