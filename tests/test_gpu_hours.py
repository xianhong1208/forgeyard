"""GPU hours (1.6.1, #190): what the usage recorder counts while a replica holds its cards (from engine start until its
reservation is freed; not while downloading or asleep), how much (exclusive cards whole, a shared card its reservation's
share, a CPU replica its RAM), to which deployment, team, machine, GPU model and UTC day, the busy part, the Usage API
and a deployment's page, /metrics, a server that was down, and the migration (FY-GPUH-*).

Expected values come from the rule, with a fake clock: one replica on one card for 10 minutes is 600,000 GPU-ms
(0.17 GPU hours); a 20 GiB reservation on an 80 GiB card is a quarter of that."""

import re
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import inspect, select

from forgeyard import metrics
from forgeyard.db import Accelerator, Deployment, GpuUsageDaily, Replica, Workload, get_engine, new_session
from forgeyard.gateway.balancer import Balancer
from forgeyard.gateway.usage import recorder
from forgeyard.migrate import alembic_config, upgrade
from forgeyard.workloads import gpu_hours, sleep
from tests.conftest import add_user, sign_in
from tests.test_scheduler import deploy, fleet  # noqa: F401 (the fixture)
from tests.workload_helpers import make_node

GIB = 1024**3
MIB = 1024**2
PW = "quiet-meadow-4821"
A100 = "NVIDIA A100 80GB PCIe"
# Six in the morning (UTC) of the day the tests run: the Usage page's "last N days" and "this month" include it.
T0 = (int(time.time()) // 86400) * 86400 + 6 * 3600.0
TODAY = datetime.fromtimestamp(T0, tz=timezone.utc).date().isoformat()


@pytest.fixture()
def clock(monkeypatch):
    """The recorder's and the meter's clock, and a balancer on the same clock for the busy part."""
    t = [T0]
    busy = Balancer(clock=lambda: t[0], wall=lambda: t[0])
    monkeypatch.setattr(recorder, "clock", lambda: t[0])
    monkeypatch.setattr(recorder.gpu, "clock", lambda: t[0])
    monkeypatch.setattr(recorder.gpu, "_busy", busy.take_busy)
    recorder.flush()   # the first write sees the replicas: counting starts here
    return SimpleNamespace(t=t, busy=busy)


def run(clock, seconds: float, step: float = 5.0) -> None:
    """The recorder writes every `step` seconds for `seconds`."""
    for _ in range(int(round(seconds / step))):
        clock.t[0] += step
        recorder.flush()


def hold(workload: str, state: str = "healthy", *, cards=None, vram=None, share=None, download=None, reason=None,
         ordinal: int = 0) -> str:
    """Set what a replica holds and its state, as its agent and the scheduler would have; returns its uid."""
    db = new_session()
    try:
        r = db.scalar(select(Replica).join(Workload).where(Workload.name == workload, Replica.ordinal == ordinal))
        if cards is not None:
            accs = {a.index: a for a in db.scalars(select(Accelerator).where(Accelerator.node_id == r.node_id,
                                                                            Accelerator.vendor != "cpu")).all()}
            r.gpus = [{"key": accs[i].key, "vendor": accs[i].vendor, "index": i, "model": accs[i].model} for i in cards]
        if vram is not None:
            r.vram = vram
        if share is not None:
            spec = dict(r.spec or {})
            res = dict(spec.get("resources") or {})
            res["gpus"] = {**(res.get("gpus") or {}), "share": share}
            spec["resources"] = res
            r.spec = spec
        r.state, r.download, r.reason = state, download, reason
        db.commit()
        return r.uid
    finally:
        db.close()


def rows(**where) -> list[GpuUsageDaily]:
    db = new_session()
    try:
        q = select(GpuUsageDaily).order_by(GpuUsageDaily.day, GpuUsageDaily.id)
        for k, v in where.items():
            q = q.where(getattr(GpuUsageDaily, k) == v)
        return list(db.scalars(q).all())
    finally:
        db.close()


def gpu_ms(**where) -> int:
    return sum(r.gpu_ms for r in rows(**where))


def dep_id(name: str) -> int:
    db = new_session()
    try:
        return db.scalar(select(Deployment.id).where(Deployment.name == name))
    finally:
        db.close()


@pytest.fixture()
def chat(fleet, clock):  # noqa: F811
    """`chat` (vLLM, Qwen3-4B) placed on the A100 host's card #4, holding it exclusively and healthy."""
    c, a100, rtx = fleet
    deploy(c, "chat", "qwen3-4b", "vllm")
    hold("chat", cards=[4], share="exclusive")
    recorder.flush()
    return SimpleNamespace(c=c, a100=a100, rtx=rtx, clock=clock, id=dep_id("chat"))


def test_fy_gpuh_001_one_exclusive_card_for_ten_minutes(chat):
    """FY-GPUH-001: a replica holding one card for 10 minutes is 600,000 GPU-ms (0.17 GPU hours), on one row for its
    day, deployment, machine and GPU model (no team), with no RAM-time."""
    run(chat.clock, 600)
    [row] = rows()
    assert (row.day, row.deployment_id, row.team_id, row.node_id, row.vendor, row.gpu_model) == \
        (TODAY, chat.id, 0, chat.a100, "nvidia", A100)
    assert row.gpu_ms == 600_000 and row.ram_mib_ms == 0
    assert round(row.gpu_ms / 3_600_000, 2) == 0.17


def test_fy_gpuh_002_a_shared_card_counts_its_reservations_share(chat):
    """FY-GPUH-002: 20 GiB reserved on a shared 80 GiB card is a quarter of the card: 10 minutes = 150,000 GPU-ms. A
    reservation larger than the card counts the card once."""
    hold("chat", cards=[4], vram=20 * GIB, share="vram")
    run(chat.clock, 600)
    assert gpu_ms() == 150_000
    hold("chat", cards=[4], vram=100 * GIB, share="vram")
    run(chat.clock, 60)
    assert gpu_ms() == 150_000 + 60_000


def test_fy_gpuh_003_an_exclusive_replica_on_two_cards_counts_two(chat):
    """FY-GPUH-003: an exclusive replica on two cards counts each whole: 10 minutes = 1,200,000 GPU-ms; two replicas on
    machines with different cards add to one row per GPU model."""
    hold("chat", cards=[4, 5], vram=70 * GIB, share="exclusive")
    run(chat.clock, 600)
    assert gpu_ms() == 1_200_000
    deploy(chat.c, "asr", "faster-whisper-small", "faster-whisper")
    hold("asr", cards=[1], share="exclusive")
    recorder.flush()
    run(chat.clock, 60)
    assert {r.gpu_model: r.gpu_ms for r in rows()} == {A100: 1_200_000 + 120_000, "NVIDIA GeForce RTX 4090": 60_000}


def test_fy_gpuh_004_downloading_and_pending_do_not_count(chat):
    """FY-GPUH-004: a placed replica that downloads its model (progress), pulls its image, or fetches without progress
    (an older agent), or is still pending, counts nothing; counting starts once its engine runs."""
    for state, download, reason in (
            ("starting", {"phase": "model", "done": 1, "total": 9}, "downloading the model: 0.00 of 0.00 GiB (11%)"),
            ("starting", {"phase": "image", "done": 1, "total": 9}, "pulling the image: 1 of 9 layers"),
            ("starting", None, "downloading the model: 420 MiB"),
            ("starting", None, "preparing the model"),
            ("pending", None, None)):
        hold("chat", state, cards=[4], download=download, reason=reason)
        run(chat.clock, 60)
        assert gpu_ms() == 0, (state, reason)
    hold("chat", "starting", cards=[4], reason="waiting for the health check")
    recorder.flush()   # first seen counting
    run(chat.clock, 60)
    assert gpu_ms() == 60_000


def test_fy_gpuh_005_asleep_and_stopped_do_not_count(chat):
    """FY-GPUH-005: a deployment put to sleep stops counting at once (its replicas are stopping, then stopped), and so
    does one a person stopped; woken again, it counts again."""
    run(chat.clock, 60)
    db = new_session()
    try:
        sleep.put_to_sleep(db, db.get(Deployment, chat.id), lambda _n: True, 1)
        db.commit()
    finally:
        db.close()
    run(chat.clock, 300)
    hold("chat", "stopped", cards=[4])
    run(chat.clock, 300)
    assert gpu_ms() == 60_000
    r = chat.c.post(f"/api/deployments/{chat.id}/start")
    assert r.status_code == 200, r.text
    hold("chat", cards=[4], share="exclusive")
    recorder.flush()
    run(chat.clock, 60)
    assert gpu_ms() == 120_000
    assert chat.c.post(f"/api/deployments/{chat.id}/stop").status_code == 200
    run(chat.clock, 300)
    assert gpu_ms() == 120_000


def test_fy_gpuh_006_a_cpu_replica_counts_its_ram_not_a_gpu(owner, clock):
    """FY-GPUH-006: a CPU replica adds no GPU time, but its reserved RAM × time: 2 GiB for 10 minutes = 2048 MiB ×
    600,000 ms, on a row named after the machine's CPU."""
    from tests.test_scheduler import seed_catalog

    seed_catalog()
    node = make_node("cpu-a", "nvidia-rtx4090x2", features=("workloads", "models"))
    deploy(owner, "small", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2})
    hold("small", vram=2 * GIB)
    recorder.flush()
    run(clock, 600)
    [row] = rows()
    assert (row.vendor, row.gpu_model, row.node_id, row.gpu_ms) == ("cpu", "AMD Ryzen 9 7950X 16-Core Processor", node, 0)
    assert row.ram_mib_ms == 2048 * 600_000


