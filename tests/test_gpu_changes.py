"""A machine's GPUs (1.5.4; FY-GPUCH-*): the agent counts the host's GPUs on its PCI bus next to the ones it sees
(agent/gpu_census.py), the server records GPUs added and removed (machines/gpu_changes.py), and Setup health says what
to do (advisor.machine_gpus_hidden, machine_gpu_changes)."""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from forgeyard import advisor
from forgeyard.agent import gpu_census as gc
from forgeyard.db import AuditEvent, Node, new_session, utcnow
from forgeyard.machines import gpu_changes, store
from forgeyard.machines.messages import HelloIn, InventoryIn, parse_gpu_census
from forgeyard.workloads import placement
from tests.machines_helpers import SIM, connect, hello, joined, machine
from tests.test_advisor import edit, findings, gpu, node

GIB = 1024**3


# ------------------------------------------------------------------ a stand-in /sys/bus/pci/devices
def pci(root: Path, bdf: str, vendor: str, cls: str, *, vf: bool = False) -> None:
    d = root / bdf
    d.mkdir(parents=True)
    (d / "vendor").write_text(vendor + "\n")
    (d / "class").write_text(cls + "\n")
    if vf:
        (d / "physfn").mkdir()


def nvidia_host(root: Path, gpus: int) -> list[str]:
    """An NVIDIA server: each GPU with its HDMI audio function (some cards have one), an NVSwitch, the CPU's own
    functions and an Ethernet card; returns the GPUs' bus ids."""
    ids = []
    for i in range(gpus):
        bdf = f"0000:{0x17 + i * 0x10:02x}:00.0"
        pci(root, bdf, "0x10de", "0x030200")
        pci(root, bdf[:-1] + "1", "0x10de", "0x040300")
        ids.append(bdf)
    pci(root, "0000:e0:00.0", "0x10de", "0x068000")   # NVSwitch
    pci(root, "0000:00:00.0", "0x8086", "0x060000")   # host bridge
    pci(root, "0000:03:00.0", "0x15b3", "0x020000")   # NIC
    return ids


def seen(ids: list[str], vendor: str = "nvidia", nvml: bool = True) -> list[dict]:
    """Accelerators as the inventory reports them: NVML writes an 8-digit domain in upper case."""
    def bus(b):
        return ("0000" + b).upper() if nvml else b
    return [{"vendor": vendor, "detail": {"pci_bus_id": bus(b)}} for b in ids] + [{"vendor": "cpu", "detail": {}}]


def test_fy_gpuch_001_an_nvidia_host_with_8_or_9_gpus(tmp_path):
    """FY-GPUCH-001: 3D controllers of vendor 0x10de count, their audio functions, an NVSwitch and other vendors do
    not; NVML's "00000000:17:00.0" matches sysfs's "0000:17:00.0"; a 9th GPU the agent does not see is named."""
    eight = nvidia_host(tmp_path / "a", 8)
    assert gc.host_gpus(tmp_path / "a") == {"nvidia": eight}
    c = gc.census(seen(eight), tmp_path / "a")
    assert c == {"host_gpus": 8, "visible_gpus": 8, "missing": [],
                 "vendors": {"nvidia": {"host": 8, "visible": 8, "missing": []}}}
    nine = nvidia_host(tmp_path / "b", 9)
    c = gc.census(seen(nine[:8]), tmp_path / "b")
    assert (c["host_gpus"], c["visible_gpus"], c["missing"]) == (9, 8, [nine[8]])
    pci(tmp_path / "c", "0000:01:00.0", "0x10de", "0x030000")   # a GeForce on VGA
    assert gc.host_gpus(tmp_path / "c") == {"nvidia": ["0000:01:00.0"]}


