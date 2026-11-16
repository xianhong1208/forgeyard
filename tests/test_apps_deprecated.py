"""Apps are deprecated in 1.4 and removed in 1.5 (#88, #89): the apps part of /api/workloads, the
templates and the gateway's /x/ keep working and say so with a Deprecation header, a server log line
at most once an hour per kind, and a Setup health finding while any app exists (FY-APPDEP-*)."""

from __future__ import annotations

import logging

import pytest

from forgeyard import deprecation
from tests.gateway_helpers import api_client, new_key
from tests.test_advisor import findings
from tests.test_passthrough import xw  # noqa: F401 - the fixture: an exposed app "web" behind its guard
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import make_node, service, task

DEPRECATED = ("deprecation", "link")


@pytest.fixture(autouse=True)
def fresh_log():
    deprecation.reset_for_tests()
    yield
    deprecation.reset_for_tests()


def marked(r) -> bool:
    return (r.headers.get("deprecation") == deprecation.DEPRECATION_HEADER
            and deprecation.LINK_HEADER in r.headers.get_list("link"))


def unmarked(r) -> bool:
    return "deprecation" not in r.headers and deprecation.GUIDE not in r.headers.get("link", "")


def test_fy_appdep_001_the_header_is_rfc_9745_with_a_link_to_the_guide():
    """FY-APPDEP-001: `Deprecation: @<epoch of 2026-11-16>` and a deprecation Link to the reference's
    section; no Sunset (1.5 has no date)."""
    assert deprecation.DEPRECATION_HEADER == "@1794787200"
    assert deprecation.LINK_HEADER == '</guide/en/reference#apps-deprecated>; rel="deprecation"; type="text/html"'
    assert "removed in Forgeyard 1.5" in deprecation.message("x")


def test_fy_appdep_002_every_answer_about_an_app_says_so_errors_included(owner):
    """FY-APPDEP-002: create, list (with an app in it), read, expose, stop, start, logs and delete of an
    app carry the headers, and so does an error after the app is known (409 already_stopped)."""
    node = make_node()
    r = owner.post("/api/workloads", json=service("web", machine_id=node))
    assert r.status_code == 201 and marked(r)
    wid = r.json()["workload"]["id"]
    for r in (owner.get("/api/workloads"), owner.get(f"/api/workloads/{wid}"),
              owner.put(f"/api/workloads/{wid}/gateway", json={"expose": True}),
              owner.post(f"/api/workloads/{wid}/stop"), owner.get(f"/api/workloads/{wid}/replicas/0/logs")):
        assert r.status_code < 300 and marked(r), (r.request.url, r.status_code)
    r = owner.post(f"/api/workloads/{wid}/stop")
    assert r.status_code == 409 and marked(r)
    r = owner.post(f"/api/workloads/{wid}/start")  # its replica may still be stopping (409 still_stopping)
    assert r.status_code in (200, 409) and marked(r)
    # A bad spec for a new app is about an app too.
    r = owner.post("/api/workloads", json={"name": "Bad Name"})
    assert r.status_code == 422 and marked(r)
    r = owner.delete(f"/api/workloads/{wid}")
    assert r.status_code == 200 and marked(r)


def test_fy_appdep_003_model_workloads_and_other_api_are_not_deprecated(owner):
    """FY-APPDEP-003: a model deployment's workload (list, read, logs, and the 409 for stopping it there)
    is how models run: no header. Neither has a list without apps, nor the rest of the API."""
    seed_catalog()
    make_node("cpu-01", features=MODELS)
    dep = deploy(owner, "chat", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0})["deployment"]
    wid = dep["workload"]["id"]
    for r in (owner.get("/api/workloads"), owner.get(f"/api/workloads/{wid}"),
              owner.get(f"/api/workloads/{wid}/replicas/0/logs"), owner.post(f"/api/workloads/{wid}/stop"),
              owner.get("/api/deployments"), owner.get("/api/machines"), owner.get("/api/keys")):
        assert unmarked(r), (r.request.url, r.status_code)
    assert owner.post(f"/api/workloads/{wid}/stop").json()["detail"]["code"] == "managed_by_deployment"
    assert not findings().get("apps-deprecated")


