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
web UI shows it as a banner (machines, serving, keys, providers, cloud-models, users, and route:4 or provider:2 for
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
    ApiKey, Deployment, GatewayRoute, Node, Provider, ProviderModel, Replica, RouteTarget, ServerSetting, UsageDaily,
    User, Workload, as_utc, utcnow,
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
# A running exclusive replica that reserves less than half of a card at least this big is pointed at
# Share VRAM (1.3.1): the rest of the card could hold other models.
LARGE_CARD = 24 * GIB
# 1.5.0 (#37): a running deployment with sleep when idle off that had no request for this long and holds GPU memory
# is pointed at it (Q1 A); one that sleeps on, woke more than WAKES_OFTEN times in a day and takes longer than
# SLOW_START to start is pointed at a longer idle time (Q7 A).
IDLE_HINT = timedelta(days=3)
WAKES_OFTEN = 10
SLOW_START = 60.0
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
    global resolver, listening
    resolver = _system_resolve
    listening = _listening
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
        from forgeyard.models import trials

        # 1.5.2 (#95): an engine trial's temporary deployment is not one to give advice about.
        return list(self.db.scalars(select(Deployment).where(trials.not_trial()).options(
            selectinload(Deployment.workload).selectinload(Workload.replicas).selectinload(Replica.node))
            .order_by(Deployment.name)).all())

    @cached_property
    def last_use_day(self) -> dict[int, str]:
        """The last UTC day (YYYY-MM-DD) each deployment answered or was asked a request, from Usage."""
        from sqlalchemy import func

        rows = self.db.execute(select(UsageDaily.deployment_id, func.max(UsageDaily.day))
                               .where(UsageDaily.deployment_id.is_not(None), UsageDaily.requests > 0)
                               .group_by(UsageDaily.deployment_id)).all()
        return {dep_id: day for dep_id, day in rows}

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
                  "provider keys and apps' environment values cannot be read after a restore. "
                  "Download a backup here (encrypted with a passphrase you choose, with the CA), or confirm here "
                  f"once you have your own backup of key id {kid}.",
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
                      "HOST=0.0.0.0 and restart.", f"{REFERENCE}#common-settings", "Open the reference",
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
                      "to the address other machines use.", f"{REFERENCE}#common-settings", "Open the reference",
                      pages=("machines",))
    else:
        yield Finding("public-url-unset", "info", "PUBLIC_URL is not set",
                      "Join commands, set-password links and the gateway address use whatever address each browser "
                      "opened Forgeyard with. Set PUBLIC_URL so they are always the one other machines use.",
                      f"{REFERENCE}#common-settings", "Open the reference")


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


@check
def admins_without_mfa(ctx: Context) -> Iterator[Finding]:
    """1.6.0 (#39): active admins who sign in with a password alone. A warning once two-factor sign-in is required
    of admins (PR 2): they are then on a deadline."""
    from forgeyard.security import mfa, mfa_policy

    with_mfa = mfa.users_with_factor(ctx.db)
    bare = sorted(u.username for u in ctx.users if u.role == "admin" and u.status == "active" and u.id not in with_mfa)
    if not bare:
        return
    required = mfa_policy.load(ctx.db).level != "off"  # admins and everyone both cover admins
    detail = (f"{_names(bare)} {'signs' if len(bare) == 1 else 'sign'} in with a password alone. An admin account "
              "can change everything here: add an authenticator app in My account (each admin does it for their own "
              "account).")
    if required:
        detail += (" Two-factor sign-in is required of admins: after the grace period, an admin without one can "
                   "only add it.")
    yield Finding("admins-without-mfa", "warning" if required else "info",
                  f"{_plural(len(bare), 'admin')} without two-factor sign-in", detail, "/account", "Open My account",
                  pages=("users",))


@check
def mfa_setup_pending(ctx: Context) -> Iterator[Finding]:
    """1.6.0 (#39, PR 2): two-factor sign-in is required and some people it covers have not added it yet."""
    from forgeyard.security import mfa, mfa_policy

    policy = mfa_policy.load(ctx.db)
    if policy.level == "off":
        return
    with_mfa = mfa.users_with_factor(ctx.db)
    waiting: list[tuple[str, datetime]] = []
    overdue: list[str] = []
    for u in ctx.users:
        if u.status != "active":
            continue
        e = mfa_policy.enrolment(ctx.db, u, policy=policy, has_factor=u.id in with_mfa)
        if e is None:
            continue
        if e["due"]:
            overdue.append(u.username)
        else:
            waiting.append((u.username, e["by"]))
    if not waiting and not overdue:
        return
    parts = []
    if waiting:
        first = min(by for _, by in waiting)
        parts.append(f"{_plural(len(waiting), 'person', 'people')} still in the grace period "
                     f"({_names(sorted(n for n, _ in waiting))}; the first one ends {first:%Y-%m-%d %H:%M} UTC).")
    if overdue:
        parts.append(f"{_plural(len(overdue), 'person', 'people')} past it ({_names(sorted(overdue))}): they can "
                     "only add an authenticator until they do.")
    yield Finding("mfa-setup-pending", "info",
                  f"{_plural(len(waiting) + len(overdue), 'person', 'people')} still to add two-factor sign-in",
                  " ".join(parts) + " Each person adds it in My account; an admin can only remind them.",
                  "/users", "Open Users", pages=("users",))


