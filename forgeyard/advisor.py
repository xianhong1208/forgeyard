"""The setup advisor: settings and combinations that are broken, risky or useless, found from what the
server already knows (its settings, the database and the agents' last reports). Nothing is stored
for it except, per admin, which findings they snoozed (user preferences) and whether an admin has
confirmed a backup of the secret key (one `server_settings` row).

Each check is a small function over a `Context` that yields `Finding`s:

    {id, severity: error | warning | info, title, detail, fix_url, fix_label, scope, pages}

- error: broken or unsafe now. It cannot be snoozed.
- warning: likely to cost money, leak, or break soon.
- info: useless or surprising.

`scope` names what it is about (server, machine:3, key:12, route:4, provider:2, deployment:5,
setting:NAME); `id@scope` is the finding's key, which a snooze remembers. `pages` says where the
web UI shows it as a banner (machines, keys, providers, models, users, and route:4 or provider:2 for
a detail page); every finding is also listed under Get started -> Setup health.

Admins only: the findings name machines, keys and people.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import logging
import re
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cached_property
from typing import Callable, Iterable, Iterator, Optional
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard import __version__, settings_check
from forgeyard.config import Settings
from forgeyard.db import (
    ApiKey, Deployment, GatewayRoute, Node, Provider, ProviderModel, Replica, RouteTarget, ServerSetting, User,
    Workload, as_utc, utcnow,
)

log = logging.getLogger("forgeyard")

SEVERITIES = ("error", "warning", "info")
SNOOZE_DAYS = 30
SNOOZE_PREF = "advisor_snoozed"        # user preference: {finding key: ISO time it wakes up}
MAX_SNOOZES = 200
BACKUP_SETTING = "secret_key_backup"   # server_settings row: {"kid", "by", "at"}

GIB = 1024**3
MIB = 1024**2
# A card counts as used by other processes when this much more is in use than Forgeyard reserved
# there (the driver and an idle context take a few hundred MiB).
OUTSIDE_USE_MIN = GIB
# A card with a running replica and less free than this (or VRAM_HEADROOM_MIB, if larger) left.
MARGIN_MIN = 256 * MIB
OFFLINE_GRACE = timedelta(minutes=5)
WAITING_AFTER = timedelta(minutes=15)
CACHE_FREE_MIN = 0.10
KEY_IDLE_DAYS = 90
PRICES_STALE_DAYS = 60
CA_WARN_DAYS = 90
CERT_WARN_DAYS = 30
CERT_ERROR_DAYS = 14
RESOLVE_TIMEOUT = 2.0
RESOLVE_TTL = 300.0

LEVEL_LABEL = {"off": "Never", "fallback": "Only to spill", "on": "Allowed"}
REFERENCE = settings_check.REFERENCE


@dataclass
class Finding:
    id: str
    severity: str
    title: str
    detail: str
    fix_url: str
    fix_label: str = "Fix it"
    scope: str = "server"
    pages: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.id}@{self.scope}"

    @property
    def snoozable(self) -> bool:
        return self.severity != "error"

    def out(self) -> dict:
        return {"id": self.id, "key": self.key, "severity": self.severity, "title": self.title, "detail": self.detail,
                "fix_url": self.fix_url, "fix_label": self.fix_label, "scope": self.scope, "pages": list(self.pages),
                "snoozable": self.snoozable}


def _names(items: Iterable[str], most: int = 3) -> str:
    items = list(items)
    shown = ", ".join(items[:most])
    return shown + (f" and {len(items) - most} more" if len(items) > most else "")


def _plural(n: int, word: str, many: Optional[str] = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _size(n: float) -> str:
    return f"{n / GIB:.1f} GiB"


# ------------------------------------------------------------------ host names and addresses
def is_loopback_host(host: str) -> bool:
    host = (host or "").strip().strip("[]").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def host_of(url: str) -> str:
    try:
        return (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return ""


def _public(addr: str) -> bool:
    from forgeyard.gateway import egress

    return egress.address_problem(addr, private_ok=False) is None


def _system_resolve(host: str) -> list[str]:
    return list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))


resolver: Callable[[str], list[str]] = _system_resolve   # tests replace it
_resolved: dict[str, tuple[float, list[str]]] = {}
_resolve_lock = threading.Lock()
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="advisor-dns")


def addresses(host: str) -> list[str]:
    """The host's addresses, cached for a few minutes; [] when it cannot be resolved in time."""
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    now = time.monotonic()
    with _resolve_lock:
        hit = _resolved.get(host)
        if hit and hit[0] > now:
            return hit[1]
    try:
        found = _pool.submit(resolver, host).result(timeout=RESOLVE_TIMEOUT)
    except Exception:
        found = []
    with _resolve_lock:
        if len(_resolved) > 500:
            _resolved.clear()
        _resolved[host] = (now + RESOLVE_TTL, found)
    return found


def reset() -> None:
    global resolver
    resolver = _system_resolve
    with _resolve_lock:
        _resolved.clear()


