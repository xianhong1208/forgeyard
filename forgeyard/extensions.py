"""Extensions (1.6.1, #190): code distributed separately from this repository (the enterprise edition's package)
that adds to the server through a small, internal interface.

This is not a public plugin API. It is versioned to the exact release: an extension names the one Forgeyard version
it was built for (`requires`), and the server refuses to start with any other, so a hook may change in any release.

**Loading.** At startup (create_app) every entry point in the group `forgeyard.extensions` is loaded, by name. Its
object, or the object its callable returns, has `name`, `version`, `requires`, optionally `edition` ("enterprise"),
and `register(reg)`, which calls the registration methods below. The server logs `extension loaded: <name>
<version>`; GET /api/info names the edition and the features, and Setup health lists the extensions (admins).

**Hooks.** Each has a community default, used when no extension registered one, and nothing else changes then:

| Registration | Called by | Without an extension |
|---|---|---|
| `router(APIRouter)` | create_app, after the community routers | no route |
| `migrations(path)` | migrate.upgrade(): the path joins Alembic's version locations; `upgrade heads` | `heads` = `head` |
| `placement_filter(fn(db, ctx, nodes) -> nodes)` | scheduler.evaluate(): machines left out are refused with a reason | every machine |
| `placement_check(fn(db, ctx, ask) -> None or (code, text))` | scheduler.preview() (deploy, edit, trials, model pages), scheduler.place_replica() (start, wake, restart, move, new replicas), autoscale's room check | allowed |
| `gateway_check(fn(caller, route) -> None or (status, code, text[, extra]))` | the gateway, before the key's rate limits and before routing (forward(), video jobs) | allowed |
| `usage_observer(fn(rows))` | the usage recorder, after each write: the counts it added | — |
| `findings(fn(advisor_ctx) -> findings)` | the setup advisor | none |
| `features(names)` | GET /api/info `features`: which pages the console shows | [] |
| `audit_actions(prefixes)` | GET /api/info `audit_actions`: the audit page's action filter | [] |
| `switch_policy(fn(db, deployment, recipe) -> None or (code, text))` | changing a deployment's engine version | allowed |

A gateway check's optional 4th element (1.6.2), a dict, adds fields to the error body next to `message`, `type`,
`param` and `code` (as a budget's refusal carries its numbers), e.g. `{"resets_at": "2026-12-01T00:00:00Z"}`; it
cannot replace those four. A 3-tuple adds none.

A gateway check runs on the event loop for every request: it must answer from memory and never block (no database,
no network). A hook that raises is logged and treated as allowing (a placement or request goes on): an extension's
bug must not stop models from serving.

**What a placement check is asked** (1.6.2, #190). `ctx` (forgeyard.models.catalog.Context) is the deployment as it
would run: `ctx.team_id` is its team (the one a deploy or an estimate names; in an edit's preview the new team when
the edit changes it), `ctx.replicas` how many replicas it would have, `ctx.deployment` the row (before it exists, an
object with `served_name` only). `ask` (`Ask` below) is what this placement adds:

| Call | `ask.adding` | `ask.release` | `ask.replacing` |
|---|---|---|---|
| preview of a deploy, an estimate, a trial, a model page | ctx.replicas | empty | False |
| preview of an edit | ctx.replicas minus the replicas it releases (at least 0) | the deployment's replica ids | True |
| place_replica, a new replica (deploy, start, wake, a replica an edit or autoscale added) | 1 | empty | False |
| place_replica, a replica placed again (an edit's rolling restart, offline move, failed download, out of ports, its machine removed) | 0 | empty | True |
| autoscale's room check for one more replica | 1 | empty | False |

So "refuse when `ask.adding` > 0" never stops a running replica from serving: a replica placed again adds nothing.
Which replicas are placed again is kept in memory (like the machine a restarted replica ran on): after a server
restart, a replica still waiting to be placed again is asked as a new one.

The machine filter gets the same `ctx` (with `team_id`).

**GPU hours** (1.6.1) are read, not observed: the usage observer gets the request rows only. An extension that
enforces GPU-hour quotas (at placement, through `placement_check`) reads this month's GPU time of a team with
`forgeyard.workloads.gpu_hours.month_total(db, team_id=…)["gpu_ms"]`, from the database, at most one write (5 s) behind.

Community code never checks a licence; it only asks the registry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from forgeyard import __version__

log = logging.getLogger("forgeyard")

GROUP = "forgeyard.extensions"
COMMUNITY = "community"


@dataclass(frozen=True)
class Ask:
    """What a placement check is asked (1.6.2, #190): how many replicas this placement adds to what the deployment
    holds now, which of its replicas stop holding for it (an edit's preview), and whether existing replicas are placed
    again rather than started. See the table in this module's docstring."""
    adding: int
    release: frozenset[int] = frozenset()
    replacing: bool = False


# The fields of a gateway error body that a gateway check's extra fields cannot replace (forgeyard/gateway/errors.py).
GATEWAY_ERROR_FIELDS = ("message", "type", "param", "code")


class ExtensionError(RuntimeError):
    """An extension that cannot be loaded: the server does not start."""


@dataclass
class Loaded:
    name: str
    version: str


@dataclass
class Registry:
    loaded: list[Loaded] = field(default_factory=list)
    edition: str = COMMUNITY
    routers: list = field(default_factory=list)
    migration_paths: list[str] = field(default_factory=list)
    placement_filters: list[Callable] = field(default_factory=list)
    placement_checks: list[Callable] = field(default_factory=list)
    gateway_checks: list[Callable] = field(default_factory=list)
    usage_observers: list[Callable] = field(default_factory=list)
    finding_checks: list[Callable] = field(default_factory=list)
    feature_names: list[str] = field(default_factory=list)
    audit_prefixes: list[str] = field(default_factory=list)
    switch_policies: list[Callable] = field(default_factory=list)

    def reset(self) -> None:
        self.__init__()

    # ---------------------------------------------------------------- what an extension registers
    def router(self, router) -> None:
        self.routers.append(router)

    def migrations(self, path) -> None:
        self.migration_paths.append(str(Path(path)))

    def placement_filter(self, fn: Callable) -> None:
        self.placement_filters.append(fn)

    def placement_check(self, fn: Callable) -> None:
        self.placement_checks.append(fn)

    def gateway_check(self, fn: Callable) -> None:
        self.gateway_checks.append(fn)

    def usage_observer(self, fn: Callable) -> None:
        self.usage_observers.append(fn)

    def findings(self, fn: Callable) -> None:
        self.finding_checks.append(fn)

    def features(self, names: Iterable[str]) -> None:
        self.feature_names.extend(n for n in names if n not in self.feature_names)

    def audit_actions(self, prefixes: Iterable[str]) -> None:
        self.audit_prefixes.extend(p for p in prefixes if p not in self.audit_prefixes)

    def switch_policy(self, fn: Callable) -> None:
        self.switch_policies.append(fn)

    # ---------------------------------------------------------------- what community code asks
    def allowed_nodes(self, db, ctx, nodes: list) -> list:
        for fn in self.placement_filters:
            try:
                nodes = list(fn(db, ctx, nodes))
            except Exception:
                log.exception("Extension placement filter %s failed; every machine stays", _name(fn))
        return nodes

    def placement_refused(self, db, ctx, ask: Ask) -> Optional[tuple[str, str]]:
        return self._first(self.placement_checks, "placement check", db, ctx, ask)

    def gateway_refused(self, caller, route) -> Optional[tuple[int, str, str, dict]]:
        """(status, code, text, extra fields of the error body) when a gateway check refuses the request, else None.
        A check may answer three elements (no extra fields) or four (1.6.2); the error's own fields stay its own."""
        if not self.gateway_checks:
            return None
        why = self._first(self.gateway_checks, "gateway check", caller, route)
        if why is None:
            return None
        extra = why[3] if len(why) > 3 and isinstance(why[3], dict) else {}
        return why[0], why[1], why[2], {k: v for k, v in extra.items() if k not in GATEWAY_ERROR_FIELDS}

    def switch_refused(self, db, deployment, recipe) -> Optional[tuple[str, str]]:
        return self._first(self.switch_policies, "engine switch policy", db, deployment, recipe)

    def usage_written(self, rows: list[dict]) -> None:
        for fn in self.usage_observers:
            try:
                fn(rows)
            except Exception:
                log.exception("Extension usage observer %s failed", _name(fn))

    def _first(self, fns: list[Callable], what: str, *args) -> Optional[tuple]:
        for fn in fns:
            try:
                why = fn(*args)
            except Exception:
                log.exception("Extension %s %s failed; allowing", what, _name(fn))
                continue
            if why:
                return tuple(why)
        return None


def _name(fn: Callable) -> str:
    return f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', repr(fn))}"


registry = Registry()


def discover() -> list[EntryPoint]:
    """The installed entry points of the group, by name."""
    return sorted(entry_points(group=GROUP), key=lambda ep: ep.name)


def _describe(ep: EntryPoint, obj: Any) -> tuple[str, str, str]:
    try:
        obj.register
        return str(obj.name), str(obj.version), str(obj.requires)
    except AttributeError as e:
        raise ExtensionError(f"The extension {ep.name} ({ep.value}) has no {e.name!r}: it needs name, version, "
                             "requires and register(reg).") from None


def load(version: str = __version__) -> Registry:
    """Load every installed extension into the registry (emptied first). Raises ExtensionError when one was built
    for another Forgeyard version, or cannot be loaded."""
    registry.reset()
    for ep in discover():
        try:
            obj = ep.load()
        except Exception as e:
            raise ExtensionError(f"The extension {ep.name} ({ep.value}) could not be loaded: {e}") from e
        ext = obj() if callable(obj) and not hasattr(obj, "register") else obj
        name, ext_version, requires = _describe(ep, ext)
        if requires != version:
            raise ExtensionError(
                f"The extension {name} {ext_version} was built for Forgeyard {requires}, and this server is Forgeyard "
                f"{version}. Install the {name} release built for {version}, or remove it.")
        ext.register(registry)
        if getattr(ext, "edition", None):
            registry.edition = str(ext.edition)
        registry.loaded.append(Loaded(name, ext_version))
        log.info("extension loaded: %s %s", name, ext_version)
    return registry
