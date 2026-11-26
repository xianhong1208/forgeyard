"""A sample extension (1.6.1, #190) that uses every hook of forgeyard/extensions.py, so the interface is checked in
public CI: tests/test_extensions.py loads it through a real entry point object and asserts what callers see.

What it refuses is driven by the module's sets below, which the tests fill: a deployment whose served name is in
REFUSE_PLACEMENT is refused (code `sample_quota`), one in REFUSE_ADDING only when the placement adds replicas (as a
quota would: running replicas placed again keep serving), machines named in HIDDEN are left out of placement, and a
request with an API key named in REFUSE_KEYS gets 402 `sample_quota_exhausted` (with the fields of EXTRA_FIELDS in its
error body, when set: the 4th element a gateway check may answer). ASKED and FILTERED record what the
placement check and the machine filter were given (1.6.2)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from forgeyard import __version__
from forgeyard.advisor import Finding

REFUSE_PLACEMENT: set[str] = set()
REFUSE_ADDING: set[str] = set()
HIDDEN: set[str] = set()
REFUSE_KEYS: set[str] = set()
EXTRA_FIELDS: dict = {}            # the gateway refusal's extra error fields (empty: it answers three elements)
RAISE: set[str] = set()            # hooks that raise, to check that a broken extension does not stop serving
OBSERVED: list[dict] = []          # what the usage observer was given
ASKED: list[dict] = []             # what each placement check was asked: the deployment's team and the Ask
FILTERED: list[dict] = []          # what the machine filter was given

router = APIRouter(prefix="/api/ext/sample", tags=["sample extension"])


@router.get("/ping")
def ping():
    return {"ok": True, "from": "sample"}


def _served(ctx) -> str:
    return getattr(ctx.deployment, "served_name", "") or ""


def placement_check(db, ctx, ask):
    ASKED.append({"served": _served(ctx), "team_id": ctx.team_id, "replicas": ctx.replicas, "adding": ask.adding,
                  "release": sorted(ask.release), "replacing": ask.replacing})
    if "placement_check" in RAISE:
        raise RuntimeError("sample placement check is broken")
    if _served(ctx) in REFUSE_PLACEMENT or (_served(ctx) in REFUSE_ADDING and ask.adding > 0):
        return "sample_quota", f"The sample extension's quota allows no more replicas of {_served(ctx)}."
    return None


def placement_filter(db, ctx, nodes):
    FILTERED.append({"served": _served(ctx), "team_id": ctx.team_id})
    return [n for n in nodes if n.name not in HIDDEN]


def gateway_check(caller, route):
    if "gateway_check" in RAISE:
        raise RuntimeError("sample gateway check is broken")
    if caller.key_name in REFUSE_KEYS:
        why = 402, "sample_quota_exhausted", f"The sample extension's token quota for {caller.key_name} is used up."
        return (*why, dict(EXTRA_FIELDS)) if EXTRA_FIELDS else why
    return None


def usage_observer(rows):
    OBSERVED.extend(rows)


def findings(ctx):
    yield Finding("sample-extension", "info", "The sample extension is loaded",
                  "It refuses what the tests tell it to.", "/start", scope="server")


def switch_policy(db, deployment, recipe):
    if deployment.served_name in REFUSE_PLACEMENT:
        return "not_tried", f"{recipe.name} has not passed a trial."
    return None


class SampleExtension:
    name = "sample"
    version = "0.1.0"
    requires = __version__
    edition = "enterprise"

    def register(self, reg) -> None:
        reg.router(router)
        reg.migrations(Path(__file__).with_name("migrations"))
        reg.placement_filter(placement_filter)
        reg.placement_check(placement_check)
        reg.gateway_check(gateway_check)
        reg.usage_observer(usage_observer)
        reg.findings(findings)
        reg.features(["sample"])
        reg.audit_actions(["sample."])
        reg.switch_policy(switch_policy)


def extension() -> SampleExtension:
    """The entry point's object (`tests.fixtures.ext_sample:extension`)."""
    return SampleExtension()


def reset() -> None:
    for s in (REFUSE_PLACEMENT, REFUSE_ADDING, HIDDEN, REFUSE_KEYS, RAISE):
        s.clear()
    for x in (OBSERVED, ASKED, FILTERED, EXTRA_FIELDS):
        x.clear()