def test_fy_gpuch_002_amd_apu_and_dgpus(tmp_path):
    """FY-GPUCH-002: an APU's graphics (0x0300) is one GPU, not its audio, crypto or the CPU's (0x1022) functions; an
    RX card (0x0300, with audio), an Instinct MI210 (0x0380) and an MI300 (0x1200) count; an SR-IOV virtual function
    does not."""
    apu = tmp_path / "apu"
    pci(apu, "0000:c5:00.0", "0x1002", "0x030000")
    pci(apu, "0000:c5:00.1", "0x1002", "0x040300")
    pci(apu, "0000:c5:00.2", "0x1022", "0x108000")
    pci(apu, "0000:c5:00.6", "0x1022", "0x040300")
    pci(apu, "0000:c6:00.1", "0x1022", "0x118000")   # the NPU
    assert gc.host_gpus(apu) == {"amd": ["0000:c5:00.0"]}
    amd = json.loads((SIM / "amd-gfx1151-apu.json").read_text())["accelerators"]
    assert gc.census(amd, apu)["vendors"] == {"amd": {"host": 1, "visible": 1, "missing": []}}
    dgpu = tmp_path / "dgpu"
    pci(dgpu, "0000:03:00.0", "0x1002", "0x030000")
    pci(dgpu, "0000:03:00.1", "0x1002", "0x040300")
    pci(dgpu, "0000:41:00.0", "0x1002", "0x038000")
    pci(dgpu, "0000:81:00.0", "0x1002", "0x120000")
    pci(dgpu, "0000:81:02.0", "0x1002", "0x120000", vf=True)
    assert gc.host_gpus(dgpu) == {"amd": ["0000:03:00.0", "0000:41:00.0", "0000:81:00.0"]}
    c = gc.census(seen(["0000:03:00.0", "0000:41:00.0"], "amd", nvml=False), dgpu)
    assert c["vendors"]["amd"] == {"host": 3, "visible": 2, "missing": ["0000:81:00.0"]}


def test_fy_gpuch_003_counts_only_unreadable_and_simulated(tmp_path):
    """FY-GPUCH-003: a seen GPU without its bus id compares by count; no sysfs: nothing; a simulated agent counts only
    in a stand-in tree (FORGEYARD_AGENT_SYSFS), never the real one; bus ids are normalised or refused."""
    ids = nvidia_host(tmp_path / "s", 3)
    blind = [{"vendor": "nvidia", "detail": {}}] * 2
    assert gc.census(blind, tmp_path / "s")["vendors"]["nvidia"] == {"host": 3, "visible": 2, "missing": [ids[2]]}
    assert gc.census([{"vendor": "nvidia", "detail": {}}] * 3, tmp_path / "s")["missing"] == []
    assert gc.census([], tmp_path / "nothing") is None
    assert gc.for_agent([], simulated=True, environ={}) is None
    out = gc.for_agent([], simulated=True, environ={gc.SYSFS_ENV: str(tmp_path / "s")})
    assert (out["host_gpus"], out["visible_gpus"]) == (3, 0)
    assert gc.bus_id("00000000:17:00.0") == "0000:17:00.0" and gc.bus_id("0000:C1:00.0") == "0000:c1:00.0"
    assert gc.bus_id("17:00.0") is None and gc.bus_id("0000:17:00.9") is None and gc.bus_id(None) is None