def test_fy_gpuh_007_time_counts_to_the_deployments_team(chat):
    """FY-GPUH-007: a team's deployment counts to the team; moved to another team, new rows start for it (the old ones
    stay with the first team); with no team, team 0."""
    c = chat.c
    for name in ("ml", "vision"):
        assert c.post("/api/teams", json={"name": name, "title": name.title()}).status_code == 201
    teams = {t["name"]: t["id"] for t in c.get("/api/teams").json()["teams"]}
    run(chat.clock, 60)
    for name in ("ml", "vision"):
        r = c.patch(f"/api/deployments/{chat.id}", json={"team": name})
        assert r.status_code == 200, r.text
        hold("chat", cards=[4], share="exclusive")   # the edit may restart nothing; the replica keeps its card
        run(chat.clock, 60)
    got = {r.team_id: r.gpu_ms for r in rows()}
    assert got == {0: 60_000, teams["ml"]: 60_000, teams["vision"]: 60_000}
    db = new_session()
    try:
        assert gpu_hours.month_total(db, team_id=teams["ml"])["gpu_ms"] == 60_000
    finally:
        db.close()


def test_fy_gpuh_008_days_and_months_are_utc(chat):
    """FY-GPUH-008: an interval across midnight (UTC) is split between the two days, and across the end of a month
    between the two months."""
    db = new_session()
    try:
        db.query(GpuUsageDaily).delete()
        db.commit()
    finally:
        db.close()
    recorder.gpu.reset()
    chat.clock.t[0] = datetime(2026, 11, 30, 23, 59, 58, tzinfo=timezone.utc).timestamp()
    recorder.flush()
    run(chat.clock, 5)   # 23:59:58 .. 00:00:03
    assert {r.day: r.gpu_ms for r in rows()} == {"2026-11-30": 2_000, "2026-12-01": 3_000}
    db = new_session()
    try:
        assert gpu_hours.month_total(db, month="2026-11")["gpu_ms"] == 2_000
        assert gpu_hours.month_total(db, month="2026-12")["gpu_ms"] == 3_000
    finally:
        db.close()