# --- machines
@check
def agent_outdated(ctx: Context) -> Iterator[Finding]:
    """V21: an agent older than the server (an older protocol is an error), or (1.4.2, #122) of the server's
    version but without a feature such an agent declares (built from older code). The fix opens the machine's
    update command."""
    from forgeyard.install.update import behind

    from forgeyard.machines.protocol import FEATURE_GUARD_TLS

    for n in ctx.nodes:
        why = behind(n)
        if why is None:
            continue
        if not why["older"] and not why["old_protocol"] and why["missing"] == [FEATURE_GUARD_TLS]:
            continue  # 1.4.5: said better by replica-traffic-plain ("not encrypted: update the agent")
        old_protocol = why["old_protocol"]
        if why["older"] or old_protocol:
            title = f"{n.name} runs an older agent ({n.agent_version or 'unknown'})"
            detail = (f"{n.name} runs agent {n.agent_version or 'of an unknown version'}; the server is {__version__}."
                      + (" Its protocol is older than the server's, so it cannot work correctly." if old_protocol else
                         " Upgrade it to get the fixes and features of this release."))
        else:
            title = f"{n.name}'s agent lacks what this server uses"
            detail = (f"{n.name} runs agent {n.agent_version}, the server's version, but does not offer "
                      f"{', '.join(why['missing'])}: it was built from older code. Update it.")
        yield Finding("agent-outdated", "error" if old_protocol else "warning", title, detail,
                      f"/machines/{n.id}#update", "Update command", scope=f"machine:{n.id}", pages=("machines",))


@check
def replica_traffic(ctx: Context) -> Iterator[Finding]:
    """1.4.5 (#163): how the gateway reaches each machine's replicas (security/replica_tls.py `status`). An agent too
    old for TLS keeps serving over plain HTTP (Tony's decision on #163): a warning that opens its update command. A
    TLS agent whose certificate is missing, expired or not renewed: an error, since its replicas cannot be reached
    (never over plain HTTP instead)."""
    from forgeyard.security import replica_tls

    for n in ctx.nodes:
        st = replica_tls.status(n, now=ctx.now, settings=ctx.s)
        if st is None or st["state"] in ("ok", "pending"):
            continue
        if st["state"] == "plain":
            yield Finding("replica-traffic-plain", "warning", f"Traffic to {n.name} is not encrypted: update the agent",
                          f"The gateway sends requests, answers and the replicas' internal keys to {n.name} over plain "
                          f"HTTP: its agent ({n.agent_version or 'of an unknown version'}) cannot serve TLS. It keeps "
                          "serving meanwhile; once its agent is updated, that traffic is encrypted.",
                          f"/machines/{n.id}#update", "Update command", scope=f"machine:{n.id}", pages=("machines",))
            continue
        why = f" ({st['reason']})" if st.get("reason") else ""
        if st["state"] == "missing":
            title = f"{n.name} has no certificate for its replicas"
            detail = (f"{n.name}'s agent serves its replicas over TLS only, and it has no certificate from this "
                      f"server{why}. The gateway cannot reach its replicas until it has one, and never falls back to "
                      "plain HTTP: requests go to replicas on other machines. Look at the agent's log; restarting the "
                      "agent asks again.")
        else:
            end = st["not_after"][:10] if st.get("not_after") else "?"
            title = (f"{n.name}'s replica certificate has expired" if st["state"] == "expired"
                     else f"{n.name}'s replica certificate is not being renewed")
            detail = (f"The certificate {n.name}'s agent serves its replicas with is valid until {end}{why}. It is "
                      f"renewed {replica_tls.RENEW_BEFORE_DAYS} days before that while the agent is connected; "
                      + ("an expired one is refused, so its replicas cannot be reached (never over plain HTTP instead)."
                         if st["state"] == "expired" else "that keeps failing, and once it expires its replicas cannot "
                                                          "be reached.")
                      + " Look at the agent's log, and whether it is connected.")
        yield Finding("replica-certificate", "error", title, detail,
                      "/guide/en/troubleshooting#a-machine-has-no-certificate-for-its-replicas", "What to do",
                      scope=f"machine:{n.id}", pages=("machines", "serving"))


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
                          f"The agent on {n.name} has no model cache, so no deployment is placed there"
                          + (" although it has GPUs" if gpus else "") + ". Its models.cache (FORGEYARD_MODEL_CACHE, "
                          "--model-cache) is empty, or the agent is older than 1.3.1, which had no default: set "
                          "it, or update the agent (the default is /srv/forgeyard-models).",
                          "/guide/en/reference#agent-settings", "Open the reference", scope=f"machine:{n.id}",
                          pages=("machines",))
        process = (n.runtime_info or {}).get("process") or {}
        if process and not process.get("ok"):
            yield Finding("process-workloads-off", "info", f"Processes are off on {n.name}",
                          f"The agent refuses to run apps and engines as processes: {process.get('reason') or 'no reason given'}.",
                          "/guide/en/reference#agent-settings", "Open the reference", scope=f"machine:{n.id}",
                          pages=("machines",))
        cache = n.model_cache or {}
        free, total = cache.get("free_bytes"), cache.get("total_bytes")
        if isinstance(free, int) and isinstance(total, int) and total > 0 and free < CACHE_FREE_MIN * total:
            yield Finding("model-cache-full", "warning", f"The model cache on {n.name} is nearly full",
                          f"{_size(free)} of {_size(total)} free. New models cannot be downloaded there once it is "
                          "full: remove models that are no longer deployed (the machine's page, Models in cache), "
                          "or move the cache to a larger disk.",
                          "/guide/en/how-to#free-disk-space-on-a-machine", "How to free disk space",
                          scope=f"machine:{n.id}",
                          pages=("machines",))