def test_fy_gpuch_004_the_agent_sends_it_and_the_server_checks_it(tmp_path, monkeypatch):
    """FY-GPUCH-004: the hello carries runtime_info.gpu_census (also from an agent without workloads); the server keeps
    a well-formed one with totals recomputed, and drops anything else."""
    from forgeyard.agent.client import Agent
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory

    fixture = json.loads((SIM / "nvidia-a100x8.json").read_text())["accelerators"]
    for card in fixture[:8]:   # the fixture's own eight, and a ninth it does not list
        pci(tmp_path / "s", gc.bus_id(card["detail"]["pci_bus_id"]), "0x10de", "0x030200")
    pci(tmp_path / "s", "0000:c1:00.0", "0x10de", "0x030200")
    monkeypatch.setenv(SIMULATION_ENV, "1")
    monkeypatch.setenv(gc.SYSFS_ENV, str(tmp_path / "s"))
    inv = SimulatedInventory("nvidia-a100x8")
    msg = Agent("http://127.0.0.1:1", "fym_x", inv).hello(inv.full())
    census = msg["runtime_info"]["gpu_census"]
    assert (census["host_gpus"], census["visible_gpus"], census["missing"]) == (9, 8, ["0000:c1:00.0"])
    assert HelloIn.model_validate(msg).runtime_info["gpu_census"] == census
    monkeypatch.delenv(gc.SYSFS_ENV)
    assert "runtime_info" not in Agent("http://127.0.0.1:1", "fym_x", inv).hello(inv.full())
    good = {"vendors": {"nvidia": {"host": 9, "visible": 8, "missing": ["0000:c1:00.0", "x; reboot"]}},
            "host_gpus": 99, "visible_gpus": 0, "missing": []}
    assert parse_gpu_census(good) == {"host_gpus": 9, "visible_gpus": 8, "missing": ["0000:c1:00.0"],
                                      "vendors": {"nvidia": {"host": 9, "visible": 8, "missing": ["0000:c1:00.0"]}}}
    for bad in (None, [], {"vendors": []}, {"vendors": {"nvidia": {"host": "9", "visible": 8}}},
                {"vendors": {"nvidia": {"host": 9, "visible": -1}}}, {"vendors": {"nvidia": 3}}):
        assert parse_gpu_census(bad) is None, bad
    assert parse_gpu_census({"vendors": {"intel": {"host": 2, "visible": 0}}})["vendors"] == {}


# ------------------------------------------------------------------ what the server records
def inventory(fixture: str = "nvidia-a100x8", *, drop: tuple[int, ...] = (), add: int = 0, ok: bool = True,
              swap: tuple[int, ...] = ()) -> InventoryIn:
    data = json.loads((SIM / f"{fixture}.json").read_text())
    cards = [c for c in data["accelerators"] if c["vendor"] != "cpu"]
    cpu = [c for c in data["accelerators"] if c["vendor"] == "cpu"]
    out = []
    for c in cards:
        if c["index"] in drop:
            continue
        c = copy.deepcopy(c)
        if c["index"] in swap:
            c["key"] = c["key"] + "-new"
            c["uuid"] = (c.get("uuid") or "") + "-new"
        out.append(c)
    for i in range(add):
        extra = copy.deepcopy(cards[0])
        n = len(cards) + i
        extra.update(key=f"nvidia:GPU-added-{n}", uuid=f"GPU-added-{n}", index=n)
        extra["detail"] = {**extra["detail"], "pci_bus_id": f"00000000:{0xc1 + i:02X}:00.0"}
        out.append(extra)
    collectors = copy.deepcopy(data["collectors"])
    if not ok:
        collectors["nvidia"] = {"ok": False, "count": 0, "source": "nvml", "reason": "NVML: Driver Not Loaded"}
        out = []
    return InventoryIn.model_validate({"accelerators": out + cpu, "collectors": collectors})


def report(nid: int, inv: InventoryIn) -> None:
    db = new_session()
    try:
        n = db.get(Node, nid)
        store.apply_inventory(db, n, inv)
        db.commit()
    finally:
        db.close()


def info(nid: int) -> dict:
    db = new_session()
    try:
        return dict(db.get(Node, nid).runtime_info or {})
    finally:
        db.close()


def audits() -> list[AuditEvent]:
    db = new_session()
    try:
        return list(db.scalars(select(AuditEvent).where(AuditEvent.action == "node.gpus_changed")
                               .order_by(AuditEvent.id)).all())
    finally:
        db.close()


def fresh(name: str = "gpu-01", **fields) -> int:
    """A machine whose runtime info knows no GPUs yet (as before 1.5.4), then its first report."""
    nid = node(name, runtime_info={}, **fields)
    report(nid, inventory())
    return nid