def scraped(deployment: str, model: str) -> float:
    body, _ = metrics.render()
    m = re.search(rf'^forgeyard_gpu_seconds_total\{{deployment="{deployment}",gpu_model="{re.escape(model)}"\}} (\S+)$',
                  body.decode(), re.M)
    return float(m.group(1)) if m else 0.0


def test_fy_gpuh_009_metrics_counts_gpu_seconds_per_deployment_and_model(chat):
    """FY-GPUH-009: /metrics' forgeyard_gpu_seconds_total grows by the GPU-seconds written, labelled with the deployment
    and the GPU model only (no key, user or team)."""
    before = scraped("chat", A100)
    hold("chat", cards=[4], vram=20 * GIB, share="vram")
    run(chat.clock, 600)
    assert scraped("chat", A100) - before == pytest.approx(150.0)
    line = next(ln for ln in metrics.render()[0].decode().splitlines() if ln.startswith("forgeyard_gpu_seconds_total{"))
    assert set(re.findall(r'(\w+)="', line)) == {"deployment", "gpu_model"}


def test_fy_gpuh_010_busy_is_the_part_with_a_request_in_flight(chat):
    """FY-GPUH-010: a replica with a request in flight for 150 of 600 seconds is busy a quarter of its GPU time; a
    request still in flight counts up to the write."""
    uid = hold("chat", cards=[4], share="exclusive")
    b, t = chat.clock.busy, chat.clock.t
    for _ in range(3):   # 50 s busy in each of the first three minutes
        b.acquire(uid)
        run(chat.clock, 50)
        b.release(uid)
        run(chat.clock, 10)
    run(chat.clock, 420)
    [row] = rows()
    assert row.gpu_ms == 600_000 and row.busy_ms == 150_000
    b.acquire(uid)
    t[0] += 3
    recorder.flush()
    assert rows()[0].busy_ms == 153_000
    db = new_session()
    try:
        total = gpu_hours.month_total(db, deployment_id=chat.id)
    finally:
        db.close()
    assert total["busy_share"] == round(153_000 / 603_000, 4)