@check
def machine_gpu_access(ctx: Context) -> Iterator[Finding]:
    """1.4.5 (#202): the agent's GPU access check (agent/gpu_check.py) says a new process there cannot open the GPUs.
    In a container that is the device access the NVIDIA runtime hook set up behind systemd's back, lost when the host
    ran `systemctl daemon-reload`: restarting the container gives it back, and the machine page's update command passes
    the devices with --device too, which keeps them. Meanwhile process workloads are not placed on those GPUs."""
    from forgeyard.machines import gpu_access

    for n in ctx.nodes:
        c = gpu_access.check(n)
        if n.status != "online" or c is None or c["ok"]:
            continue
        failing = gpu_access.failing_vendors(n)
        label = " and ".join(gpu_access.VENDOR_LABEL[v] for v in sorted(failing))
        why = f" ({c['reason']})" if c.get("reason") else ""
        held = (f" Until then no process workload or process engine is placed on its {label} GPUs; containers are not "
                "affected, and what already runs keeps running.")
        if gpu_access.in_container(n):
            detail = (f"New processes on {n.name} cannot open its GPUs{why}: the agent's container lost its device "
                      "access, e.g. after the host ran systemctl daemon-reload. Restart the agent container, and "
                      "update it with the command on the machine's page, which keeps the access (it passes the "
                      "devices with --device as well)." + held)
        else:
            detail = (f"New processes on {n.name}, running as {c.get('user') or 'the workload account'}, cannot open "
                      f"its GPUs{why}. Check the device permissions and that account's groups, then restart the "
                      "agent." + held)
        yield Finding("machine-gpu-access-lost", "error", f"New processes on {n.name} cannot open its GPUs", detail,
                      f"/machines/{n.id}#update", "Update command", scope=f"machine:{n.id}", pages=("machines",))


# 1.5.4: how long Setup health speaks of a machine's GPUs being added or removed (machine_gpu_changes).
GPU_CHANGE_RECENT = timedelta(days=7)


