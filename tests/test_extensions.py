"""The extension interface (1.6.1, #190; forgeyard/extensions.py), through the sample extension in
tests/fixtures/ext_sample loaded from a real entry point object: without an extension nothing changes (the community
edition); with one, its route answers, its migration branch runs, a placement it refuses is refused with its code at
deploy, edit and engine trials and waits with its reason at start, wake and scale-up, machines it leaves out are
not used, a gateway request it refuses gets its status and code before the rate limits (chat and video), its usage
observer gets what was written, its finding shows in Setup health, and /api/info names the edition and features. One
built for another version stops the start; a hook that raises allows (FY-EXT-*)."""

from __future__ import annotations

import logging
from datetime import timedelta
from importlib.metadata import EntryPoint
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, text

from forgeyard import __version__, extensions
from forgeyard.db import get_engine, new_session, utcnow
from forgeyard.gateway.usage import recorder
from forgeyard.machines.hub import hub
from forgeyard.workloads import placement
from tests.conftest import SETUP_TOKEN, OWNER, make_client
from tests.fixtures import ext_sample
from tests.gateway_helpers import api_client, new_key
from tests.test_autoscale import Run, auto, no_background_scaler  # noqa: F401  (the fixture)
from tests.test_deploy_edit import CPU_BODY, cpu_row, ready, rows, set_replica, sweep
from tests.test_gateway import chat, err, gw  # noqa: F401  (the fixture)
from tests.test_placement_fetch import DNS, report, set_node
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.test_video_generation import make, vid  # noqa: F401  (the fixture)
from tests.test_wake import _api, sleepy
from tests.workload_helpers import make_node

GIB = 1024**3
SAMPLE = EntryPoint("sample", "tests.fixtures.ext_sample:extension", extensions.GROUP)
COMMUNITY_HEAD = "0023_gpu_usage"
COMMUNITY_TABLE = "gpu_usage_daily"   # made by the community head


@pytest.fixture()
def sample(monkeypatch):
    """The sample extension is installed (list it before the client fixtures: the app loads it when made)."""
    monkeypatch.setattr(extensions, "discover", lambda: [SAMPLE])
    ext_sample.reset()
    yield ext_sample
    ext_sample.reset()


@pytest.fixture()
def no_extension(monkeypatch):
    """Nothing installed, whatever this environment has (an extension's own CI runs this suite with it installed;
    list it before the client fixtures)."""
    monkeypatch.setattr(extensions, "discover", lambda: [])


@pytest.fixture()
def cpus(owner):
    """Two CPU machines; cpu-a has much more free RAM, so binpack prefers cpu-b."""
    seed_catalog()
    a = make_node("cpu-a", features=MODELS)
    b = make_node("cpu-b", features=MODELS)
    cpu_row(a, vram_free=100 * GIB)
    cpu_row(b, vram_free=20 * GIB)
    return owner, a, b


def heads() -> set[str]:
    with get_engine().connect() as conn:
        return {r[0] for r in conn.execute(text("SELECT version_num FROM alembic_version"))}


def events(c, dep_id: int) -> list[str]:
    return [e["message"] for e in c.get(f"/api/deployments/{dep_id}").json()["events"]]


# ------------------------------------------------------------------ without an extension
def test_fy_ext_001_without_an_extension_the_community_edition(no_extension, owner):
    """FY-EXT-001: nothing installed: /api/info says community with no features (signed in only), Setup health lists
    no extension, no extension route, one migration head (the community's)."""
    assert owner.get("/api/info").json() == {"version": __version__, "edition": "community", "features": [],
                                             "audit_actions": []}
    assert owner.get("/api/advisor").json()["extensions"] == []
    r = owner.get("/api/ext/sample/ping")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"
    assert heads() == {COMMUNITY_HEAD}
    assert not inspect(get_engine()).has_table("ext_sample_notes")
    with make_client() as anon:
        assert anon.get("/api/info").status_code == 401