def test_fy_gpuch_005_first_report_sets_then_added_removed_swapped(owner):
    """FY-GPUCH-005: the first report only sets what is known; a GPU added is a change (runtime info, audit with model
    and memory); renumbered cards are none; a swapped card (same index, new UUID) is one removed and one added; changes
    keep the last MAX_CHANGES."""
    nid = fresh()
    assert gpu_changes.changes_of(info(nid)) == [] and len(info(nid)["gpu_set"]) == 8 and audits() == []
    report(nid, inventory())
    assert audits() == []
    report(nid, inventory(add=1))
    [c] = gpu_changes.changes_of(info(nid))
    assert [g["index"] for g in c["added"]] == [8] and c["removed"] == [] and c["gpus"] == 9
    assert c["added"][0]["model"] == "NVIDIA A100 80GB PCIe" and c["added"][0]["vram_total"] == 80 * GIB
    [a] = audits()
    assert (a.actor_name, a.target) == ("agent", "gpu-01")
    assert a.detail["added"][0]["key"] == "nvidia:GPU-added-8" and a.detail["gpus"] == 9
    report(nid, inventory(add=1, swap=(3,)))
    last = gpu_changes.changes_of(info(nid))[-1]
    assert [g["index"] for g in last["added"]] == [3] and [g["index"] for g in last["removed"]] == [3]
    assert last["added"][0]["key"].endswith("-new") and len(audits()) == 2
    renumbered = inventory(add=1, swap=(3,))
    for i, card in enumerate(reversed([x for x in renumbered.accelerators if x.vendor == "nvidia"])):
        card.index = i
    report(nid, renumbered)
    assert len(audits()) == 2
    for i in range(gpu_changes.MAX_CHANGES + 3):
        report(nid, inventory(add=1 + i % 2, swap=(3,)))
    assert len(gpu_changes.changes_of(info(nid))) == gpu_changes.MAX_CHANGES


def test_fy_gpuch_006_a_failed_collector_and_a_reconnect_change_nothing(owner):
    """FY-GPUCH-006: when NVML fails nothing counts as removed (and nothing is added when it works again); the known
    GPUs survive the next hello, which replaces the rest of the runtime info; a key carried over to a new name is no
    change."""
    nid = fresh()
    report(nid, inventory(ok=False))
    assert audits() == [] and len(info(nid)["gpu_set"]) == 8
    report(nid, inventory())
    assert audits() == []
    report(nid, inventory(add=1))
    db = new_session()
    try:
        n = db.get(Node, nid)
        msg = hello(inventory=inventory(add=1).model_dump(exclude_none=True), runtime_info={"process": {"ok": True}})
        store.apply_hello(db, n, HelloIn.model_validate(msg), "10.0.0.9")
        db.commit()
    finally:
        db.close()
    assert len(info(nid)["gpu_set"]) == 9 and len(gpu_changes.changes_of(info(nid))) == 1
    assert len(audits()) == 1
    amd = node("apu-01", "amd-gfx1151-apu", runtime_info={})
    report(amd, inventory("amd-gfx1151-apu"))
    data = json.loads((SIM / "amd-gfx1151-apu.json").read_text())
    card = copy.deepcopy(data["accelerators"][0])
    card["detail"]["aliases"] = [card["key"]]
    card["key"] = "amd:renamed"
    report(amd, InventoryIn.model_validate({"accelerators": [card, data["accelerators"][1]],
                                            "collectors": data["collectors"]}))
    assert list(info(amd)["gpu_set"]) == ["amd:renamed"] and len(audits()) == 1