@check
def machine_gpus_hidden(ctx: Context) -> Iterator[Finding]:
    """1.5.4: the host has GPUs its agent does not see (agent/gpu_census.py: sysfs next to NVML / amd-smi). Usually a
    GPU installed after the agent container was created: a container only has the device nodes it was started with.
    Only when the agent sees some of a vendor's GPUs and nothing limits it to them (gpu_changes.census_state): an agent
    that sees none of them (a CPU-only agent on a GPU host) or was given some on purpose (`--gpus "device=…"`,
    NVIDIA_VISIBLE_DEVICES, ROCR_VISIBLE_DEVICES…) is left alone; the machine's page says so quietly. Agents before
    1.5.4 do not count, and nothing is said for them."""
    from forgeyard.machines import gpu_access
    from forgeyard.machines.gpu_changes import census_state

    for n in ctx.nodes:
        census = (n.runtime_info or {}).get("gpu_census")
        if n.status != "online" or not isinstance(census, dict):
            continue
        short = [(gpu_access.VENDOR_LABEL.get(v, v), c) for v, c in sorted((census.get("vendors") or {}).items())
                 if isinstance(c, dict) and census_state(c) == "hidden"]
        if not short:
            continue
        counts = " and ".join(f"{c['host']} {label} GPUs" for label, c in short)
        seen = " and ".join(str(c["visible"]) for _, c in short)
        missing = [m for _, c in short for m in c.get("missing") or []]
        at = f" Not seen: PCI {_names(missing, 4)}." if missing else ""
        if gpu_access.in_container(n):
            how = ("Its agent runs in a container, which has only the GPU devices it was created with. Re-create it "
                   "with the Update the agent command on the machine's page: it passes the host's GPU devices as they "
                   "are when you run it. Then, if the machine has a GPU access list, allow the new GPU there.")
        else:
            how = ("Restart the agent. If it still does not see them, check the driver on the host (nvidia-smi or "
                   "amd-smi lists every GPU it can use).")
        yield Finding("machine-gpus-hidden", "warning",
                      f"{n.name}: the host has {counts} but the agent sees {seen}",
                      f"The PCI bus of {n.name} lists {counts}; its agent sees {seen}, so Forgeyard cannot use the "
                      f"others.{at} {how} If a GPU is not one for Forgeyard (a desktop CPU's integrated graphics, "
                      "say), snooze this.",
                      f"/machines/{n.id}#update", "Update command", scope=f"machine:{n.id}",
                      pages=("machines", f"machine:{n.id}"))


def _recent_gpu_changes(n: Node, since: datetime) -> tuple[list[dict], list[dict]]:
    """The GPUs added and removed on this machine since `since` that are still added (reported now) or still removed
    (not reported now), oldest change first."""
    from forgeyard.machines import gpu_changes

    present = {a.key for a in n.accelerators}
    added: dict[str, dict] = {}
    removed: dict[str, dict] = {}
    for c in gpu_changes.changes(n):
        try:
            at = as_utc(datetime.fromisoformat(str(c.get("at"))))
        except ValueError:
            continue
        if at < since:
            continue
        # A GPU that comes back (or goes again) within the window cancels out: nothing to do about it.
        for g in c.get("added") or []:
            if isinstance(g, dict) and isinstance(g.get("key"), str):
                if removed.pop(g["key"], None) is None:
                    added[g["key"]] = dict(g, at=at)
        for g in c.get("removed") or []:
            if isinstance(g, dict) and isinstance(g.get("key"), str):
                if added.pop(g["key"], None) is None:
                    removed[g["key"]] = dict(g, at=at)
    return ([g for k, g in added.items() if k in present], [g for k, g in removed.items() if k not in present])


def _gpu_names(gpus: list[dict]) -> str:
    from forgeyard.machines.gpu_changes import label

    return ", ".join(label(g) for g in gpus)


@check
def machine_gpu_changes(ctx: Context) -> Iterator[Finding]:
    """1.5.4: a machine's GPUs changed in the last GPU_CHANGE_RECENT (machines/gpu_changes.py). A new GPU on a machine
    with a GPU access list is not used until an admin allows it (the list fails closed); without a list placement uses
    it at once. A removed GPU: the deployments whose replicas were placed on it keep that place until restarted."""
    from forgeyard.machines import gpu_changes

    since = ctx.now - GPU_CHANGE_RECENT
    for n in ctx.nodes:
        added, removed = _recent_gpu_changes(n, since)
        scope, pages = f"machine:{n.id}", ("machines", f"machine:{n.id}")
        if added and n.gpu_allowlist is not None:
            blocked = [g for g in added if g["key"] not in n.gpu_allowlist]
            if blocked:
                allow = ", ".join(f"GPU #{g.get('index')}" for g in blocked)
                yield Finding("machine-gpu-not-allowed", "warning",
                              f"New {'GPU' if len(blocked) == 1 else 'GPUs'} on {n.name}: allow {allow}?",
                              f"{_gpu_names(blocked)} appeared on {n.name} on {blocked[0]['at']:%Y-%m-%d}. This "
                              "machine has a GPU access list, so Forgeyard does not use a new GPU until it is allowed: "
                              f"Change GPU access on the machine's page and tick {allow}. If it is for other "
                              "programs, leave it out and snooze this.",
                              f"/machines/{n.id}#gpu-access", "Choose GPUs", scope=scope, pages=pages)
        elif added:
            yield Finding("machine-gpu-added", "info",
                          f"New {'GPU on ' + n.name + ' is' if len(added) == 1 else 'GPUs on ' + n.name + ' are'} in use",
                          f"{_gpu_names(added)} appeared on {n.name} on {added[0]['at']:%Y-%m-%d}. This machine has "
                          "no GPU access list, so placement already uses "
                          + ("it" if len(added) == 1 else "them")
                          + ". To keep Forgeyard off a GPU, choose which ones it may use.",
                          f"/machines/{n.id}#gpu-access", "Choose GPUs", scope=scope, pages=pages)
        if removed:
            live = gpu_changes.placed_on(ctx.db, n, {g["key"] for g in removed})
            held = sorted({d for g in removed for d in (g.get("deployments") or []) + live.get(g["key"], [])})
            placed = (f" Placed on {'it' if len(removed) == 1 else 'them'}: {_names(held, 6)}. Forgeyard does not move "
                      "a replica because its GPU is gone: it keeps its place there. Restart "
                      + ("that deployment" if len(held) == 1 else "those deployments")
                      + " (stop and start it, or edit it) and placement picks GPUs the machine has now, or another "
                      "machine." if held else " No deployment was placed on "
                      + ("it." if len(removed) == 1 else "them."))
            yield Finding("machine-gpu-removed", "warning",
                          f"{n.name} no longer reports " + ", ".join(f"GPU #{g.get('index')}" for g in removed),
                          f"{_gpu_names(removed)} on {n.name} stopped being reported on "
                          f"{removed[-1]['at']:%Y-%m-%d}: taken out, failed, or no longer visible to the agent "
                          f"(check it on the host with nvidia-smi or amd-smi).{placed}",
                          f"/machines/{n.id}", "Open the machine", scope=scope, pages=pages)