def _version(v: Optional[str]) -> tuple[int, ...]:
    m = re.match(r"\d+(?:\.\d+)*", (v or "").strip().lstrip("v"))
    return tuple(int(x) for x in m.group(0).split(".")) if m else ()


# ------------------------------------------------------------------ what the checks look at
class Context:
    def __init__(self, db: DbSession, settings: Settings, *, now: Optional[datetime] = None,
                 request_host: Optional[str] = None):
        self.db = db
        self.s = settings
        self.now = now or utcnow()
        self.request_host = (request_host or "").lower()
        self.found: list[Finding] = []

    def setting(self, key: str) -> Optional[dict]:
        row = self.db.get(ServerSetting, key)
        return dict(row.value or {}) if row is not None else None

    @cached_property
    def nodes(self) -> list[Node]:
        return list(self.db.scalars(select(Node).where(Node.status != "revoked")
                                    .options(selectinload(Node.accelerators)).order_by(Node.name)).all())

    @cached_property
    def users(self) -> list[User]:
        return list(self.db.scalars(select(User)).all())

    @cached_property
    def active_keys(self) -> list[ApiKey]:
        keys = self.db.scalars(select(ApiKey).where(ApiKey.revoked_at.is_(None)).options(selectinload(ApiKey.user))
                               .order_by(ApiKey.name)).all()
        return [k for k in keys if k.expires_at is None or as_utc(k.expires_at) > self.now]

    @cached_property
    def providers(self) -> list[Provider]:
        return list(self.db.scalars(select(Provider).options(selectinload(Provider.models)).order_by(Provider.name)).all())

    @cached_property
    def routes(self) -> list[GatewayRoute]:
        from forgeyard.models import routes

        return list(self.db.scalars(routes.query().order_by(GatewayRoute.served_name)).all())

    @cached_property
    def deployments(self) -> list[Deployment]:
        return list(self.db.scalars(select(Deployment).options(
            selectinload(Deployment.workload).selectinload(Workload.replicas).selectinload(Replica.node))
            .order_by(Deployment.name)).all())

    @cached_property
    def cloud(self) -> dict:
        from forgeyard.gateway import keys as gkeys

        return gkeys.get_cloud(self.db)

    def usable_cloud_targets(self, route: GatewayRoute) -> list[RouteTarget]:
        return [t for t in route.targets if is_cloud(t) and t.enabled and t.provider_model.provider.enabled]

    @cached_property
    def used_cloud_models(self) -> list[ProviderModel]:
        """Provider models outside your network that some route can send requests to."""
        seen: dict[int, ProviderModel] = {}
        for r in self.routes:
            for t in self.usable_cloud_targets(r):
                seen.setdefault(t.provider_model.id, t.provider_model)
        return list(seen.values())

    @cached_property
    def any_budget(self) -> bool:
        return (any(k.budget_micros is not None for k in self.active_keys)
                or any(u.budget_micros is not None for u in self.users if u.status == "active")
                or any(p.budget_micros is not None for p in self.providers if p.enabled))


def is_cloud(t: RouteTarget) -> bool:
    return t.kind == "provider" and t.provider_model is not None and bool(t.provider_model.provider.external)


def is_local(t: RouteTarget) -> bool:
    return not is_cloud(t)


def target_usable(t: RouteTarget) -> bool:
    if not t.enabled:
        return False
    if t.kind == "provider":
        return t.provider_model is not None and t.provider_model.provider.enabled
    return t.deployment is not None


# ------------------------------------------------------------------ the checks
Check = Callable[[Context], Iterable[Finding]]
CHECKS: list[Check] = []


def check(fn: Check) -> Check:
    CHECKS.append(fn)
    return fn


# --- server, network and security
@check
def settings_problems(ctx: Context) -> Iterator[Finding]:
    """Misspelled, ignored and risky server settings (forgeyard/settings_check.py)."""
    for p in settings_check.problems(ctx.s, db=ctx.db):
        yield Finding(p.id, p.severity, p.title, p.detail, p.fix_url, p.fix_label, p.scope)


@check
def secret_key_backup(ctx: Context) -> Iterator[Finding]:
    """D5: until an admin confirms a backup of the current key (again after the key changes)."""
    from forgeyard.security import crypto

    kid = crypto.box().kid
    if (ctx.setting(BACKUP_SETTING) or {}).get("kid") == kid:
        return
    where = (f"the value of {crypto.KEY_ENV}" if crypto.key_from_env() else str(crypto.key_path()))
    yield Finding("secret-key-backup", "warning", f"Back up the secret key (key id {kid})",
                  f"Keep a copy of {where} apart from the database. Without it, the stored Hugging Face tokens, "
                  "provider keys and workload environment values cannot be read after a restore. "
                  f"Confirm here once you have a backup of key id {kid}.",
                  "/guide/en/how-to#back-up-and-restore", "How to back up", scope=f"secret-key:{kid}")