# ------------------------------------------------------------------ loading
def test_fy_ext_002_loaded_from_its_entry_point(sample, caplog):
    """FY-EXT-002: the entry point's callable gives the extension; the log names it; its route answers after the
    community's; /api/info names the edition, its features and audit prefixes; Setup health lists it with its
    finding."""
    with caplog.at_level(logging.INFO, logger="forgeyard"):
        with make_client() as c:
            assert "extension loaded: sample 0.1.0" in caplog.messages
            r = c.post("/api/auth/setup", json={"setup_token": SETUP_TOKEN, "username": OWNER[0],
                                                "password": OWNER[1]})
            assert r.status_code == 200, r.text
            assert c.get("/api/ext/sample/ping").json() == {"ok": True, "from": "sample"}
            assert c.get("/api/info").json() == {"version": __version__, "edition": "enterprise",
                                                 "features": ["sample"], "audit_actions": ["sample."]}
            report = c.get("/api/advisor").json()
            assert report["extensions"] == [{"name": "sample", "version": "0.1.0"}]
            assert [f for f in report["findings"] if f["id"] == "sample-extension"][0]["severity"] == "info"


class _Old(ext_sample.SampleExtension):
    requires = "1.0.0"


class _Broken:   # no register()
    name = "broken"
    version = "0.1.0"
    requires = __version__


def test_fy_ext_003_built_for_another_version_stops_the_start(monkeypatch, caplog):
    """FY-EXT-003: an extension built for another Forgeyard version, or without the interface's attributes, stops the
    start with a clear message (the CLI exits 2 before serving)."""
    from forgeyard import cli

    monkeypatch.setattr(ext_sample, "old", lambda: _Old(), raising=False)
    monkeypatch.setattr(extensions, "discover",
                        lambda: [EntryPoint("sample", "tests.fixtures.ext_sample:old", extensions.GROUP)])
    with pytest.raises(extensions.ExtensionError) as e:
        make_client()
    assert str(e.value) == (f"The extension sample 0.1.0 was built for Forgeyard 1.0.0, and this server is Forgeyard "
                            f"{__version__}. Install the sample release built for {__version__}, or remove it.")
    monkeypatch.setattr("uvicorn.Server.run", lambda self: pytest.fail("the server must not start"))
    with caplog.at_level(logging.ERROR, logger="forgeyard"):
        assert cli._serve() == 2
    assert str(e.value) in caplog.messages
    monkeypatch.setattr(ext_sample, "broken", _Broken(), raising=False)
    monkeypatch.setattr(extensions, "discover",
                        lambda: [EntryPoint("broken", "tests.fixtures.ext_sample:broken", extensions.GROUP)])
    with pytest.raises(extensions.ExtensionError, match="has no 'register'"):
        make_client()


def test_fy_ext_004_its_migrations_are_a_branch_of_their_own(sample):
    """FY-EXT-004: its migration ran at startup (its table exists) after the community migration it depends on
    (Alembic keeps the one head that implies both); down to base and up again ("upgrade heads") brings both back."""
    from alembic import command

    from forgeyard.migrate import alembic_config, upgrade

    with make_client():
        assert heads() == {"ext_sample_0001"}
        assert inspect(get_engine()).has_table("ext_sample_notes") and inspect(get_engine()).has_table(COMMUNITY_TABLE)
        cfg = alembic_config()
        with get_engine().begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "base")
        assert not inspect(get_engine()).has_table("ext_sample_notes")
        assert not inspect(get_engine()).has_table(COMMUNITY_TABLE)
        upgrade()
        assert heads() == {"ext_sample_0001"}
        assert inspect(get_engine()).has_table("ext_sample_notes") and inspect(get_engine()).has_table(COMMUNITY_TABLE)