# 1.4.5 (#171): a loopback replica address works for a machine elsewhere only when its port range is forwarded to
# the server's host (the SSH tunnel of the guide); the advisor looks for that on ranges up to this size.
FORWARD_PROBE_MAX = 64


def _listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


listening: Callable[[str, int], bool] = _listening   # tests replace it


@check
def machine_loopback_address(ctx: Context) -> Iterator[Finding]:
    """1.4.5 (#171): a machine publishes its replicas on a loopback address (its connection to the server came
    through an SSH tunnel or a proxy on its own host), but it is not this server's host: the gateway would dial
    that address here, on the server's host. Which agent runs on the server's host is told by the connection itself
    (messages.same_end: its own end is the peer the server sees, so nothing sits between them); agents before 1.4.5
    do not report it and are not checked. Not raised when every port of a small range is forwarded here (something
    listens on each on the server's loopback), as the guide's tunnel does."""
    for n in ctx.nodes:
        info = n.runtime_info or {}
        address = info.get("address")
        if not isinstance(address, str) or not is_loopback_host(address) or info.get("same_host") is not False:
            continue
        ports = info.get("ports") or {}
        low, high = ports.get("low"), ports.get("high")
        if not (isinstance(low, int) and isinstance(high, int) and low <= high):
            low, high = 30000, 32767  # the agent's default range
        if high - low < FORWARD_PROBE_MAX:
            missing = [p for p in range(low, high + 1) if not listening(address, p)]
            if not missing:
                continue
            shown = ", ".join(str(p) for p in missing[:4]) + (" …" if len(missing) > 4 else "")
            forwarded = (f" Nothing listens on {_plural(len(missing), 'port')} of its range here ({shown}): forward "
                         "every port of the range back through the tunnel.")
        else:
            forwarded = (f" Its port range ({low}-{high}) is too large to forward: choose a small one "
                         "(FORGEYARD_PORT_RANGE) and forward each port back through the tunnel.")
        yield Finding("machine-loopback-address", "warning", f"{n.name} publishes its replicas on {address}",
                      f"{n.name}'s agent reached the server through a tunnel or a proxy, so it publishes its replicas "
                      f"on {address}, and the gateway dials that address on the server's own host, not on "
                      f"{n.name}." + forwarded + " Or give the agent an address the server can reach "
                      "(FORGEYARD_BIND_ADDRESS, --bind-address). If the server does reach it, snooze this.",
                      "/guide/en/troubleshooting#a-machine-publishes-its-replicas-on-a-loopback-address",
                      "What to do", scope=f"machine:{n.id}", pages=("machines",))


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
                          "agent again; if the machine is gone for good, revoke it.",
                          "/guide/en/troubleshooting#an-agent-shows-offline", "Why a machine is offline",
                          scope=f"machine:{n.id}", pages=("machines", "serving"))


# How long Setup health says what the 1.5.3 upgrade removed (the audit log keeps it for good).
APPS_REMOVED_DAYS = 30