@check
def host_loopback(ctx: Context) -> Iterator[Finding]:
    """V2: listening on loopback only, while machines joined from other addresses."""
    s = ctx.s
    if not is_loopback_host(s.host) or s.trusted_proxy_list:
        return
    if s.public_url.strip() and not is_loopback_host(host_of(s.public_url)):
        return  # a proxy on this machine forwards to it
    remote = [n for n in ctx.nodes if n.ip and not is_loopback_host(n.ip)]
    if remote:
        yield Finding("host-loopback", "error", f"The server listens on {s.host} only",
                      f"{_names(n.name for n in remote)} joined from another address, but HOST={s.host} accepts "
                      "connections from this machine only, so their agents cannot reach the server. Set "
                      "HOST=0.0.0.0 and restart.", f"{REFERENCE}#network-and-addresses", "Open the reference",
                      pages=("machines",))


@check
def public_url_unset(ctx: Context) -> Iterator[Finding]:
    """V3: no PUBLIC_URL on a server other machines can reach: links carry the address each browser used."""
    s = ctx.s
    if s.public_url.strip() or is_loopback_host(s.host):
        return
    here = ctx.request_host
    if here and is_loopback_host(here):
        yield Finding("public-url-unset", "warning", "PUBLIC_URL is not set",
                      f"You opened Forgeyard as {here}, so join commands, set-password links and the gateway "
                      "address made on this page say that too, and other machines cannot use them. Set PUBLIC_URL "
                      "to the address other machines use.", f"{REFERENCE}#network-and-addresses", "Open the reference",
                      pages=("machines",))
    else:
        yield Finding("public-url-unset", "info", "PUBLIC_URL is not set",
                      "Join commands, set-password links and the gateway address use whatever address each browser "
                      "opened Forgeyard with. Set PUBLIC_URL so they are always the one other machines use.",
                      f"{REFERENCE}#network-and-addresses", "Open the reference")