def test_fy_appdep_004_the_templates_api_says_so(owner):
    """FY-APPDEP-004"""
    r = owner.get("/api/workloads/templates")
    assert r.status_code == 200 and r.json()["templates"] and marked(r)


def test_fy_appdep_005_x_says_so_on_answers_and_on_its_own_errors(xw):  # noqa: F811
    """FY-APPDEP-005: an app's answer through /x/, a 404 for an unknown app and a 401 without a key all
    carry the headers; /v1 does not."""
    r = xw.api.get("/x/web/hello")
    assert r.status_code == 200 and marked(r)
    assert r.headers["content-security-policy"].startswith("sandbox")  # the forced headers stay
    r = xw.api.get("/x/nothing-here/a")
    assert r.status_code == 404 and marked(r)
    r = api_client(xw.owner, "").get("/x/web/hello")
    assert r.status_code == 401 and marked(r)
    assert unmarked(xw.api.get("/v1/models"))


def test_fy_appdep_006_the_log_says_it_at_most_once_an_hour_per_kind(owner, caplog):
    """FY-APPDEP-006: the first call of each kind logs a warning naming what to do and where; more calls
    within the hour do not; an hour later it is said again."""
    node = make_node()
    caplog.set_level(logging.WARNING, logger="forgeyard.deprecation")
    owner.post("/api/workloads", json=task(machine_id=node))
    owner.get("/api/workloads")
    owner.get("/api/workloads/templates")
    owner.get("/api/workloads/templates")
    lines = [r.getMessage() for r in caplog.records if r.name == "forgeyard.deprecation"]
    assert len(lines) == 2, lines
    assert "Apps (workloads that are not models)" in lines[0] and "POST /api/workloads" in lines[0]
    assert "removed in Forgeyard 1.5" in lines[0] and "#apps-deprecated" in lines[0]
    assert "templates" in lines[1]
    t = [1000.0]
    deprecation.reset_for_tests()
    assert deprecation.warn("x", clock=lambda: t[0]) is True
    t[0] += deprecation.LOG_EVERY_S - 1
    assert deprecation.warn("x", clock=lambda: t[0]) is False
    assert deprecation.warn("apps", clock=lambda: t[0]) is True   # per kind
    t[0] += 1
    assert deprecation.warn("x", clock=lambda: t[0]) is True


def test_fy_appdep_007_setup_health_warns_while_any_app_exists(owner):
    """FY-APPDEP-007: no app, no finding; with apps, one warning naming them, linking the reference and
    shown on Machines; it goes once they are deleted."""
    assert not findings().get("apps-deprecated")
    node = make_node()
    a = owner.post("/api/workloads", json=service("web", machine_id=node)).json()["workload"]["id"]
    owner.post("/api/workloads", json=task("batch", machine_id=node))
    f = findings()["apps-deprecated"]
    assert len(f) == 1 and f[0].severity == "warning" and f[0].snoozable
    assert f[0].title == "2 apps will stop working in Forgeyard 1.5"
    assert "batch, web are apps" in f[0].detail and "/x/" in f[0].detail
    assert f[0].fix_url == "/guide/en/reference#apps-deprecated" and f[0].pages == ("machines",)
    owner.delete(f"/api/workloads/{a}")
    assert findings()["apps-deprecated"][0].title == "1 app will stop working in Forgeyard 1.5"
    b = next(w["id"] for w in owner.get("/api/workloads").json()["workloads"] if w["name"] == "batch")
    owner.delete(f"/api/workloads/{b}")
    assert not findings().get("apps-deprecated")


def test_fy_appdep_008_the_console_options_no_longer_name_apps(xw):  # noqa: F811
    """FY-APPDEP-008: the key list and the Playground's vision options have no `workloads` for the console;
    a key's own `workloads` setting stays in the API (deprecated)."""
    keys = xw.owner.get("/api/keys").json()
    assert "workloads" not in keys
    assert {k["name"]: k["workloads"] for k in keys["keys"]}["laptop"] == "all"
    assert "workloads" not in xw.owner.get("/api/playground/vision/options").json()
    k = new_key(xw.owner, "listed", workloads="listed", workload_list=["web"])["api_key"]
    assert (k["workloads"], k["workload_list"]) == ("listed", ["web"])