@check
def apps_removed(ctx: Context) -> Iterator[Finding]:
    """V51 (1.5.3, #89; was apps-deprecated in 1.4): the upgrade removed apps (workloads that were not models) and
    the API keys' /x/ access, and wrote what it removed in the audit log. Said for a month after it did, so nobody
    learns it only from a program that stopped working."""
    from forgeyard import removed
    from forgeyard.db import AuditEvent

    since = ctx.now - timedelta(days=APPS_REMOVED_DAYS)
    rows = ctx.db.execute(select(AuditEvent.action, AuditEvent.target, AuditEvent.detail)
                          .where(AuditEvent.action.in_((removed.AUDIT_APP, removed.AUDIT_KEY)), AuditEvent.at >= since)
                          .order_by(AuditEvent.id)).all()
    apps = [t or "?" for a, t, _ in rows if a == removed.AUDIT_APP]
    # A revoked key lost nothing it could still use: in the audit log, not here.
    keys = [t or "?" for a, t, d in rows if a == removed.AUDIT_KEY and not (d or {}).get("revoked")]
    if not apps and not keys:
        return
    parts = []
    if apps:
        parts.append(f"stopped and removed {_plural(len(apps), 'app', 'apps')} ({_names(apps)})")
    if keys:
        parts.append(f"took /x/ access from {_plural(len(keys), 'API key', 'API keys')} ({_names(keys)})")
    yield Finding("apps-removed", "info",
                  f"The {removed.REMOVED_IN} upgrade removed {_plural(len(apps), 'app', 'apps')}" if apps else
                  f"The {removed.REMOVED_IN} upgrade removed /x/ access from {_plural(len(keys), 'API key', 'API keys')}",
                  f"Apps, the templates and the gateway's /x/ were removed in Forgeyard {removed.REMOVED_IN}. The upgrade "
                  f"{' and '.join(parts)}; the audit log has one entry for each, with what it was (actions "
                  f"{removed.AUDIT_APP} and {removed.AUDIT_KEY}). Deploy what you need as a model instead (a custom "
                  "engine runs a model Forgeyard has no engine for).",
                  "/audit", "Open the audit log", pages=("machines",))


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
                          "/serving", "Open Serving", scope=f"gpu:{n.id}:{a.index}", pages=("serving", "machines"))


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
                          f"/serving/{d.id}", "Open the deployment", scope=f"deployment:{d.id}",
                          pages=("serving", f"deployment:{d.id}"))


@check
def exclusive_could_share(ctx: Context) -> Iterator[Finding]:
    """V30 (1.3.1): a running exclusive replica reserves under half of a large card."""
    for d in ctx.deployments:
        w = d.workload
        if w is None or w.desired_state != "running" or (d.params or {}).get("share") != "exclusive":
            continue
        found = None
        for r in sorted(w.replicas, key=lambda r: r.ordinal):
            if r.node is None or not r.gpus or not r.vram or r.final or r.state not in ("healthy", "running"):
                continue
            for g in r.gpus:
                a = next((x for x in r.node.accelerators if x.key == g.get("key")), None)
                if a is not None and a.vram_total and a.vram_total >= LARGE_CARD and r.vram * 2 < a.vram_total:
                    found = (r, a)
                    break
            if found:
                break
        if found is None:
            continue
        r, a = found
        yield Finding("deployment-could-share", "info", f"{d.name} keeps a whole GPU for {_size(r.vram)}",
                      f"Replica {r.ordinal} of {d.name} reserves {_size(r.vram)} of the {_size(a.vram_total)} of GPU "
                      f"#{a.index} on {r.node.name}, and keeps the card to itself (exclusive). With Share VRAM the "
                      "rest of the card can hold other models; they then share its compute. Change it with Edit on "
                      "the deployment (its replicas restart).", f"/serving/{d.id}/edit",
                      "Change the deployment", scope=f"deployment:{d.id}", pages=("serving", f"deployment:{d.id}"))


def _gpu_held(d: Deployment) -> list[str]:
    """What a deployment's settled replicas hold on GPUs: "14.4 GiB on box GPU #4", one per replica."""
    out = []
    for r in sorted(d.workload.replicas, key=lambda r: r.ordinal):
        cards = [g for g in r.gpus or [] if g.get("vendor") != "cpu"]
        if r.final or r.node is None or not cards or not r.vram or r.state not in ("healthy", "running"):
            continue
        indexes = ", ".join(f"#{g.get('index')}" for g in cards)
        out.append(f"{_size(r.vram)} on {r.node.name} GPU {indexes}")
    return out