def _covers(names: list[str], ips: list[str], host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
        return any(ipaddress.ip_address(x) == ip for x in ips)
    except ValueError:
        pass
    for n in names:
        n = n.lower().rstrip(".")
        if n == host or (n.startswith("*.") and host.endswith(n[1:]) and host.count(".") == n.count(".")):
            return True
    return False


@check
def certificates(ctx: Context) -> Iterator[Finding]:
    """V5: your own certificate does not name PUBLIC_URL's host. V8: a certificate expires soon."""
    from pathlib import Path

    from forgeyard.security import tls

    s = ctx.s
    mode = tls.mode(s)
    if mode == "off":
        return
    try:
        if mode == "own":
            info = tls.read_cert(Path(s.tls_cert_file).read_bytes())
            what, warn_days, anchor = "Your server certificate", CERT_WARN_DAYS, "/guide/en/how-to#use-your-own-certificate-or-a-reverse-proxy"
        else:
            info = tls.read_cert((s.tls_dir / "ca.crt").read_bytes())
            what, warn_days, anchor = "Forgeyard's certificate authority", CA_WARN_DAYS, "/trust"
    except (OSError, ValueError, tls.TlsError):
        return
    host = host_of(s.public_url)
    if mode == "own" and host and not _covers(info.names, info.ips, host):
        yield Finding("certificate-name-missing", "error", f"Your certificate does not cover {host}",
                      f"PUBLIC_URL says {host}, but TLS_CERT_FILE names {_names(info.names + info.ips) or 'nothing'}. "
                      "Agents and browsers refuse it at that address. Get a certificate that names it.",
                      "/guide/en/how-to#use-your-own-certificate-or-a-reverse-proxy", "How to use your own certificate",
                      pages=("machines",))
    left = as_utc(info.not_after) - ctx.now
    if left < timedelta(days=warn_days):
        days = max(0, left.days)
        yield Finding("certificate-expiring", "error" if left < timedelta(days=CERT_ERROR_DAYS) else "warning",
                      f"{what} expires in {_plural(days, 'day')}",
                      f"{what} is valid until {as_utc(info.not_after):%Y-%m-%d}. After that agents and browsers refuse "
                      "the server." + (" Replace the files named by TLS_CERT_FILE and TLS_KEY_FILE; the server picks "
                                       "them up without a restart." if mode == "own" else
                                       " Making a new one means every agent and browser trusts it again."),
                      anchor, "What to do", scope=f"certificate:{mode}")


@check
def registrations_pending(ctx: Context) -> Iterator[Finding]:
    """V19: people waiting for an admin."""
    waiting = [u.username for u in ctx.users if u.status == "pending"]
    if waiting:
        yield Finding("registrations-pending", "info", f"{_plural(len(waiting), 'person', 'people')} waiting for approval",
                      f"{_names(waiting)} asked for an account. Approve or remove them on Users.", "/users", "Open Users",
                      pages=("users",))


# --- machines
@check
def agent_outdated(ctx: Context) -> Iterator[Finding]:
    """V21: an agent older than the server (an older protocol is an error)."""
    from forgeyard.machines.protocol import PROTOCOL_VERSION

    mine = _version(__version__)
    for n in ctx.nodes:
        old_protocol = n.protocol is not None and n.protocol < PROTOCOL_VERSION
        theirs = _version(n.agent_version)
        if not old_protocol and (not theirs or theirs >= mine):
            continue
        yield Finding("agent-outdated", "error" if old_protocol else "warning",
                      f"{n.name} runs an older agent ({n.agent_version or 'unknown'})",
                      f"{n.name} runs agent {n.agent_version or 'of an unknown version'}; the server is {__version__}."
                      + (" Its protocol is older than the server's, so it cannot work correctly." if old_protocol else
                         " Upgrade it to get the fixes and features of this release."),
                      "/guide/en/how-to#upgrade-the-server-and-the-agents", "How to upgrade", scope=f"machine:{n.id}",
                      pages=("machines",))


def _settled_holds(ctx: Context, node: Node):
    from forgeyard.workloads import placement

    return placement.usage(ctx.db, node.id, node=node).cards


@check
def gpus_shared_no_allowlist(ctx: Context) -> Iterator[Finding]:
    """V22: a machine whose cards are used by other processes, and nobody chose which ones Forgeyard may use."""
    from forgeyard.api.onboarding import gpu_choice_made
    from forgeyard.workloads import placement

    decided = None
    for n in ctx.nodes:
        gpus = [a for a in n.accelerators if placement.is_gpu(a)]
        if not gpus or n.gpu_allowlist is not None:
            continue
        if decided is None:
            decided = gpu_choice_made(ctx.db)
        if n.id in decided:
            continue
        cards = _settled_holds(ctx, n)
        busy = []
        for a in gpus:
            holds = cards.get(a.key, [])
            if a.vram_used is None or any(h.vram is None or h.pending for h in holds):
                continue  # cannot tell what is ours
            outside = a.vram_used - sum(h.vram for h in holds)
            if outside >= OUTSIDE_USE_MIN:
                busy.append((a.index, outside))
        if busy:
            listing = ", ".join(f"#{i} ({_size(b)})" for i, b in busy[:6]) + (" …" if len(busy) > 6 else "")
            yield Finding("gpus-shared-no-allowlist", "warning", f"Other processes use GPUs on {n.name}",
                          f"Memory on {listing} is in use outside Forgeyard, and Forgeyard may still place replicas on "
                          "any card there. Choose the GPUs it may use (Change GPU access).",
                          "/machines", "Choose GPUs", scope=f"machine:{n.id}", pages=("machines",))


@check
def machine_models_and_runtimes(ctx: Context) -> Iterator[Finding]:
    """V23: an online agent without a model cache. V27: process workloads refused there. V24: its model
    cache disk is nearly full. V26: simulated hardware."""
    from forgeyard.machines.protocol import FEATURE_MODELS, FEATURE_WORKLOADS
    from forgeyard.workloads import placement

    for n in ctx.nodes:
        if n.simulated:
            yield Finding("machine-simulated", "warning", f"{n.name} reports simulated hardware",
                          f"{n.name}'s agent was started with --simulate-inventory: its GPUs are not real. That is for "
                          "development only; revoke it on a real installation.", "/machines", "Open Machines",
                          scope=f"machine:{n.id}", pages=("machines",))
        if n.status != "online" or FEATURE_WORKLOADS not in (n.features or []):
            continue
        gpus = any(placement.is_gpu(a) for a in n.accelerators)
        if FEATURE_MODELS not in (n.features or []):
            yield Finding("machine-no-models", "warning" if gpus else "info", f"{n.name} serves no models",
                          f"The agent on {n.name} has no model cache (models.cache, FORGEYARD_MODEL_CACHE or "
                          "--model-cache), so no deployment is placed there" + (" although it has GPUs." if gpus else "."),
                          "/guide/en/reference#agent-settings", "Open the reference", scope=f"machine:{n.id}",
                          pages=("machines",))
        process = (n.runtime_info or {}).get("process") or {}
        if process and not process.get("ok"):
            yield Finding("process-workloads-off", "info", f"Process workloads are off on {n.name}",
                          f"The agent refuses process workloads: {process.get('reason') or 'no reason given'}.",
                          "/guide/en/reference#agent-settings", "Open the reference", scope=f"machine:{n.id}",
                          pages=("machines",))
        cache = n.model_cache or {}
        free, total = cache.get("free_bytes"), cache.get("total_bytes")
        if isinstance(free, int) and isinstance(total, int) and total > 0 and free < CACHE_FREE_MIN * total:
            yield Finding("model-cache-full", "warning", f"The model cache on {n.name} is nearly full",
                          f"{_size(free)} of {_size(total)} free. New models cannot be downloaded there once it is "
                          "full: remove models that are no longer deployed, or move the cache to a larger disk.",
                          "/guide/en/how-to#move-a-model-cache", "How to move a cache", scope=f"machine:{n.id}",
                          pages=("machines",))


@check
def offline_with_replicas(ctx: Context) -> Iterator[Finding]:
    """V25: a machine that has been offline for a while still holds replicas."""
    from forgeyard.workloads import placement

    for n in ctx.nodes:
        if n.status != "offline":
            continue
        last = as_utc(n.last_heartbeat_at) if n.last_heartbeat_at else None
        if last is not None and ctx.now - last < OFFLINE_GRACE:
            continue
        held = placement.holding(ctx.db, n.id)
        if held:
            names = sorted({r.workload.name for r in held})
            since = f"since {last:%Y-%m-%d %H:%M} UTC" if last else "and has never sent a heartbeat"
            yield Finding("machine-offline-with-replicas", "warning", f"{n.name} is offline with replicas on it",
                          f"{n.name} has been offline {since}, and {_names(names)} still "
                          f"{'has' if len(names) == 1 else 'have'} replicas placed there that cannot answer. Start its "
                          "agent again, or revoke the machine so they are placed elsewhere.",
                          "/guide/en/troubleshooting#an-agent-shows-offline", "Why a machine is offline",
                          scope=f"machine:{n.id}", pages=("machines", "models"))


# --- models and deployments
@check
def replica_margin_low(ctx: Context) -> Iterator[Finding]:
    """V28: a card with a running replica has almost no free memory left: other work grew."""
    from forgeyard.workloads import placement

    floor = max(placement.headroom(), MARGIN_MIN)
    for n in ctx.nodes:
        if n.status != "online":
            continue
        cards = _settled_holds(ctx, n)
        for a in n.accelerators:
            holds = [h for h in cards.get(a.key, []) if h.vram and not h.pending]
            if not placement.is_gpu(a) or not holds or a.vram_free is None or a.vram_free >= floor:
                continue
            names = sorted({h.workload for h in holds})
            outside = max(0, (a.vram_used or 0) - sum(h.vram or 0 for h in cards.get(a.key, [])))
            yield Finding("replica-margin-low", "warning", f"GPU #{a.index} on {n.name} is almost full",
                          f"{_size(a.vram_free)} is free on the card that runs {_names(names)}; Forgeyard keeps "
                          f"{_size(floor)}. Other processes use about {_size(outside)} there. If they grow, the "
                          "replica can run out of memory: free memory on the card, or move the deployment.",
                          "/models", "Open Models", scope=f"gpu:{n.id}:{a.index}", pages=("models", "machines"))


@check
def deployments_waiting(ctx: Context) -> Iterator[Finding]:
    """V29: a deployment has waited for room for a long time."""
    for d in ctx.deployments:
        w = d.workload
        if w is None or w.desired_state != "running":
            continue
        stuck = [r for r in w.replicas if not r.final and r.state == "pending" and r.node_id is None
                 and r.updated_at is not None and ctx.now - as_utc(r.updated_at) > WAITING_AFTER]
        if stuck:
            reason = next((r.reason for r in stuck if r.reason), None)
            yield Finding("deployment-waiting", "warning", f"{d.name} is still waiting for room",
                          f"{_plural(len(stuck), 'replica')} of {d.name} found no machine for over "
                          f"{int(WAITING_AFTER.total_seconds() // 60)} minutes" + (f": {reason}" if reason else ".")
                          + " Free memory, allow more GPUs, or lower its context or concurrency.",
                          f"/models/deployments/{d.id}", "Open the deployment", scope=f"deployment:{d.id}",
                          pages=("models", f"deployment:{d.id}"))


# llama.cpp's short and long spellings of the flags Forgeyard sets.
FLAG_ALIASES = {"-c": "--ctx-size", "-ngl": "--n-gpu-layers", "--gpu-layers": "--n-gpu-layers", "-np": "--parallel",
                "-t": "--threads", "-m": "--model", "-a": "--alias"}


def _flag(token: str) -> Optional[str]:
    if not token.startswith("-") or token in ("-", "--") or re.match(r"-\d", token):
        return None
    name = token.split("=", 1)[0]
    return FLAG_ALIASES.get(name, name)


def managed_flags(recipe) -> set[str]:
    """The flags a recipe sets itself (and --api-key, which carries the replica's key)."""
    tokens = list(recipe.args)
    for o in recipe.optional_args:
        tokens += o.args
    for v in recipe.variants:
        tokens += v.args or []
    return {f for f in (_flag(t) for t in tokens) if f} | {"--api-key"}


@check
def extra_args_override(ctx: Context) -> Iterator[Finding]:
    """V31: extra engine arguments that repeat a flag Forgeyard sets (the estimate, the binding or the
    key guard then no longer hold)."""
    from forgeyard.models import catalog

    recipes: dict[str, object] = {}
    for d in ctx.deployments:
        extra = (d.params or {}).get("extra_args") or []
        if not extra:
            continue
        if d.recipe not in recipes:
            recipes[d.recipe] = catalog.get_recipe(ctx.db, d.recipe)
        recipe = recipes[d.recipe]
        if recipe is None:
            continue
        managed = managed_flags(recipe)
        clash = sorted({f for f in (_flag(a) for a in extra) if f in managed})
        if clash:
            yield Finding("extra-args-override", "warning", f"{d.name}'s extra engine arguments override Forgeyard's",
                          f"{', '.join(clash)} {'is' if len(clash) == 1 else 'are'} set by Forgeyard from the "
                          "deployment's parameters (memory, context, address, key). Repeating "
                          f"{'it' if len(clash) == 1 else 'them'} in Extra engine arguments can break the memory "
                          "estimate, the gateway's connection or the key guard. Use the parameters instead.",
                          f"/models/deployments/{d.id}", "Open the deployment", scope=f"deployment:{d.id}",
                          pages=("models", f"deployment:{d.id}"))


# --- gateway, cloud and money
def _reach(ctx: Context, providers: set[int]) -> dict[str, list[str]]:
    """Route name -> names of the given providers it can send requests to."""
    out: dict[str, list[str]] = {}
    for r in ctx.routes:
        names = sorted({t.provider_model.provider.name for t in ctx.usable_cloud_targets(r)
                        if t.provider_model.provider.id in providers})
        if names:
            out[r.served_name] = names
    return out


@check
def key_cloud_without_budget(ctx: Context) -> Iterator[Finding]:
    """V35: a key may use the cloud and no budget applies to it."""
    from forgeyard.gateway import keys as gkeys

    unbudgeted = {p.id for p in ctx.providers if p.external and p.enabled and p.budget_micros is None}
    reach = _reach(ctx, unbudgeted) if unbudgeted else {}
    if not reach:
        return
    for k in ctx.active_keys:
        level = gkeys.effective_cloud(k, k.user, ctx.cloud["max"])
        if level == "off" or k.budget_micros is not None or (k.user is not None and k.user.budget_micros is not None):
            continue
        models = sorted(reach) if k.models is None else sorted(set(reach) & set(k.models))
        if not models:
            continue
        providers = sorted({p for m in models for p in reach[m]})
        owner = k.user.username if k.user else "?"
        yield Finding("key-cloud-without-budget", "warning", f"The key {k.name} may spend with no limit",
                      f"{owner}'s key {k.name} is set to {LEVEL_LABEL[level]} for cloud providers and reaches "
                      f"{_names(models)} through {_names(providers)}, but no budget applies: none on the key, on "
                      f"{owner}, or on {'that provider' if len(providers) == 1 else 'those providers'}. Set a monthly "
                      "budget on one of them.", "/keys", "Open API keys", scope=f"key:{k.id}", pages=("keys",))


def priced(pm: ProviderModel) -> bool:
    if pm.price_input is None:
        return False
    return pm.api == "openai-embeddings" or pm.price_output is not None


@check
def cloud_models_unpriced(ctx: Context) -> Iterator[Finding]:
    """V36: under any budget, a route's cloud model without a price is refused (402 budget_unpriced).
    V37: without budgets its cost is unknown."""
    for pm in ctx.used_cloud_models:
        if priced(pm):
            continue
        p = pm.provider
        if ctx.any_budget:
            yield Finding("cloud-model-unpriced", "error", f"{pm.upstream_model} has no price, and budgets are set",
                          f"Requests to {pm.upstream_model} ({p.name}) are refused with 402 budget_unpriced whenever a "
                          "budget applies, because their cost cannot be reserved. Set its input and output prices, or "
                          "refresh them from OpenRouter.", f"/providers/{p.id}", "Set prices", scope=f"provider-model:{pm.id}",
                          pages=("providers", f"provider:{p.id}"))
        else:
            yield Finding("cloud-model-cost-unknown", "info", f"The cost of {pm.upstream_model} is unknown",
                          f"{pm.upstream_model} ({p.name}) has no price, so Usage cannot say what it costs, and it will "
                          "be refused as soon as a budget is set. Set its prices, or refresh them from OpenRouter.",
                          f"/providers/{p.id}", "Set prices", scope=f"provider-model:{pm.id}",
                          pages=("providers", f"provider:{p.id}"))


@check
def providers_setup(ctx: Context) -> Iterator[Finding]:
    """V38: no API key where the kind needs one. V39: inside your network, but a public address.
    V46: EGRESS_ALLOWED_HOSTS leaves it out."""
    from forgeyard.gateway import quirks
    from forgeyard.models.fetch import host_matches

    allowed = ctx.s.egress_allowed_host_list
    for p in ctx.providers:
        if not p.enabled:
            continue
        kind = quirks.get(p.kind)
        pages = ("providers", f"provider:{p.id}")
        if kind.key_required and not p.key_last4:
            yield Finding("provider-no-key", "error", f"{p.name} has no API key",
                          f"{kind.label} needs an API key, and {p.name} has none: every request to it fails.",
                          f"/providers/{p.id}", "Add the key", scope=f"provider:{p.id}", pages=pages)
        host = host_of(p.base_url)
        if not host:
            continue
        if allowed and not host_matches(host, allowed):
            yield Finding("provider-blocked-by-egress", "error", f"Requests to {p.name} are blocked",
                          f"{host} is not in EGRESS_ALLOWED_HOSTS, so every request to {p.name} answers 502 "
                          "provider_blocked. Add the host to EGRESS_ALLOWED_HOSTS, or disable the provider.",
                          f"{REFERENCE}#cloud-providers", "Open the reference", scope=f"provider:{p.id}", pages=pages)
        if not p.external:
            found = addresses(host)
            if found and all(_public(a) for a in found):
                yield Finding("provider-inside-public-address", "warning",
                              f"{p.name} is marked as inside your network, but its address is public",
                              f"{host} resolves to {_names(found, 2)}. A provider inside your network is used by every "
                              "key, whatever its cloud setting, and no budget applies to it. If it is a third party, "
                              "tick Outside your network (cloud).", f"/providers/{p.id}", "Open the provider",
                              scope=f"provider:{p.id}", pages=pages)


@check
def cloud_defaults(ctx: Context) -> Iterator[Finding]:
    """V40: new keys may use the cloud by default, or people may give their own keys Allowed.
    V47: cloud routes exist, but no key may use the cloud."""
    from forgeyard.gateway import keys as gkeys

    if not ctx.used_cloud_models:
        return
    c = ctx.cloud
    if c["default"] != "off" or c["max"] == "on":
        parts = []
        if c["default"] != "off":
            parts.append(f"New keys get {LEVEL_LABEL[c['default']]} for cloud providers.")
        if c["max"] == "on":
            parts.append("Anyone may set their own keys to Allowed, which sends requests to the cloud tier whenever "
                         "a route has one.")
        yield Finding("cloud-default-open", "info", "Keys may use cloud providers by default",
                      " ".join(parts) + " Check that this is what you want: it costs money and sends prompts outside "
                      "your network.", "/keys", "Open Cloud for keys", pages=("keys",))
    elif not any(gkeys.effective_cloud(k, k.user, c["max"]) != "off" for k in ctx.active_keys):
        names = sorted({r.served_name for r in ctx.routes if ctx.usable_cloud_targets(r)})
        yield Finding("cloud-unreachable", "info", "Cloud routes exist, but no key may use them",
                      f"{_names(names)} {'has' if len(names) == 1 else 'have'} cloud targets, but every key's cloud "
                      "setting is Never and new keys get Never too, so only the playground reaches them. Give a key "
                      "Only to spill or Allowed, or change Cloud for keys.", "/keys", "Open API keys", pages=("keys",))


@check
def routes_setup(ctx: Context) -> Iterator[Finding]:
    """V41: a cloud tier before the first local one. V42: a cloud tier or spill rule that is never used.
    V43: no usable target. V44: a mixed route whose local deployments are stopped."""
    for r in ctx.routes:
        url, pages = f"/models/routes/{r.id}", ("models", f"route:{r.id}")
        usable = [t for t in r.targets if target_usable(t)]
        if not usable:
            yield Finding("route-no-target", "error", f"{r.served_name} has no target that can answer",
                          f"Every target of {r.served_name} is disabled (or its provider is), so it answers 503 "
                          "no_target.", url, "Open the route", scope=f"route:{r.id}", pages=pages)
            continue
        local = [t for t in usable if is_local(t)]
        cloud = [t for t in usable if is_cloud(t)]
        if local and cloud and min(t.tier for t in cloud) < min(t.tier for t in local):
            yield Finding("route-cloud-before-local", "warning", f"{r.served_name} tries the cloud before local",
                          f"A cloud tier of {r.served_name} comes before its first local tier: keys set to Allowed go "
                          "to the cloud first even when the local replicas are idle, and keys set to Only to spill "
                          "skip that tier. Move the local tier first unless that is what you want.", url, "Open the route",
                          scope=f"route:{r.id}", pages=pages)
        if r.local_only and cloud:
            yield Finding("route-cloud-never-used", "info", f"{r.served_name}'s cloud tier is never used",
                          f"{r.served_name} is Local only, so its cloud targets never get a request.", url, "Open the route",
                          scope=f"route:{r.id}", pages=pages)
        elif local and (r.spill_on_long_prompt or (r.wait_ms or 0) > 0) \
                and not any(t.tier > min(x.tier for x in local) for t in usable):
            rule = "Spill a prompt that is too long" if r.spill_on_long_prompt else "Wait for a free local slot"
            yield Finding("route-cloud-never-used", "info", f"{r.served_name}: {rule} has no effect",
                          f"{r.served_name} has no tier after its local one, so there is nothing to spill to.",
                          url, "Open the route", scope=f"route:{r.id}", pages=pages)
        if local and cloud:
            deps = [t.deployment for t in local if t.kind == "local" and t.deployment is not None]
            if deps and all(d.workload is not None and d.workload.desired_state == "stopped" for d in deps):
                yield Finding("route-local-stopped", "warning", f"{r.served_name}'s local deployment is stopped",
                              f"{_names(d.name for d in deps)} {'is' if len(deps) == 1 else 'are'} stopped, so every "
                              f"request to {r.served_name} from a key that may use the cloud goes to the cloud.",
                              url, "Open the route", scope=f"route:{r.id}", pages=pages)


@check
def budgets_zero(ctx: Context) -> Iterator[Finding]:
    """V45: a budget of $0 blocks every cloud request (empty means no budget)."""
    zero = ([("key", k.id, f"the key {k.name}", "/keys", ("keys",)) for k in ctx.active_keys if k.budget_micros == 0]
            + [("person", u.id, u.username, "/usage?tab=cost", ()) for u in ctx.users if u.budget_micros == 0]
            + [("provider", p.id, p.name, f"/providers/{p.id}", ("providers", f"provider:{p.id}"))
               for p in ctx.providers if p.budget_micros == 0])
    for scope, sid, name, url, pages in zero:
        yield Finding("budget-zero", "info", f"The budget of {name} is $0",
                      "A budget of $0 blocks every cloud request it applies to. Leave it empty for no budget.",
                      url, "Change it", scope=f"{scope}:{sid}", pages=pages)


@check
def prices_stale(ctx: Context) -> Iterator[Finding]:
    """V48: prices from OpenRouter that were not refreshed for a while."""
    old = [pm for pm in ctx.used_cloud_models if pm.price_source == "openrouter" and pm.price_checked_at is not None
           and ctx.now - as_utc(pm.price_checked_at) > timedelta(days=PRICES_STALE_DAYS)]
    if old:
        oldest = min(as_utc(pm.price_checked_at) for pm in old)
        yield Finding("prices-stale", "info", f"Prices of {_plural(len(old), 'model')} are over {PRICES_STALE_DAYS} days old",
                      f"{_names(pm.upstream_model for pm in old)} were last refreshed from OpenRouter on "
                      f"{oldest:%Y-%m-%d}. Refresh them so costs and budgets use current prices.",
                      "/providers", "Refresh prices", pages=("providers",))


@check
def keys_idle(ctx: Context) -> Iterator[Finding]:
    """V50: keys that never expire and have not been used for a long time."""
    cutoff = ctx.now - timedelta(days=KEY_IDLE_DAYS)
    idle = [k for k in ctx.active_keys if k.expires_at is None and as_utc(k.last_used_at or k.created_at) < cutoff]
    if idle:
        yield Finding("keys-idle-never-expire", "info",
                      f"{_plural(len(idle), 'key')} never expire{'s' if len(idle) == 1 else ''} and "
                      f"{'was' if len(idle) == 1 else 'were'} not used for {KEY_IDLE_DAYS} days",
                      f"{_names(f'{k.name} ({k.user.username if k.user else '?'})' for k in idle)}. A key nobody uses "
                      "is only a risk if it leaks: revoke the ones that are no longer needed.", "/keys", "Open API keys",
                      pages=("keys",))


# ------------------------------------------------------------------ running them
ORDER = {s: i for i, s in enumerate(SEVERITIES)}


def run(db: DbSession, settings: Settings, *, now: Optional[datetime] = None, request_host: Optional[str] = None,
        strict: bool = False) -> list[Finding]:
    """Every finding, errors first. A check that fails is logged and skipped (`strict` raises)."""
    ctx = Context(db, settings, now=now, request_host=request_host)
    for fn in CHECKS:
        try:
            ctx.found.extend(fn(ctx))
        except Exception:
            if strict:
                raise
            log.exception("Setup advisor: the check %s failed", fn.__name__)
    # Keys may use the cloud by default AND some key has no budget: more than a surprise.
    if any(f.id == "key-cloud-without-budget" for f in ctx.found):
        for f in ctx.found:
            if f.id == "cloud-default-open":
                f.severity = "warning"
    return sorted(ctx.found, key=lambda f: (ORDER[f.severity], f.title))


# ------------------------------------------------------------------ snoozing (per admin)
def snoozed(user: User, now: datetime) -> dict[str, datetime]:
    out = {}
    for key, until in ((user.preferences or {}).get(SNOOZE_PREF) or {}).items():
        try:
            when = as_utc(datetime.fromisoformat(until))
        except (TypeError, ValueError):
            continue
        if when > now:
            out[key] = when
    return out


def set_snooze(user: User, key: str, until: Optional[datetime], now: datetime) -> None:
    """Snooze (until a time) or wake (None) one finding; expired entries are dropped."""
    current = {k: v for k, v in snoozed(user, now).items() if k != key}
    if until is not None:
        current[key] = until
    if len(current) > MAX_SNOOZES:
        current = dict(sorted(current.items(), key=lambda kv: kv[1])[-MAX_SNOOZES:])
    prefs = dict(user.preferences or {})
    prefs[SNOOZE_PREF] = {k: v.isoformat() for k, v in current.items()}
    user.preferences = prefs


def split(findings: list[Finding], user: User, now: datetime) -> tuple[list[dict], list[dict]]:
    """(shown, snoozed) for this admin. Errors are always shown."""
    sleeping = snoozed(user, now)
    shown, hidden = [], []
    for f in findings:
        out = f.out()
        if f.snoozable and f.key in sleeping:
            out["snoozed_until"] = sleeping[f.key].isoformat()
            hidden.append(out)
        else:
            shown.append(out)
    return shown, hidden