# ------------------------------------------------------------------ placement
def test_fy_ext_005_a_refused_placement_at_deploy_estimate_and_edit(sample, cpus):
    """FY-EXT-005: a deployment the extension refuses: deploy answers 409 with the extension's code and text (not
    does_not_fit), the estimate does not fit and says so, a replica added by an edit is refused the same way; other
    deployments are not touched."""
    c, a, b = cpus
    sample.REFUSE_PLACEMENT.add("chat")
    r = c.post("/api/deployments", json={"name": "chat", **CPU_BODY})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "sample_quota"
    assert r.json()["detail"]["message"] == "The sample extension's quota allows no more replicas of chat."
    est = c.post("/api/deployments/estimate", json={"name": "chat", **CPU_BODY}).json()["placement"]
    assert not est["fits"] and est["refused"] == {"code": "sample_quota", "message": r.json()["detail"]["message"]}
    other = deploy(c, "other", **CPU_BODY)["deployment"]
    assert rows("other")[0].node_id == b
    sample.REFUSE_PLACEMENT.add("other")
    r = c.patch(f"/api/deployments/{other['id']}", json={"replicas": 2})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "sample_quota", r.text
    assert len(rows("other")) == 1


def test_fy_ext_006_start_and_wake_wait_with_its_reason(sample, cpus):
    """FY-EXT-006: a stopped deployment started while the extension refuses it: its replica waits, "Cannot be placed:
    <the extension's text>", with an event; an asleep one woken by a request gets 503 cannot_wake with that text at
    once."""
    c, a, b = cpus
    d = deploy(c, "chat", **CPU_BODY)["deployment"]
    assert c.post(f"/api/deployments/{d['id']}/stop").status_code == 200
    set_replica("chat", 0, state="stopped")
    sample.REFUSE_PLACEMENT.add("chat")
    assert c.post(f"/api/deployments/{d['id']}/start").status_code == 200
    why = "Cannot be placed: The sample extension's quota allows no more replicas of chat."
    [r] = rows("chat")
    assert r.node_id is None and r.reason == why
    assert why in events(c, d["id"])
    sample.REFUSE_PLACEMENT.clear()
    sleepy(c, "nap")
    sample.REFUSE_PLACEMENT.add("nap")
    r = _api(c).post("/v1/chat/completions", json={"model": "nap", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503 and err(r)["code"] == "cannot_wake", r.text
    assert "The sample extension's quota allows no more replicas of nap." in err(r)["message"]


def test_fy_ext_007_no_scale_up_it_refuses(sample, cpus, monkeypatch):
    """FY-EXT-007: automatic replicas: one more is not added while the extension refuses it (the reason is the
    extension's); once it allows, it is."""
    c, a, b = cpus
    run = Run(monkeypatch)
    auto(c)
    sample.REFUSE_PLACEMENT.add("chat")
    run.load("chat", 2)
    done = run.run(20)
    assert done == ["Wants 2 replicas, room for 1: The sample extension's quota allows no more replicas of chat."], done
    assert len(rows("chat")) == 1
    sample.REFUSE_PLACEMENT.clear()
    done = run.run(2)
    assert done and done[0].startswith("Scaled up to 2"), done
    assert len(rows("chat")) == 2


def test_fy_ext_008_machines_it_leaves_out(sample, cpus):
    """FY-EXT-008: a machine the extension's filter leaves out is never chosen (binpack would pick cpu-b) and, when
    the deployment is pinned to it, says why it cannot."""
    c, a, b = cpus
    sample.HIDDEN.add("cpu-b")
    deploy(c, "chat", **CPU_BODY)
    assert rows("chat")[0].node_id == a
    r = c.post("/api/deployments", json={"name": "pinned", "machine_id": b, **CPU_BODY})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "does_not_fit", r.text
    [m] = r.json()["detail"]["machines"]
    assert m["name"] == "cpu-b" and not m["fits"] and m["reason"] == "not one of the machines this deployment may use"


# ------------------------------------------------------------------ the gateway
def test_fy_ext_009_a_refused_request_gets_its_status_and_code(sample, gw):  # noqa: F811
    """FY-EXT-009: a key the extension refuses gets its status and code in OpenAI's shape and reaches no engine,
    before the key's rate limit (the refusals take no slot of its 1 request a minute); other keys are served; the
    usage observer gets what each write added, the refusals counted as errors."""
    blocked = new_key(gw.owner, "blocked", rpm=1)["key"]
    sample.REFUSE_KEYS.add("blocked")
    api = api_client(gw.owner, blocked)
    for _ in range(2):
        r = chat(api)
        assert r.status_code == 402, r.text
        assert err(r)["code"] == "sample_quota_exhausted"
        assert err(r)["message"] == "The sample extension's token quota for blocked is used up."
    assert gw.e1.posts() == [] and gw.e2.posts() == []
    assert chat(gw.api).status_code == 200
    sample.REFUSE_KEYS.clear()
    assert chat(api).status_code == 200   # its one request this minute is still free
    recorder.flush()
    assert sum(r["requests"] for r in sample.OBSERVED) == 4
    assert sum(r["errors"] for r in sample.OBSERVED) == 2
    assert all(r["model"] == "chat" and r["source"] == "api" and r["day"] and "prompt_tokens" in r
               for r in sample.OBSERVED)


def test_fy_ext_010_a_refused_video_job(sample, vid):  # noqa: F811
    """FY-EXT-010: POST /v1/videos is checked too: the extension's status and code, no job on any engine."""
    sample.REFUSE_KEYS.add("laptop")
    r = make(vid.api)
    assert r.status_code == 402 and err(r)["code"] == "sample_quota_exhausted", r.text
    assert all(not e.posts for e in vid.engines)
    sample.REFUSE_KEYS.clear()
    assert make(vid.api).status_code == 200


# ------------------------------------------------------------------ a broken extension
def test_fy_ext_011_a_hook_that_raises_allows(sample, gw, caplog):  # noqa: F811
    """FY-EXT-011: a placement check and a gateway check that raise are logged and allow: requests are served and
    deployments placed (an extension's bug must not stop models from serving)."""
    sample.RAISE.update({"placement_check", "gateway_check"})
    with caplog.at_level(logging.ERROR, logger="forgeyard"):
        assert chat(gw.api).status_code == 200
        r = gw.owner.post("/api/deployments", json={"name": "more", **CPU_BODY})
        assert r.status_code == 201, r.text
    text_ = "\n".join(caplog.messages)
    assert "Extension gateway check tests.fixtures.ext_sample.gateway_check failed; allowing" in text_
    assert "Extension placement check tests.fixtures.ext_sample.placement_check failed; allowing" in text_


# ------------------------------------------------------------------ what a placement check is asked (1.6.2)
@pytest.fixture()
def teams(sample, cpus, monkeypatch):
    """The two CPU machines, teams red and blue; agents are not told anything (no connection)."""
    c, a, b = cpus
    monkeypatch.setattr(hub, "poke", lambda node_id: False)
    ids = {}
    for name in ("red", "blue"):
        r = c.post("/api/teams", json={"name": name, "title": name.title()})
        assert r.status_code == 201, r.text
        ids[name] = r.json()["team"]["id"]
    return SimpleNamespace(c=c, a=a, b=b, red=ids["red"], blue=ids["blue"])


def asked(served: str) -> list[dict]:
    """What the sample's placement check was asked for that deployment, each distinct ask once, in order."""
    out: list[dict] = []
    for x in ext_sample.ASKED:
        if x["served"] == served and x not in out:
            out.append(x)
    return out


def ask(served: str, team_id, replicas: int, adding: int, release=(), replacing: bool = False) -> dict:
    return {"served": served, "team_id": team_id, "replicas": replicas, "adding": adding, "release": list(release),
            "replacing": replacing}


def test_fy_ext_012_estimate_and_deploy_ask_with_the_team_and_what_they_add(teams):
    """FY-EXT-012: the estimate and the deploy's preview are asked with the team the request names and every replica
    as added; the machine filter sees the team too; each replica placed after the deploy is asked as one added. No
    team, or a name no team has (the estimate does not refuse it, as before): no team."""
    c = teams.c
    r = c.post("/api/deployments/estimate", json={"name": "chat", **CPU_BODY, "team": "red", "replicas": 2})
    assert r.status_code == 200 and r.json()["placement"]["fits"] is True, r.text
    assert asked("chat") == [ask("chat", teams.red, 2, 2)]
    assert {f["team_id"] for f in ext_sample.FILTERED if f["served"] == "chat"} == {teams.red}
    for team in (None, "nobody"):
        ext_sample.ASKED.clear()
        assert c.post("/api/deployments/estimate", json={"name": "chat", **CPU_BODY, "team": team}).status_code == 200
        assert asked("chat") == [ask("chat", None, 1, 1)]
    ext_sample.ASKED.clear()
    ext_sample.FILTERED.clear()
    deploy(c, "chat", **CPU_BODY, team="red", replicas=2)
    assert asked("chat") == [ask("chat", teams.red, 2, 2), ask("chat", teams.red, 2, 1)]
    assert sum(1 for x in ext_sample.ASKED if x["served"] == "chat") == 3   # the preview, then each replica
    assert all(r.node_id is not None for r in rows("chat"))
    assert {f["team_id"] for f in ext_sample.FILTERED if f["served"] == "chat"} == {teams.red}


def test_fy_ext_013_an_edit_preview_asks_for_the_new_team_and_the_replicas_it_adds(teams):
    """FY-EXT-013: an edit's preview releases the deployment's replicas and places them again: it adds only replicas
    beyond them (1 → 2: one), none when the number stays or shrinks; the team is the one the edit moves it to (another
    team, or none), else its own. A quota that refuses only added replicas refuses 1 → 2, not a change of settings."""
    c = teams.c
    d = deploy(c, "chat", **CPU_BODY, team="red")["deployment"]
    rid = rows("chat")[0].id
    path = f"/api/deployments/{d['id']}"
    for change, expected in (
            ({"team": "blue", "replicas": 2}, ask("chat", teams.blue, 2, 1, [rid], True)),
            ({"params": {"context": 2048}}, ask("chat", teams.red, 1, 0, [rid], True)),
            ({"team": None}, ask("chat", None, 1, 0, [rid], True)),
            ({"team": "red", "replicas": 2}, ask("chat", teams.red, 2, 1, [rid], True))):
        ext_sample.ASKED.clear()
        r = c.patch(path, json={**change, "dry_run": True})
        assert r.status_code == 200, r.text
        assert asked("chat") == [expected], change
    ext_sample.REFUSE_ADDING.add("chat")
    r = c.patch(path, json={"replicas": 2})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "sample_quota", r.text
    assert len(rows("chat")) == 1
    assert c.patch(path, json={"params": {"context": 2048}, "team": "blue"}).status_code == 200


def test_fy_ext_014_a_rolling_restart_is_asked_as_replacing_and_keeps_serving(teams):
    """FY-EXT-014: a change that restarts the replicas of a running deployment at a quota that refuses added replicas:
    the edit is allowed; the replica that had ended for good starts again at once and the healthy one restarts after
    it (the rolling restart); each is asked as replacing (adds nothing), with the deployment's team, and is placed."""
    c = teams.c
    d = deploy(c, "chat", **CPU_BODY, team="red", replicas=2)["deployment"]
    ready("chat", 0)
    set_replica("chat", 1, state="failed", final=True)
    ext_sample.REFUSE_ADDING.add("chat")
    ext_sample.ASKED.clear()
    r = c.patch(f"/api/deployments/{d['id']}", json={"params": {"context": 2048}})
    assert r.status_code == 200 and r.json()["restart"] == "rolling", r.text
    ids = sorted(x.id for x in rows("chat"))
    assert asked("chat") == [ask("chat", teams.red, 2, 0, ids, True), ask("chat", teams.red, 2, 0, (), True)]
    one = rows("chat")[1]
    assert one.generation == 2 and one.node_id is not None and one.reason is None
    assert rows("chat")[0].generation == 1   # waits for the one before it
    ready("chat", 1)
    ext_sample.ASKED.clear()
    sweep()
    zero = rows("chat")[0]
    assert zero.generation == 2 and zero.node_id is not None and zero.reason is None
    assert asked("chat") == [ask("chat", teams.red, 2, 0, (), True)]


def test_fy_ext_015_start_and_wake_are_asked_as_new_replicas(teams):
    """FY-EXT-015: a person's Start and a request that wakes an asleep deployment start replicas: each is asked as
    one added (not replacing), with the deployment's team; a quota that refuses added replicas refuses them."""
    c = teams.c
    d = deploy(c, "chat", **CPU_BODY, team="red")["deployment"]
    assert c.post(f"/api/deployments/{d['id']}/stop").status_code == 200
    set_replica("chat", 0, state="stopped")
    ext_sample.ASKED.clear()
    assert c.post(f"/api/deployments/{d['id']}/start").status_code == 200
    assert asked("chat") == [ask("chat", teams.red, 1, 1)] and rows("chat")[0].node_id is not None
    assert c.post(f"/api/deployments/{d['id']}/stop").status_code == 200
    set_replica("chat", 0, state="stopped")
    ext_sample.REFUSE_ADDING.add("chat")
    assert c.post(f"/api/deployments/{d['id']}/start").status_code == 200
    assert rows("chat")[0].node_id is None and "quota" in rows("chat")[0].reason
    ext_sample.REFUSE_ADDING.clear()
    sleepy(c, "nap", team="blue")
    ext_sample.ASKED.clear()
    ext_sample.REFUSE_ADDING.add("nap")
    r = _api(c).post("/v1/chat/completions", json={"model": "nap", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503 and err(r)["code"] == "cannot_wake", r.text
    assert asked("nap") == [ask("nap", teams.blue, 1, 1)]


def _offline(node_id: int) -> None:
    from forgeyard.workloads import offline

    offline.server_started(utcnow() - timedelta(days=3))
    set_node(node_id, status="offline", last_heartbeat_at=utcnow() - timedelta(minutes=11))


def test_fy_ext_016_replicas_placed_again_are_asked_as_replacing(teams):
    """FY-EXT-016: a replica moved off a machine offline too long, one placed again after its download could not reach
    the source, and one whose machine was removed are each asked as replacing (adding nothing), with the deployment's
    team, so a quota that refuses added replicas lets them run elsewhere. Stopped and started again afterwards, a
    replica is a new one again."""
    from forgeyard.workloads import offline

    c, a, b = teams.c, teams.a, teams.b
    for name in ("moved", "fetched"):
        deploy(c, name, **CPU_BODY, team="red")
        assert rows(name)[0].node_id == b   # binpack
    ext_sample.REFUSE_ADDING.update({"moved", "fetched", "removed"})
    ext_sample.ASKED.clear()
    assert report(b, rows("fetched")[0], "backoff", DNS) is True
    assert rows("fetched")[0].node_id == a
    assert asked("fetched") == [ask("fetched", teams.red, 1, 0, (), True)]
    try:
        _offline(b)
        db = new_session()
        try:
            assert offline.move_off(db) == ["moved"]
            placement.place_pending(db)
        finally:
            db.close()
    finally:
        offline.server_started()
    assert rows("moved")[0].node_id == a and asked("moved") == [ask("moved", teams.red, 1, 0, (), True)]
    small = make_node("cpu-c", features=MODELS)
    cpu_row(small, vram_free=10 * GIB)
    ext_sample.REFUSE_ADDING.discard("removed")
    deploy(c, "removed", **CPU_BODY, team="blue")
    ext_sample.REFUSE_ADDING.add("removed")
    assert rows("removed")[0].node_id == small
    ext_sample.ASKED.clear()
    assert c.delete(f"/api/machines/{small}").status_code == 200
    assert rows("removed")[0].node_id == a and asked("removed") == [ask("removed", teams.blue, 1, 0, (), True)]
    dep = next(x["id"] for x in c.get("/api/deployments").json()["deployments"] if x["name"] == "moved")
    assert c.post(f"/api/deployments/{dep}/stop").status_code == 200
    set_replica("moved", 0, state="stopped")
    ext_sample.ASKED.clear()
    assert c.post(f"/api/deployments/{dep}/start").status_code == 200
    assert asked("moved") == [ask("moved", teams.red, 1, 1)] and rows("moved")[0].node_id is None


def test_fy_ext_017_a_replica_out_of_ports_is_asked_as_replacing(sample, owner, monkeypatch):
    """FY-EXT-017: a replica its agent could not give a port, taken off the machine because the count agrees, is
    asked as replacing (adding nothing) when it is placed again; a quota that refuses added replicas does not refuse
    it (it waits for a port, saying so)."""
    from tests.test_placement_ports import GUARDED, LLAMA, ranged, set_range
    from tests.test_placement_ports import report as port_report

    monkeypatch.setattr(hub, "poke", lambda node_id: False)
    seed_catalog()
    nid = make_node("tunnel", features=GUARDED, runtime_info=ranged(33000, 33003))
    r = owner.post("/api/teams", json={"name": "red", "title": "Red"})
    red = r.json()["team"]["id"]
    deploy(owner, "pair", **LLAMA, team="red", replicas=2)
    assert all(x.node_id == nid for x in rows("pair"))
    set_range(nid, 33000, 33001)
    sample.REFUSE_ADDING.add("pair")
    sample.ASKED.clear()
    second = rows("pair")[1]
    assert port_report(nid, second, "backoff", "no free host port left in 33000-33001; restarting in 2 s") is True
    sweep()
    assert asked("pair") == [ask("pair", red, 2, 0, (), True)]
    left = rows("pair")[1]
    assert left.node_id is None and "no free port" in left.reason and "quota" not in left.reason
    # Stopped while it waits, then started: it starts as a new replica (the quota refuses it now).
    dep = next(x["id"] for x in owner.get("/api/deployments").json()["deployments"] if x["name"] == "pair")
    assert owner.post(f"/api/deployments/{dep}/stop").status_code == 200
    set_replica("pair", 0, state="stopped")
    sample.ASKED.clear()
    assert owner.post(f"/api/deployments/{dep}/start").status_code == 200
    assert asked("pair") == [ask("pair", red, 2, 1)]
    assert all(x.node_id is None and "quota" in x.reason for x in rows("pair"))


def test_fy_ext_018_autoscale_asks_for_one_more_replica(sample, cpus, monkeypatch):
    """FY-EXT-018: the autoscaler's room check is asked for one added replica, with the deployment's team."""
    c, a, b = cpus
    run = Run(monkeypatch)
    d = auto(c)
    assert c.post("/api/teams", json={"name": "red", "title": "Red"}).status_code == 201
    red = c.get("/api/teams").json()["teams"][0]["id"]
    assert c.patch(f"/api/deployments/{d['id']}", json={"team": "red"}).status_code == 200
    sample.REFUSE_ADDING.add("chat")
    sample.ASKED.clear()
    run.load("chat", 2)
    assert run.run(20) == ["Wants 2 replicas, room for 1: The sample extension's quota allows no more replicas of "
                           "chat."]
    assert asked("chat") == [ask("chat", red, 1, 1)]