@check
def deployment_idle_awake(ctx: Context) -> Iterator[Finding]:
    """V52 (1.5.0, #37): a running deployment with sleep when idle off, no request for IDLE_HINT, holding GPU memory."""
    from datetime import timezone

    from forgeyard.workloads import sleep

    for d in ctx.deployments:
        w = d.workload
        if w is None or w.desired_state != "running" or sleep.setting(d.params).on:
            continue
        held = _gpu_held(d)
        if not held:
            continue
        # Quiet since its last request (Usage keeps days: the end of that day), what this server saw since it
        # started, or when it was deployed, started or changed, whichever is latest.
        times = [as_utc(d.created_at), as_utc(w.updated_at)]
        if (day := ctx.last_use_day.get(d.id)) is not None:
            times.append(datetime.fromisoformat(day).replace(tzinfo=timezone.utc) + timedelta(days=1))
        if (seen := sleep.idle.last(d.id)) is not None:
            times.append(as_utc(seen))
        quiet = ctx.now - max(t for t in times if t is not None)
        if quiet < IDLE_HINT:
            continue
        days = quiet.days
        yield Finding("deployment-idle-awake", "info", f"{d.name} had no requests for {days} days: let it sleep when idle?",
                      f"{d.name} had no requests for {days} days and holds {'; '.join(held)}. With Sleep when idle its "
                      "replicas stop after the idle time (30 minutes by default) and give that memory back; the model "
                      "stays listed, and the next request (or Wake now on its page) starts it again: the request waits "
                      "for it, or is served by a cloud tier meanwhile when its key may use one. Turn it on with Edit "
                      "on the deployment: nothing restarts.", f"/serving/{d.id}/edit", "Change the deployment",
                      scope=f"deployment:{d.id}", pages=("serving", f"deployment:{d.id}"))


@check
def deployment_wakes_often(ctx: Context) -> Iterator[Finding]:
    """V53 (1.5.0, #37): sleep when idle on, woken more than WAKES_OFTEN times in a day, slower than SLOW_START."""
    from forgeyard.workloads import sleep, starts

    for d in ctx.deployments:
        w = d.workload
        s = sleep.setting(d.params)
        if w is None or not s.on:
            continue
        n = sleep.wakes(ctx.db, w, ctx.now)
        if n <= WAKES_OFTEN:
            continue
        last = starts.last_start(ctx.db, w.id)
        if last is None or float(last["seconds"]) <= SLOW_START:
            continue
        took = round(float(last["seconds"]))
        yield Finding("deployment-wakes-often", "info", f"{d.name} woke {n} times in a day: raise its idle time",
                      f"{d.name} woke {n} times in the last 24 hours, and its last start took {took} s: each time, the "
                      f"first request waited about that long. It sleeps after {s.idle_minutes} min without a request; "
                      "a longer idle time (Edit on the deployment) keeps it awake between the requests it gets, at the "
                      "cost of holding its memory longer.", f"/serving/{d.id}/edit", "Change the deployment",
                      scope=f"deployment:{d.id}", pages=("serving", f"deployment:{d.id}"))


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
                          f"/serving/{d.id}", "Open the deployment", scope=f"deployment:{d.id}",
                          pages=("serving", f"deployment:{d.id}"))


REFUSED = re.compile(r"access refused|HTTP 40[13]\b", re.I)


@check
def gated_without_token(ctx: Context) -> Iterator[Finding]:
    """V33: a deployment of a gated Hugging Face model with no token of its own while the server has
    none either. An error once a replica's download was refused, information before."""
    from forgeyard.models import hf_token

    if not ctx.deployments or hf_token.info(ctx.db)["set"]:
        return
    for d in ctx.deployments:
        m = d.model
        if m is None or m.kind != "hf" or not (m.meta or {}).get("gated") or d.hf_token:
            continue
        refused = any(r.reason and REFUSED.search(r.reason) for r in d.workload.replicas if not r.final)
        yield Finding("gated-model-no-token", "error" if refused else "info",
                      f"{d.name} needs a Hugging Face token" if refused else f"{d.name} may not download: no Hugging Face token",
                      f"{m.name} is gated on Hugging Face, and neither the server nor this deployment has a token"
                      + (", so its download was refused." if refused else ": its download will be refused.")
                      + " Set the server's token under Models → Sources (after accepting the model's licence on "
                      "Hugging Face with that account), or give the deployment its own.",
                      "/models?tab=sources", "Set the token", scope=f"deployment:{d.id}",
                      pages=("serving", f"deployment:{d.id}"))


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
                          "refresh them from OpenRouter.", f"/cloud/providers/{p.id}", "Set prices", scope=f"provider-model:{pm.id}",
                          pages=("providers", f"provider:{p.id}"))
        else:
            yield Finding("cloud-model-cost-unknown", "info", f"The cost of {pm.upstream_model} is unknown",
                          f"{pm.upstream_model} ({p.name}) has no price, so Usage cannot say what it costs, and it will "
                          "be refused as soon as a budget is set. Set its prices, or refresh them from OpenRouter.",
                          f"/cloud/providers/{p.id}", "Set prices", scope=f"provider-model:{pm.id}",
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
                          f"/cloud/providers/{p.id}", "Add the key", scope=f"provider:{p.id}", pages=pages)
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
                              "tick Outside your network (cloud).", f"/cloud/providers/{p.id}", "Open the provider",
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
                      "your network.", "/keys", "Open Cloud policy", pages=("keys",))
    elif not any(gkeys.effective_cloud(k, k.user, c["max"]) != "off" for k in ctx.active_keys):
        names = sorted({r.served_name for r in ctx.routes if ctx.usable_cloud_targets(r)})
        yield Finding("cloud-unreachable", "info", "Cloud routes exist, but no key may use them",
                      f"{_names(names)} {'has' if len(names) == 1 else 'have'} cloud targets, but every key's cloud "
                      "setting is Never and new keys get Never too, so only the playground reaches them. Give a key "
                      "Only to spill or Allowed, or change the cloud policy.", "/keys", "Open API keys", pages=("keys",))