# ------------------------------------------------------------------ Setup health
def test_fy_gpuch_007_the_host_has_gpus_the_agent_does_not_see(owner):
    """FY-GPUCH-007: an online machine whose host has more GPUs than its agent sees: a warning naming the counts and
    the PCI address, linking to the update command; for an agent container, re-create it; otherwise restart and check
    the driver. Nothing when they agree, without a census (an older agent), or offline."""
    census = {"host_gpus": 9, "visible_gpus": 8, "missing": ["0000:c1:00.0"],
              "vendors": {"nvidia": {"host": 9, "visible": 8, "missing": ["0000:c1:00.0"]}}}
    nid = node("gpu-01", runtime_info={"gpu_census": census, "install": {"method": "container", "known": True}})
    [f] = findings()["machine-gpus-hidden"]
    assert (f.severity, f.snoozable, f.scope) == ("warning", True, f"machine:{nid}")
    assert f.pages == ("machines", f"machine:{nid}")
    assert f.title == "gpu-01: the host has 9 NVIDIA GPUs but the agent sees 8"
    assert f.fix_url == f"/machines/{nid}#update" and f.fix_label == "Update command"
    for part in ("PCI 0000:c1:00.0", "Re-create it with the Update the agent command", "allow the new GPU there",
                 "snooze this"):
        assert part in f.detail, part
    edit(Node, nid, runtime_info={"gpu_census": census, "install": {"method": "install.sh", "known": True}})
    [f] = findings()["machine-gpus-hidden"]
    assert "Restart the agent" in f.detail and "Re-create" not in f.detail
    edit(Node, nid, status="offline")
    assert "machine-gpus-hidden" not in findings()
    even = {**census, "visible_gpus": 9, "missing": [], "vendors": {"nvidia": {"host": 9, "visible": 9, "missing": []}}}
    edit(Node, nid, status="online", runtime_info={"gpu_census": even})
    assert "machine-gpus-hidden" not in findings()
    edit(Node, nid, runtime_info={})
    assert "machine-gpus-hidden" not in findings()


def test_fy_gpuch_008_a_new_gpu_and_the_gpu_access_list(owner):
    """FY-GPUCH-008: with a GPU access list a new GPU stays not allowed (placement does not use it) and Setup health
    asks to allow it, linking to GPU access; once it is on the list the finding goes. Without a list placement uses
    it at once and the finding is a note. Older than 7 days, or while the GPU is not reported: nothing."""
    nid = fresh(allowlist=None)
    first = [gpu(nid, i).key for i in range(2)]
    edit(Node, nid, gpu_allowlist=first)
    report(nid, inventory(add=1))
    db = new_session()
    try:
        n = db.get(Node, nid)
        new = next(a for a in n.accelerators if a.key == "nvidia:GPU-added-8")
        assert placement.is_allowed(n, new) is False
    finally:
        db.close()
    [f] = findings()["machine-gpu-not-allowed"]
    assert f.title == "New GPU on gpu-01: allow GPU #8?" and f.severity == "warning"
    assert f.fix_url == f"/machines/{nid}#gpu-access" and f.fix_label == "Choose GPUs"
    assert "GPU #8 (NVIDIA A100 80GB PCIe, 80 GiB) appeared on gpu-01" in f.detail and "tick GPU #8" in f.detail
    assert "machine-gpu-added" not in findings()
    edit(Node, nid, gpu_allowlist=first + ["nvidia:GPU-added-8"])
    assert "machine-gpu-not-allowed" not in findings() and "machine-gpu-added" not in findings()
    edit(Node, nid, gpu_allowlist=None)
    [f] = findings()["machine-gpu-added"]
    assert (f.severity, f.title) == ("info", "New GPU on gpu-01 is in use")
    assert "no GPU access list, so placement already uses it" in f.detail
    db = new_session()
    try:
        n = db.get(Node, nid)
        assert placement.is_allowed(n, next(a for a in n.accelerators if a.key == "nvidia:GPU-added-8"))
    finally:
        db.close()
    assert "machine-gpu-added" not in findings(now=utcnow() + advisor.GPU_CHANGE_RECENT + timedelta(minutes=1))
    report(nid, inventory(ok=False))   # not reported now (NVML failed): nothing to say about it meanwhile
    assert "machine-gpu-added" not in findings() and len(audits()) == 1