def test_fy_gpuh_011_the_api_and_who_sees_what(chat):
    """FY-GPUH-011: GET /api/usage/gpu by deployment, team, machine and GPU model for admins and operators; a team's
    managers see their team's (?team=), members and other users get 403, a team they are not in 404; a bad group 422.
    A deployment's page has its GPU time this month."""
    c = chat.c
    assert c.post("/api/teams", json={"name": "ml", "title": "Machine learning"}).status_code == 201
    for name, role in (("mia", "user"), ("max", "user"), ("ola", "operator"), ("uwe", "user")):
        add_user(c, name, role=role)
    assert c.put("/api/teams/ml/members/mia", json={"role": "manager"}).status_code == 200
    assert c.put("/api/teams/ml/members/max", json={"role": "member"}).status_code == 200
    assert c.patch(f"/api/deployments/{chat.id}", json={"team": "ml"}).status_code == 200
    hold("chat", cards=[4], share="exclusive")
    recorder.flush()
    run(chat.clock, 600)
    for group, name in (("deployment", "chat"), ("team", "Machine learning"), ("machine", "a100"), ("gpu_model", A100)):
        r = c.get(f"/api/usage/gpu?days=30&group={group}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert [(x["name"], x["gpu_ms"]) for x in body["rows"]] == [(name, 600_000)], group
        assert body["total"]["gpu_ms"] == 600_000 and body["everything"] is True and body["counting_since"]
    people = {n: sign_in(c, n, PW) for n in ("mia", "max", "ola", "uwe")}
    assert people["ola"].get("/api/usage/gpu").json()["total"]["gpu_ms"] == 600_000
    mine = people["mia"].get("/api/usage/gpu?team=ml&days=30").json()
    assert mine["total"]["gpu_ms"] == 600_000 and mine["team"]["name"] == "ml" and [t["name"] for t in mine["teams"]] == ["ml"]

    def code(r):
        return r.status_code, r.json()["detail"]["code"]

    assert code(people["mia"].get("/api/usage/gpu")) == (403, "forbidden")
    assert code(people["max"].get("/api/usage/gpu?team=ml")) == (403, "not_team_manager")
    assert code(people["uwe"].get("/api/usage/gpu?team=ml")) == (404, "team_not_found")
    assert code(people["uwe"].get("/api/usage/gpu")) == (403, "forbidden")
    assert code(c.get("/api/usage/gpu?group=key")) == (422, "bad_group")
    page = c.get(f"/api/deployments/{chat.id}").json()["deployment"]["usage"]["gpu"]
    assert page["gpu_ms"] == 600_000 and page["month"] == TODAY[:7] and page["busy_share"] == 0


def test_fy_gpuh_012_a_server_that_was_down_counts_nothing(chat):
    """FY-GPUH-012: after a restart (an empty meter) the first write counts nothing, and a gap between two writes longer
    than MAX_GAP (a suspended server) counts MAX_GAP only."""
    run(chat.clock, 60)
    recorder.gpu.reset()          # the server restarted
    chat.clock.t[0] += 3600       # it was down for an hour
    recorder.flush()
    assert gpu_ms() == 60_000
    run(chat.clock, 60)
    assert gpu_ms() == 120_000
    chat.clock.t[0] += 600        # one write ten minutes after the last one
    recorder.flush()
    assert gpu_ms() == 120_000 + int(gpu_hours.MAX_GAP * 1000)


def test_fy_gpuh_013_a_failed_write_is_counted_at_the_next(chat, monkeypatch):
    """FY-GPUH-013: when the database refuses a write, its GPU time is written with the next one: not lost, not
    counted twice."""
    run(chat.clock, 60)
    real = recorder.gpu.write

    def broken(db):
        real(db)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(recorder.gpu, "write", broken)
    chat.clock.t[0] += 5
    with pytest.raises(RuntimeError):
        recorder.flush()
    monkeypatch.setattr(recorder.gpu, "write", real)
    run(chat.clock, 5)
    assert gpu_ms() == 70_000


def test_fy_gpuh_014_the_migration_adds_and_drops_the_table(chat):
    """FY-GPUH-014: 0023 adds gpu_usage_daily with its slot index (unique); going down drops it and nothing else; up
    again brings it back empty."""
    run(chat.clock, 60)

    def tables():
        return set(inspect(get_engine()).get_table_names())

    before = tables()
    assert "gpu_usage_daily" in before
    idx = {i["name"]: i for i in inspect(get_engine()).get_indexes("gpu_usage_daily")}
    assert idx["ix_gpu_usage_daily_slot"]["unique"] and idx["ix_gpu_usage_daily_slot"]["column_names"] == \
        ["day", "deployment_id", "team_id", "node_id", "gpu_model"]
    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0022_teams")
    assert tables() == before - {"gpu_usage_daily"}
    upgrade()
    assert tables() == before and rows() == []