def route_place(r: GatewayRoute) -> tuple[str, tuple[str, ...], str]:
    """Where the console shows a route (1.4, #81): a deployment's own route is its routing and fallback
    (named "cloud fallback" before 1.5.4), on the deployment's page; a name an admin made is on Cloud ->
    Cloud models."""
    if r.deployment_id is not None:
        return (f"/serving/{r.deployment_id}#routing",
                ("serving", f"deployment:{r.deployment_id}", f"route:{r.id}"), "Open its routing")
    return f"/cloud/models/{r.id}", ("cloud-models", f"route:{r.id}"), "Open the model name"


@check
def routes_setup(ctx: Context) -> Iterator[Finding]:
    """V41: a cloud tier before the first local one. V42: a cloud tier or spill rule that is never used.
    V43: no usable target. V44: a mixed route whose local deployments are stopped."""
    for r in ctx.routes:
        url, pages, label = route_place(r)
        usable = [t for t in r.targets if target_usable(t)]
        if not usable:
            yield Finding("route-no-target", "error", f"{r.served_name} has no target that can answer",
                          f"Every target of {r.served_name} is disabled (or its provider is), so it answers 503 "
                          "no_target.", url, label, scope=f"route:{r.id}", pages=pages)
            continue
        local = [t for t in usable if is_local(t)]
        cloud = [t for t in usable if is_cloud(t)]
        if local and cloud and min(t.tier for t in cloud) < min(t.tier for t in local):
            yield Finding("route-cloud-before-local", "warning", f"{r.served_name} tries the cloud before local",
                          f"A cloud tier of {r.served_name} comes before its first local tier: keys set to Allowed go "
                          "to the cloud first even when the local replicas are idle, and keys set to Only to spill "
                          "skip that tier. Move the local tier first unless that is what you want.", url, label,
                          scope=f"route:{r.id}", pages=pages)
        if r.local_only and cloud:
            yield Finding("route-cloud-never-used", "info", f"{r.served_name}'s cloud tier is never used",
                          f"{r.served_name} is Local only, so its cloud targets never get a request.", url, label,
                          scope=f"route:{r.id}", pages=pages)
        elif local and (r.spill_on_long_prompt or (r.wait_ms or 0) > 0) \
                and not any(t.tier > min(x.tier for x in local) for t in usable):
            rule = "Spill a prompt that is too long" if r.spill_on_long_prompt else "Wait for a free local slot"
            yield Finding("route-cloud-never-used", "info", f"{r.served_name}: {rule} has no effect",
                          f"{r.served_name} has no tier after its local one, so there is nothing to spill to.",
                          url, label, scope=f"route:{r.id}", pages=pages)
        if local and cloud:
            deps = [t.deployment for t in local if t.kind == "local" and t.deployment is not None]
            if deps and all(d.workload is not None and d.workload.desired_state == "stopped" for d in deps):
                yield Finding("route-local-stopped", "warning", f"{r.served_name}'s local deployment is stopped",
                              f"{_names(d.name for d in deps)} {'is' if len(deps) == 1 else 'are'} stopped, so every "
                              f"request to {r.served_name} from a key that may use the cloud goes to the cloud.",
                              url, label, scope=f"route:{r.id}", pages=pages)


@check
def budgets_zero(ctx: Context) -> Iterator[Finding]:
    """V45: a budget of $0 blocks every cloud request (empty means no budget)."""
    zero = ([("key", k.id, f"the key {k.name}", "/keys", ("keys",)) for k in ctx.active_keys if k.budget_micros == 0]
            + [("person", u.id, u.username, "/usage?tab=cost", ()) for u in ctx.users if u.budget_micros == 0]
            + [("provider", p.id, p.name, f"/cloud/providers/{p.id}", ("providers", f"provider:{p.id}"))
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
                      "/cloud", "Refresh prices", pages=("providers",))


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
