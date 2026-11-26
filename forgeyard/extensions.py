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
| `placement_check(fn(db, ctx) -> None or (code, text))` | scheduler.preview() (deploy, edit, trials, model pages), scheduler.place_replica() (start, wake, restart, move, new replicas), autoscale's room check | allowed |
| `gateway_check(fn(caller, route) -> None or (status, code, text))` | the gateway, before the key's rate limits and before routing (forward(), video jobs) | allowed |
| `usage_observer(fn(rows))` | the usage recorder, after each write: the counts it added | — |
| `findings(fn(advisor_ctx) -> findings)` | the setup advisor | none |
| `features(names)` | GET /api/info `features`: which pages the console shows | [] |
| `audit_actions(prefixes)` | GET /api/info `audit_actions`: the audit page's action filter | [] |
| `switch_policy(fn(db, deployment, recipe) -> None or (code, text))` | changing a deployment's engine version | allowed |

A gateway check runs on the event loop for every request: it must answer from memory and never block (no database,
no network). A hook that raises is logged and treated as allowing (a placement or request goes on): an extension's
bug must not stop models from serving.

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

    def placement_refused(self, db, ctx) -> Optional[tuple[str, str]]:
        return self._first(self.placement_checks, "placement check", db, ctx)

    def gateway_refused(self, caller, route) -> Optional[tuple[int, str, str]]:
        if not self.gateway_checks:
            return None
        return self._first(self.gateway_checks, "gateway check", caller, route)

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