def test_fy_gpuch_009_a_removed_gpu_names_the_deployments_placed_on_it(owner):
    """FY-GPUCH-009: a GPU that stops being reported: a warning naming the deployments with a replica placed on it
    (also in the audit entry, not in GET /api/machines), which keep their place until restarted; none named when
    nothing was placed there; gone once the GPU is back (a GPU back within the window is not "new" either)."""
    from tests.test_advisor import deployment

    nid = fresh()
    deployment("chat-a", node_id=nid, replica={"ordinal": 0, "state": "healthy",
                                               "gpus": [{"key": gpu(nid, 3).key, "index": 3}]})
    report(nid, inventory(drop=(3,)))
    [f] = findings()["machine-gpu-removed"]
    assert f.title == "gpu-01 no longer reports GPU #3" and f.severity == "warning"
    assert "Placed on it: chat-a" in f.detail and "keeps its place there" in f.detail
    assert "Restart that deployment" in f.detail and f.fix_url == f"/machines/{nid}"
    [a] = audits()
    assert a.detail["removed"][0]["deployments"] == ["chat-a"]
    public = machine(owner, nid)["runtime_info"]
    assert "gpu_set" not in public and "deployments" not in public["gpu_changes"][0]["removed"][0]
    assert public["gpu_changes"][0]["removed"][0]["index"] == 3
    report(nid, inventory())
    assert "machine-gpu-removed" not in findings() and "machine-gpu-added" not in findings()
    report(nid, inventory(drop=(5,)))
    [f] = findings()["machine-gpu-removed"]
    assert "No deployment was placed on it." in f.detail


def test_fy_gpuch_010_through_a_real_hello(owner):
    """FY-GPUCH-010: end to end over the agent's websocket: a machine's first hello records no change; reconnecting
    with a ninth GPU does (audit node.gpus_changed, the machine's gpu_changes) and its census reaches the API."""
    j = joined(owner, "gpu-01")
    with connect(owner, j["credential"]) as ws:
        ws.send_json(hello())
        assert ws.receive_json()["type"] == "welcome"
    assert audits() == [] and "gpu_changes" not in machine(owner, j["node_id"])["runtime_info"]
    inv = inventory(add=1)
    census = {"host_gpus": 9, "visible_gpus": 9, "missing": [],
              "vendors": {"nvidia": {"host": 9, "visible": 9, "missing": []}}}
    with connect(owner, j["credential"]) as ws:
        ws.send_json(hello(inventory=inv.model_dump(exclude_none=True), runtime_info={"gpu_census": census}))
        assert ws.receive_json()["type"] == "welcome"
    m = machine(owner, j["node_id"])
    assert m["runtime_info"]["gpu_census"] == census
    assert [g["index"] for g in m["runtime_info"]["gpu_changes"][0]["added"]] == [8]
    assert len(audits()) == 1
    rows = owner.get("/api/audit?limit=20").json()["events"]
    assert any(e["action"] == "node.gpus_changed" and e["target"] == "gpu-01" for e in rows)


@pytest.mark.parametrize("change", [{"at": "nonsense", "added": [{"key": "x"}]}, "nonsense"])
def test_fy_gpuch_011_a_malformed_record_is_skipped(owner, change):
    """FY-GPUCH-011: a record that cannot be read (a bad time, not an object) is skipped by Setup health and the API."""
    nid = node("gpu-01", runtime_info={"gpu_changes": [change]})
    assert not {"machine-gpu-added", "machine-gpu-removed", "machine-gpu-not-allowed"} & set(findings())
    assert owner.get("/api/machines").status_code == 200
    shown = machine(owner, nid)["runtime_info"]["gpu_changes"]
    assert shown == [] if change == "nonsense" else [c["at"] for c in shown] == ["nonsense"]
