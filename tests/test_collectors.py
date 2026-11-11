"""Inventory collectors on recorded and representative tool output (FY-COL-*)."""

import json
import os
import sys
from pathlib import Path

import pytest

from forgeyard.agent.inventory import Inventory
from forgeyard.agent.inventory.amd import (ROCM_SMI, AmdCollector, amdsmi_uuid, decode_gfx, fill_from_kfd, gaps,
                                           import_amdsmi, pair_up, parse_amd_smi, parse_rocm_smi, parse_version,
                                           pick_model, read_cpu_model, read_kfd, read_rocm_version, reading,
                                           rocm_roots, unique_uuid, uuid_serial)
from forgeyard.agent.inventory.base import Result
from forgeyard.agent.inventory.cpu import CpuCollector, parse_cpuinfo, parse_meminfo
from forgeyard.agent.inventory.nvidia import NvidiaCollector
from tests.fakes import FIXTURES, FakeAmdsmi, FakeNvml, fake_runner, kfd_location, load, make_kfd

GiB, MiB = 1024**3, 1024**2
A100_FREE = [4023779328, 7219380224, 6169165824, 9480110080, 48294330368, 15321071616, 9689825280, 7716405248]


def no_tools(which=None):
    return dict(run=fake_runner({}), which=which or (lambda name: None), kfd_root="/nonexistent", rocm_root=[],
                cpuinfo="/nonexistent")


# ------------------------------------------------------------------ NVIDIA
def test_fy_col_001_nvml_8x_a100_as_recorded():
    """FY-COL-001: the 8 x A100 80GB PCIe host: name, arch 8.0, 80 GiB total, free exactly as NVML said."""
    nv = FakeNvml(load("nvml_a100x8.json"))
    r = NvidiaCollector(nvml=nv).inventory()
    assert r.ok and r.source == "nvml" and r.reason is None and len(r.accelerators) == 8
    assert {a["model"] for a in r.accelerators} == {"NVIDIA A100 80GB PCIe"}
    assert {a["arch"] for a in r.accelerators} == {"8.0"}
    assert {a["detail"]["sm"] for a in r.accelerators} == {"sm_80"}
    assert {a["detail"]["cuda"] for a in r.accelerators} == {"13.0"}
    assert {a["driver"] for a in r.accelerators} == {"580.178.04"}
    assert [a["vram_total"] for a in r.accelerators] == [80 * GiB] * 8
    assert [a["vram_free"] for a in r.accelerators] == A100_FREE
    assert r.accelerators[0]["vram_used"] // MiB == 77315 and r.accelerators[0]["detail"]["vram_reserved"] == 767 * MiB
    assert [a["index"] for a in r.accelerators] == list(range(8))
    assert all(a["key"] == f"nvidia:{a['uuid']}" and a["uuid"].startswith("GPU-") for a in r.accelerators)
    assert r.accelerators[0]["temperature"] == 36 and r.accelerators[0]["utilization"] == 0
    assert not any(a["unified_memory"] for a in r.accelerators)


def test_fy_col_002_old_pynvml_bytes_and_no_memory_v2():
    """FY-COL-002"""
    r = NvidiaCollector(nvml=FakeNvml(load("nvml_rtx4090x2.json"), bytes_strings=True, v2=False)).inventory()
    a, b = r.accelerators
    assert (a["model"], a["arch"], a["driver"]) == ("NVIDIA GeForce RTX 4090", "8.9", "550.54.14")
    assert isinstance(a["uuid"], str) and a["uuid"].startswith("GPU-")
    assert b["vram_free"] == 25757220864 - 20401094656 and b["vram_used"] == 20401094656
    assert "cuda" not in a["detail"] and a["detail"]["pci_bus_id"] == "00000000:01:00.0"


def test_fy_col_003_nvml_missing_or_without_driver_reports_why():
    """FY-COL-003: fail soft, never raise."""
    class NoModule(NvidiaCollector):
        def _start(self):
            import importlib
            try:
                importlib.import_module("pynvml_definitely_missing")
            except ImportError:
                return None, "nvidia-ml-py is not installed (uv sync --extra nvidia)"
    r = NoModule().inventory()
    assert not r.ok and r.accelerators == [] and "nvidia-ml-py" in r.reason
    r = NvidiaCollector(nvml=FakeNvml(load("nvml_a100x8.json"), init_error="NVML Shared Library Not Found")).inventory()
    assert not r.ok and "NVML did not start" in r.reason and "Shared Library" in r.reason


def test_fy_col_004_a_lost_gpu_is_skipped_and_named():
    """FY-COL-004"""
    data = load("nvml_a100x8.json")
    data["devices"][3]["lost"] = True
    data["devices"][5]["memory"] = None  # memory info NOT_SUPPORTED
    r = NvidiaCollector(nvml=FakeNvml(data)).inventory()
    assert r.ok and len(r.accelerators) == 7 and "GPU 3" in r.reason
    no_mem = next(a for a in r.accelerators if a["index"] == 5)
    assert no_mem["vram_total"] is None and no_mem["model"] == "NVIDIA A100 80GB PCIe"


def test_fy_col_005_nvml_metrics_and_read_only_calls():
    """FY-COL-005: heartbeats re-read memory/util/temp; only nvmlInit / Get* / Shutdown are ever called."""
    data = load("nvml_a100x8.json")
    nv = FakeNvml(data)
    c = NvidiaCollector(nvml=nv)
    c.inventory()
    data["devices"][0]["memory"]["free"] = 1234
    m = c.metrics()
    assert len(m) == 8 and m[0]["vram_free"] == 1234 and set(m[0]) == {"key", "vram_free", "vram_used", "utilization",
                                                                         "temperature"}
    c.close()
    assert set(nv.calls) <= {n for n in nv.calls if n.startswith(FakeNvml.READ_ONLY)}
    with pytest.raises(AssertionError):
        nv.nvmlDeviceSetPersistenceMode  # the fake refuses anything that is not read-only


# ------------------------------------------------------------------ AMD: amdsmi library
def test_fy_col_010_amdsmi_strix_halo_gfx1151_is_unified():
    """FY-COL-010: gfx1151 APU: arch from target_graphics_version, unified memory, pool = the larger (GTT)."""
    data = load("amdsmi_gfx1151_apu.json")
    data["rocm_version"] = "6.4.1"
    lib = FakeAmdsmi(data)
    r = AmdCollector(amdsmi=lib, **no_tools()).inventory()
    assert r.ok and r.source == "amdsmi"
    (gpu,) = r.accelerators
    assert (gpu["model"], gpu["arch"], gpu["unified_memory"], gpu["driver"]) == ("AMD Radeon 8060S", "gfx1151", True, "6.4.1")
    assert gpu["detail"]["amdgpu_driver"] == "6.14.0"  # the kernel driver's version, not ROCm's
    assert gpu["vram_total"] == 110 * GiB and gpu["vram_used"] == 6 * GiB and gpu["vram_free"] == 104 * GiB
    assert gpu["detail"]["vram_carveout"] == 512 * MiB and gpu["detail"]["pool"] == "gtt"
    assert gpu["detail"]["unified_by"] == "gfx1151 is an APU target"
    assert (gpu["utilization"], gpu["temperature"]) == (3, 47)
    assert all(c.startswith("amdsmi_get_") or c in ("amdsmi_init", "amdsmi_shut_down") for c in lib.calls)


def test_fy_col_011_amdsmi_two_discrete_cards():
    """FY-COL-011: 2 x 7900 XTX: VRAM is the pool, millidegrees and N/A handled."""
    r = AmdCollector(amdsmi=FakeAmdsmi(load("amdsmi_gfx1100x2.json")), **no_tools()).inventory()
    a, b = r.accelerators
    assert [x["arch"] for x in r.accelerators] == ["gfx1100", "gfx1100"]
    assert not a["unified_memory"] and a["vram_total"] == 25753026560 and a["vram_free"] == 25753026560 - 18 * GiB
    assert a["detail"]["gtt_total"] == 64 * GiB and "pool" not in a["detail"]
    assert a["key"] != b["key"] and b["utilization"] is None and b["temperature"] == 52


def test_fy_col_012_kfd_marks_an_apu_whose_target_is_not_apu_only(tmp_path):
    """FY-COL-012: MI300A shares gfx942 with the discrete MI300X; the KFD node with CPU cores tells them apart."""
    data = load("amdsmi_gfx1100x2.json")
    data["handles"] = data["handles"][:1]
    data["handles"][0]["asic_info"]["target_graphics_version"] = "gfx942"
    root = make_kfd(tmp_path / "nodes", [
        {"cpu_cores_count": 24, "simd_count": 912, "gfx_target_version": 90402, "location_id": kfd_location(0x03),
         "domain": 0}])
    r = AmdCollector(amdsmi=FakeAmdsmi(data), run=fake_runner({}), which=lambda n: None, kfd_root=root).inventory()
    (gpu,) = r.accelerators
    assert gpu["unified_memory"] and gpu["detail"]["unified_by"].startswith("kfd")
    assert gpu["detail"]["pool"] == "gtt"  # 64 GiB GTT > 24 GiB VRAM in this fixture


def test_fy_col_013_kfd_topology_parsing(tmp_path):
    """FY-COL-013"""
    root = make_kfd(tmp_path / "nodes", [
        {"cpu_cores_count": 16, "simd_count": 0},
        {"cpu_cores_count": 0, "simd_count": 80, "gfx_target_version": 110501, "location_id": kfd_location(0xc5), "domain": 0},
        {"simd_count": 192, "gfx_target_version": 90010, "location_id": kfd_location(0x83, 1, 0), "domain": 1,
         "unique_id": 11400983651823780603, "drm_render_minor": 129}])
    kfd = read_kfd(root)
    assert kfd == {"0000:c5:00.0": {"node": "1", "gfx": "gfx1151", "cpu_cores": 0, "simd": 80, "unique_id": None,
                                    "render_minor": None},
                   "0001:83:01.0": {"node": "2", "gfx": "gfx90a", "cpu_cores": 0, "simd": 192,
                                    "unique_id": 11400983651823780603, "render_minor": 129}}
    assert read_kfd(tmp_path / "missing") == {}


@pytest.mark.parametrize("value, gfx", [("gfx1151", "gfx1151"), ("gfx90a:sramecc+:xnack-", "gfx90a"), (110501, "gfx1151"),
                                        ("90402", "gfx942"), (90010, "gfx90a"), (0, None), ("N/A", None), (None, None)])
def test_fy_col_014_decode_gfx(value, gfx):
    """FY-COL-014"""
    assert decode_gfx(value) == gfx


def test_fy_col_015_kfd_supplies_gfx_when_amdsmi_does_not(tmp_path):
    """FY-COL-015: older amdsmi without target_graphics_version."""
    data = load("amdsmi_gfx1151_apu.json")
    del data["handles"][0]["asic_info"]["target_graphics_version"]
    root = make_kfd(tmp_path / "nodes", [{"simd_count": 80, "gfx_target_version": 110501,
                                          "location_id": kfd_location(0xc5), "domain": 0}])
    r = AmdCollector(amdsmi=FakeAmdsmi(data), run=fake_runner({}), which=lambda n: None, kfd_root=root).inventory()
    assert r.accelerators[0]["arch"] == "gfx1151" and r.accelerators[0]["unified_memory"]


# ------------------------------------------------------------------ AMD: CLIs
def _amd_smi(static, metric, listing=None):
    out = {"amd-smi static --asic --bus --vram --driver --board --json": (0, (FIXTURES / static).read_text(), ""),
           "amd-smi metric --mem-usage --usage --temperature --json": (0, (FIXTURES / metric).read_text(), "")}
    if listing:
        out["amd-smi list --json"] = (0, (FIXTURES / listing).read_text(), "")
    return fake_runner(out)


class _NoLib(AmdCollector):
    def __init__(self, **kw):
        kw.setdefault("rocm_root", [])
        kw.setdefault("cpuinfo", "/nonexistent")
        super().__init__(**kw)

    def _load_lib(self):
        return None, "amdsmi Python library not importable (No module named 'amdsmi')"


def test_fy_col_020_amd_smi_cli_gpu_data_format_gfx1151():
    """FY-COL-020: no amdsmi library: amd-smi --json ({"gpu_data": [...]}, values {"value","unit"})."""
    run = _amd_smi("amd_smi_static_gfx1151.json", "amd_smi_metric_gfx1151.json", "amd_smi_list_gfx1151.json")
    r = _NoLib(run=run, which=lambda n: "/opt/rocm/bin/amd-smi" if n == "amd-smi" else None, kfd_root="/nonexistent").inventory()
    assert r.ok and r.source == "amd-smi" and "amdsmi Python library not importable" in r.reason
    (gpu,) = r.accelerators
    assert (gpu["arch"], gpu["unified_memory"], gpu["model"]) == ("gfx1151", True, "AMD Radeon 8060S")
    assert gpu["vram_total"] == 112640 * MiB and gpu["vram_free"] == 106496 * MiB
    assert gpu["uuid"] == "00ff1586-0000-1000-80c5-000000000000" and gpu["temperature"] == 47


def test_fy_col_021_amd_smi_cli_bare_list_format_two_cards():
    """FY-COL-021: older amd-smi prints a bare list; the selective flags fail, the plain command works."""
    outputs = {"amd-smi static --json": (0, (FIXTURES / "amd_smi_static_gfx1100x2.json").read_text(), ""),
               "amd-smi metric --json": (0, (FIXTURES / "amd_smi_metric_gfx1100x2.json").read_text(), "")}
    run = fake_runner(outputs)
    r = _NoLib(run=run, which=lambda n: "/usr/bin/amd-smi" if n == "amd-smi" else None, kfd_root="/nonexistent").inventory()
    a, b = r.accelerators
    assert (a["arch"], a["vram_total"], a["vram_used"], a["temperature"]) == ("gfx1100", 24560 * MiB, 18432 * MiB, 61)
    assert b["temperature"] == 52 and b["utilization"] is None and not b["unified_memory"]
    assert a["key"] == "amd:0000:03:00.0"  # no uuid from `amd-smi list`: the PCI address is the key
    assert "amd-smi static --asic --bus --vram --driver --board --json" in run.seen


def test_fy_col_022_rocm_smi_fallback():
    """FY-COL-022: neither amdsmi nor amd-smi: rocm-smi --json, keys matched case-insensitively."""
    run = fake_runner({" ".join(ROCM_SMI): (2, (FIXTURES / "rocm_smi_gfx1100x2.json").read_text(), "WARNING: one field unsupported")})
    r = _NoLib(run=run, which=lambda n: "/usr/bin/rocm-smi" if n == "rocm-smi" else None, kfd_root="/nonexistent").inventory()
    assert r.ok and r.source == "rocm-smi"
    a, b = r.accelerators
    assert a["model"] == b["model"] == "Navi 31 [Radeon RX 7900 XT/7900 XTX]"
    assert (a["arch"], a["driver"], a["utilization"], a["temperature"]) == ("gfx1100", None, 88, 61)
    assert a["detail"]["amdgpu_driver"] == "6.10.5" and "driver" in a["detail"]["missing"]
    assert a["vram_free"] == 25753026560 - 18 * GiB and b["uuid"] is None and b["key"] == "amd:0000:83:00.0"
    assert "amd-smi not on PATH" in r.reason


def test_fy_col_023_no_amd_tools_is_a_reason_not_a_crash():
    """FY-COL-023"""
    r = _NoLib(**no_tools()).inventory()
    assert not r.ok and r.accelerators == []
    assert r.reason == ("amdsmi Python library not importable (No module named 'amdsmi'); amd-smi not on PATH; "
                        "rocm-smi not on PATH")


def test_fy_col_024_broken_tool_output_is_contained():
    """FY-COL-024: garbage from amd-smi, an amdsmi_init failure: still no exception."""
    run = fake_runner({"amd-smi static --asic --bus --vram --driver --board --json": (0, "Segmentation fault", ""),
                       "amd-smi static --json": (1, "", "AMDSMI_STATUS_DRM_ERROR")})
    r = AmdCollector(amdsmi=FakeAmdsmi({"handles": []}, init_error="AMDSMI_STATUS_INIT_ERROR"), run=run,
                     which=lambda n: "/x/" + n if n == "amd-smi" else None, kfd_root="/nonexistent").inventory()
    assert not r.ok and "amdsmi_init failed" in r.reason and "AMDSMI_STATUS_DRM_ERROR" in r.reason


def test_fy_col_025_amd_metrics_follow_the_backend():
    """FY-COL-025"""
    data = load("amdsmi_gfx1151_apu.json")
    c = AmdCollector(amdsmi=FakeAmdsmi(data), **no_tools())
    c.inventory()
    data["handles"][0]["memory"]["GTT"][1] = 10 * GiB
    (m,) = c.metrics()
    assert m["vram_used"] == 10 * GiB and m["vram_free"] == 100 * GiB
    run = _amd_smi("amd_smi_static_gfx1151.json", "amd_smi_metric_gfx1151.json")
    cli = _NoLib(run=run, which=lambda n: "/x" if n == "amd-smi" else None, kfd_root="/nonexistent")
    cli.inventory()
    assert cli.metrics()[0]["vram_free"] == 106496 * MiB


def test_fy_col_026_parsers_ignore_junk():
    """FY-COL-026"""
    assert parse_amd_smi(None, None, None, {}) == [] and parse_amd_smi({"gpu_data": "x"}, [], [], {}) == []
    assert parse_rocm_smi("nope", {}) == [] and parse_rocm_smi({"system": {}}, {}) == []


# ------------------------------------------------------------------ AMD: a real Strix Halo (2026-11-11)
# Ryzen AI MAX+ 395 / Radeon 8060S (gfx1151), ROCm 7.0.2, amd-smi 26.0.2, 96 GiB BIOS carve-out, GTT
# 15.5 GiB. The JSON is what the tools printed there; kfd.json and cpuinfo.txt are rebuilt from the
# agent's own --raw reading of the same machine. The amd-smi and rocm-smi readings were taken a
# few minutes apart, so their used/free numbers differ.
REAL = "strix-halo-gfx1151-amdsmi26"
REAL_UUID = "00ff1586-0000-1000-8000-000000000000"
REAL_KEY = "amd:0000:c6:00.0"


def _real(name):
    return (FIXTURES / REAL / name).read_text()


def _real_machine(tmp_path, version="7.0.2-56"):
    """KFD tree, a ROCm whose .info/version says `version` (None: no file), and the CPU's cpuinfo."""
    kfd = make_kfd(tmp_path / "nodes", load(f"{REAL}/kfd.json")["nodes"])
    rocm = tmp_path / "rocm"
    (rocm / ".info").mkdir(parents=True)
    if version:
        (rocm / ".info" / "version").write_text(version + "\n")
    return dict(kfd_root=kfd, rocm_root=[rocm], cpuinfo=FIXTURES / REAL / "cpuinfo.txt")


def _real_amd_smi():
    return fake_runner({"amd-smi static --asic --bus --vram --driver --board --json": (0, _real("amd-smi-static.json"), ""),
                        "amd-smi metric --mem-usage --usage --temperature --json": (0, _real("amd-smi-metric-sel.json"), ""),
                        "amd-smi list --json": (0, _real("amd-smi-list.json"), "")})


def _assert_real_card(gpu, source):
    assert (gpu["vendor"], gpu["index"], gpu["arch"], gpu["unified_memory"]) == ("amd", 0, "gfx1151", True)
    assert gpu["model"] == "AMD Radeon 8060S"  # market name "AMD Radeon Graphics" + the CPU's "w/ Radeon 8060S"
    assert gpu["driver"] == "7.0.2"  # ROCm, not the kernel's 7.0.0-38
    assert gpu["key"] == REAL_KEY and gpu["uuid"] is None  # no serial: the UUID is the same on every Strix Halo
    d = gpu["detail"]
    assert d["shared_uuid"] == REAL_UUID and {"amd:0x0", f"amd:{REAL_UUID}"} <= set(d["aliases"])
    assert REAL_KEY not in d["aliases"]
    assert gpu["vram_total"] == 96 * GiB == 103079215104  # the carve-out, bigger than GTT
    assert (d["pool"], d["vram_carveout"], d["unified_by"]) == ("vram", 96 * GiB, "gfx1151 is an APU target")
    assert (d["source"], d["pci_bus_id"], d["render_minor"]) == (source, "0000:c6:00.0", 128)
    assert "rocr_uuid" not in d  # KFD unique_id is 0 there


def test_fy_col_050_real_strix_halo_through_amd_smi_26(tmp_path):
    """FY-COL-050: amd-smi 26 as printed on the real machine: "N/A" sections ("usage": "N/A"),
    {"gpu_data": [...]} for static/metric but a bare list for `list`, sizes in MB = MiB."""
    run = _real_amd_smi()
    r = _NoLib(run=run, which=lambda n: "/opt/rocm/bin/" + n, **_real_machine(tmp_path)).inventory()
    assert r.ok and r.source == "amd-smi", r.reason
    (gpu,) = r.accelerators
    _assert_real_card(gpu, "amd-smi")
    assert gpu["vram_used"] == 64957 * MiB and gpu["vram_free"] == 33347 * MiB
    assert gpu["detail"]["gtt_total"] == 15862 * MiB and gpu["detail"]["gtt_used"] == 764 * MiB
    assert (gpu["temperature"], gpu["utilization"]) == (31, None)
    assert gpu["detail"]["missing"] == ["utilization"]
    assert gpu["detail"]["amdgpu_driver"].startswith("Linuxversion7.0.0-38-generic")
    assert "amd-smi version --json" not in run.seen  # ROCm's version file was enough


def test_fy_col_051_real_strix_halo_full_metric_and_the_cli_version(tmp_path):
    """FY-COL-051: the plain `amd-smi metric --json` (every section, mostly "N/A"); no version file,
    so the ROCm version comes from `amd-smi version --json`."""
    version = json.dumps([{"tool": "AMDSMI Tool", "version": "26.0.2+unknown", "amdsmi_library_version": "26.0.2",
                           "rocm_version": "7.0.2", "amdgpu_version": "N/A"}])
    seen_run = fake_runner({"amd-smi static --asic --bus --vram --driver --board --json": (0, _real("amd-smi-static.json"), ""),
                            "amd-smi metric --json": (0, _real("amd-smi-metric.json"), ""),
                            "amd-smi list --json": (0, _real("amd-smi-list.json"), ""),
                            "amd-smi version --json": (0, version, "")})
    r = _NoLib(run=seen_run, which=lambda n: "/opt/rocm/bin/" + n, **_real_machine(tmp_path, version=None)).inventory()
    (gpu,) = r.accelerators
    _assert_real_card(gpu, "amd-smi")
    assert gpu["vram_free"] == 33347 * MiB and gpu["temperature"] == 31
    assert "amd-smi metric --mem-usage --usage --temperature --json" in seen_run.seen  # tried first, then the plain one


def test_fy_col_052_real_strix_halo_through_rocm_smi(tmp_path):
    """FY-COL-052: rocm-smi as printed there ("Card Series": "N/A", "Unique ID": "0x0", the kernel as
    "Driver version"): the same key as amd-smi, and the 1.1.0 key "amd:0x0" as an alias."""
    raw = _real("rocm-smi.json")
    run = fake_runner({" ".join(ROCM_SMI): (0, raw, "")})
    r = _NoLib(run=run, which=lambda n: "/opt/rocm/bin/rocm-smi" if n == "rocm-smi" else None,
               **_real_machine(tmp_path)).inventory()
    assert r.ok and r.source == "rocm-smi"
    (gpu,) = r.accelerators
    _assert_real_card(gpu, "rocm-smi")
    assert gpu["vram_used"] == 63433625600 and gpu["vram_free"] == 39645589504
    assert (gpu["utilization"], gpu["temperature"]) == (4, 41) and gpu["detail"]["amdgpu_driver"] == "7.0.0-38-generic"
    assert "missing" not in gpu["detail"]
    old = load(f"{REAL}/agent-1.1.0-inventory.json")["accelerators"][0]
    assert (old["key"], old["model"], old["driver"]) == ("amd:0x0", "N/A", "7.0.0-38-generic")  # what 1.1.0 said
    assert old["key"] in gpu["detail"]["aliases"]


def test_fy_col_053_real_strix_halo_the_chain_as_it_ran_there(tmp_path):
    """FY-COL-053: no amdsmi in the agent's Python, amd-smi and rocm-smi both there (the 1.1.0 image
    crashed in amd-smi with "'str' object has no attribute 'get'" and fell back to rocm-smi)."""
    run_all = fake_runner({
        "amd-smi static --asic --bus --vram --driver --board --json": (0, _real("amd-smi-static.json"), ""),
        "amd-smi metric --mem-usage --usage --temperature --json": (0, _real("amd-smi-metric-sel.json"), ""),
        "amd-smi list --json": (0, _real("amd-smi-list.json"), ""),
        " ".join(ROCM_SMI): (0, _real("rocm-smi.json"), "")})
    c = AmdCollector(run=run_all, which=lambda n: "/opt/rocm/bin/" + n, **_real_machine(tmp_path))
    r = c.inventory(raw=True)
    assert r.ok and r.source == "amd-smi" and r.reason.startswith("used amd-smi (amdsmi Python library not importable")
    assert "has no attribute" not in r.reason and not any("rocm-smi" in s for s in r.raw["skipped"])
    assert r.raw["rocm_version"] == "7.0.2" and r.raw["cpu_model"] == "AMD RYZEN AI MAX+ 395 w/ Radeon 8060S"
    (m,) = c.metrics()
    assert m["key"] == REAL_KEY and m["vram_free"] == 33347 * MiB


def _na_everywhere(value):
    if isinstance(value, dict):
        return {k: _na_everywhere(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_na_everywhere(v) for v in value]
    return "N/A"


def test_fy_col_054_n_a_anywhere_never_crashes(tmp_path):
    """FY-COL-054: every value "N/A", every section "N/A", every GPU entry "N/A": no exception, and
    what could not be read is listed in detail.missing."""
    static, metric, listing = (json.loads(_real(n)) for n in ("amd-smi-static.json", "amd-smi-metric.json",
                                                               "amd-smi-list.json"))
    gpu_static, gpu_metric = static["gpu_data"][0], metric["gpu_data"][0]
    # every leaf
    (card,) = parse_amd_smi(_na_everywhere(static) | {"gpu_data": [dict(_na_everywhere(gpu_static), gpu=0)]},
                            _na_everywhere(metric), _na_everywhere(listing), {})
    assert card["key"] == "amd:0" and card["model"] == "AMD GPU"
    assert card["detail"]["missing"] == ["model", "arch", "vram_total", "vram_used", "driver", "utilization",
                                         "temperature"]
    # each section of static and metric on its own, then the whole entry
    for section in [k for k in gpu_static if k != "gpu"]:
        (c,) = parse_amd_smi({"gpu_data": [{**gpu_static, section: "N/A"}]}, metric, listing, {})
        assert c["vendor"] == "amd", section
    for section in [k for k in gpu_metric if k != "gpu"]:
        parse_amd_smi(static, {"gpu_data": [{**gpu_metric, section: "N/A"}]}, listing, {})
    assert parse_amd_smi({"gpu_data": ["N/A"]}, "N/A", "N/A", {}) == []
    assert parse_amd_smi(static, {"gpu_data": "N/A"}, "N/A", {})[0]["vram_total"] == 96 * GiB  # from vram.size
    # rocm-smi: every value N/A, the system section N/A
    rocm = json.loads(_real("rocm-smi.json"))
    (c,) = parse_rocm_smi({"card0": _na_everywhere(rocm["card0"]), "system": "N/A"}, {})
    assert c["key"] == "amd:0" and c["model"] == "AMD GPU"
    assert parse_rocm_smi({"card0": "N/A"}, {})[0]["key"] == "amd:0"
    # and through the collector, with the tools printing it
    run = fake_runner({"amd-smi static --asic --bus --vram --driver --board --json":
                       (0, json.dumps({"gpu_data": [dict(_na_everywhere(gpu_static), gpu=0)]}), ""),
                       "amd-smi metric --mem-usage --usage --temperature --json": (0, json.dumps(_na_everywhere(metric)), ""),
                       "amd-smi list --json": (0, '"N/A"', "")})
    r = _NoLib(run=run, which=lambda n: "/x/" + n, **_real_machine(tmp_path)).inventory()
    assert r.ok and r.accelerators[0]["driver"] == "7.0.2"


def test_fy_col_055_amd_uuids_and_keys():
    """FY-COL-055: amdsmi's UUID is device id + serial (amdsmi_uuid_gen); without a serial it is not unique."""
    assert amdsmi_uuid(0, 0x1586) == REAL_UUID == json.loads(_real("amd-smi-list.json"))[0]["uuid"]
    serial = 0x1122334455667788
    u = amdsmi_uuid(serial, 0x744C)
    assert u == "11ff744c-0000-1000-8022-334455667788" and uuid_serial(u) == serial and unique_uuid(u)
    assert uuid_serial(REAL_UUID) == 0 and not unique_uuid(REAL_UUID)
    assert uuid_serial("00ff1586-0000-1000-80c5-000000000000") == 0xC5 << 48  # the representative fixture's
    for shared in (None, "", "0x0", "0", "00000000-0000-0000-0000-000000000000"):
        assert not unique_uuid(shared), shared
    assert unique_uuid("0x9e3c1a2b") and uuid_serial("GPU-1234") is None


@pytest.mark.parametrize("names, unified, cpu, model", [
    (["AMD Radeon Graphics", "N/A"], True, "AMD RYZEN AI MAX+ 395 w/ Radeon 8060S", "AMD Radeon 8060S"),
    (["N/A", "0x1586"], True, "AMD Ryzen 7 8845HS w/ Radeon 780M Graphics", "AMD Radeon 780M"),
    (["AMD Radeon(TM) Graphics"], True, "AMD Ryzen 5 2400G with Radeon Vega 11 Graphics", "AMD Radeon Vega 11"),
    (["AMD Radeon 8060S"], True, "AMD RYZEN AI MAX+ 395 w/ Radeon 8060S", "AMD Radeon 8060S"),
    (["AMD Radeon Graphics", "Strix Halo [Radeon 8060S]"], True, None, "Strix Halo [Radeon 8060S]"),
    (["AMD Radeon Graphics"], True, "AMD EPYC 9654 96-Core Processor", "AMD Radeon Graphics"),
    (["AMD Radeon Graphics"], False, "AMD RYZEN AI MAX+ 395 w/ Radeon 8060S", "AMD Radeon Graphics"),
    (["Radeon RX 7900 XTX"], False, None, "Radeon RX 7900 XTX"),
    (["N/A", None, "0x744c"], False, None, None),
])
def test_fy_col_056_model_name_rule(names, unified, cpu, model):
    """FY-COL-056: a specific name first; an APU with only a generic one takes the GPU from the CPU's
    brand string; a discrete card never does."""
    assert pick_model(names, unified, cpu) == model


def test_fy_col_057_rocm_version_sources(tmp_path):
    """FY-COL-057: ROCm's .info/version, then amdsmi_get_rocm_version, then `amd-smi version`."""
    assert parse_version("7.0.2-56\n") == "7.0.2" and parse_version("ROCm 6.4") == "6.4" and parse_version("N/A") is None
    machine = _real_machine(tmp_path)
    assert read_rocm_version(machine["rocm_root"]) == "7.0.2" and read_rocm_version([tmp_path / "none"]) is None
    data = load("amdsmi_gfx1151_apu.json")
    data["rocm_version"] = "6.4.1"
    lib_first = AmdCollector(amdsmi=FakeAmdsmi(data), **{**no_tools(), **machine}).inventory()
    assert lib_first.accelerators[0]["driver"] == "7.0.2"  # the file wins
    lib_only = AmdCollector(amdsmi=FakeAmdsmi(data), **no_tools()).inventory()
    assert lib_only.accelerators[0]["driver"] == "6.4.1"
    del data["rocm_version"]
    nothing = AmdCollector(amdsmi=FakeAmdsmi(data), **no_tools()).inventory().accelerators[0]
    assert nothing["driver"] is None and nothing["detail"]["missing"] == ["driver"]
    assert rocm_roots({"ROCM_PATH": "/x/rocm"}) == [Path("/x/rocm"), Path("/opt/rocm")]
    assert rocm_roots({}) == [Path("/opt/rocm")]


FAKE_AMDSMI = """
import os
print("amdsmi_wrapper chatter on stdout")
SEEN_ROCM_PATH = os.environ.get("ROCM_PATH")
from tests.fakes import FakeAmdsmi, load
_lib = FakeAmdsmi(load("amdsmi_gfx1151_apu.json"))
AmdSmiMemoryType, AmdSmiTemperatureType = _lib.AmdSmiMemoryType, _lib.AmdSmiTemperatureType
AmdSmiTemperatureMetric = _lib.AmdSmiTemperatureMetric
for _name in [n for n in dir(FakeAmdsmi) if n.startswith("amdsmi_")]:
    globals()[_name] = getattr(_lib, _name)
"""

BROKEN_AMDSMI = """
from . import amdsmi_exception  # loads; the next line does not (as amdsmi_wrapper without its library)
print("Unable to find libamd_smi.so library try installing amd-smi-lib from your package manager")
raise KeyError("libamd_smi.so")
"""


@pytest.fixture
def clean_import(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delenv("ROCM_PATH", raising=False)
    yield
    for name in [m for m in sys.modules if m == "amdsmi" or m.startswith("amdsmi.")]:
        del sys.modules[name]


def _rocm_with_amdsmi(root, source):
    pkg = root / "share" / "amd_smi" / "amdsmi"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(source)
    (pkg / "amdsmi_exception.py").write_text("class AmdSmiException(Exception):\n    pass\n")
    (root / "lib").mkdir()
    return root


def test_fy_col_058_amdsmi_from_rocms_own_copy(tmp_path, clean_import, capsys):
    """FY-COL-058: not importable from the agent's Python, so ROCm's share/amd_smi is tried, with
    ROCM_PATH pointing at that ROCm while it loads; nothing it prints reaches stdout."""
    rocm = _rocm_with_amdsmi(tmp_path / "rocm", FAKE_AMDSMI)
    c = AmdCollector(run=fake_runner({}), which=lambda n: None, kfd_root="/nonexistent", rocm_root=[tmp_path / "none", rocm],
                     cpuinfo="/nonexistent")
    r = c.inventory()
    assert r.ok and r.source == "amdsmi" and r.reason == f"amdsmi from {rocm / 'share' / 'amd_smi'}"
    assert r.accelerators[0]["arch"] == "gfx1151"
    assert sys.modules["amdsmi"].SEEN_ROCM_PATH == str(rocm) and "ROCM_PATH" not in os.environ
    assert capsys.readouterr().out == ""
    c.close()


def test_fy_col_059_a_broken_amdsmi_copy_fails_soft(tmp_path, clean_import, capsys):
    """FY-COL-059: the real wrapper prints and then fails with KeyError when libamd_smi.so is not
    found (or with SyntaxError/OSError on a mismatch): a reason, the next tool, and no trace left."""
    rocm = _rocm_with_amdsmi(tmp_path / "rocm", BROKEN_AMDSMI)
    lib, where, problem = import_amdsmi([rocm])
    assert lib is None and where is None
    assert problem.startswith("amdsmi Python library not importable")
    assert f"amdsmi from {rocm / 'share' / 'amd_smi'} failed to load: 'libamd_smi.so' (Unable to find libamd_smi.so" in problem
    assert str(rocm / "share" / "amd_smi") not in sys.path
    assert not [m for m in sys.modules if m.split(".")[0] == "amdsmi"]  # no half-loaded submodule either
    assert "ROCM_PATH" not in os.environ and capsys.readouterr().out == ""
    r = AmdCollector(run=fake_runner({}), which=lambda n: None, kfd_root="/nonexistent", rocm_root=[rocm],
                     cpuinfo="/nonexistent").inventory()
    assert not r.ok and "failed to load" in r.reason and r.reason.endswith("rocm-smi not on PATH")


# ------------------------------------------------------------------ AMD: partial answers are merged
class NoLibdrmAmdsmi(FakeAmdsmi):
    """amdsmi as it ran in the 1.1.0-based image on the real Strix Halo: libamd_smi loads and reads
    memory, but everything that goes through libdrm fails, and the C++ code says so on its own."""

    def __init__(self):
        super().__init__({"rocm_version": "7.0.2", "handles": [{
            "memory": {"VRAM": [96 * GiB, 64957 * MiB], "VIS_VRAM": [96 * GiB, 64957 * MiB], "GTT": [15862 * MiB, 764 * MiB]},
            "activity": {"gfx_activity": "N/A"}, "temperature": {"EDGE": 31}}]})

    def amdsmi_init(self, *flags):
        os.write(1, b"Fail to open libdrm.so.2: libdrm.so.2: cannot open shared object file\n")  # C++ on fd 1
        print("amdsmi python chatter")

    def amdsmi_shut_down(self):
        os.write(1, b"shutting down\n")

    def _fails(self, *a):
        os.write(1, b"Fail to open libdrm.so.2\n")
        raise RuntimeError("AMDSMI_STATUS_DRM_ERROR")

    amdsmi_get_gpu_asic_info = amdsmi_get_gpu_board_info = amdsmi_get_gpu_driver_info = _fails
    amdsmi_get_gpu_device_uuid = amdsmi_get_gpu_device_bdf = _fails


def test_fy_col_060_a_partial_amdsmi_answer_is_completed_from_kfd(tmp_path, capfd):
    """FY-COL-060: amdsmi without libdrm gives memory only. KFD (one GPU node) gives the address and
    gfx1151, which sets unified memory; the CPU names the GPU; ROCm's version file the driver. The
    library's own output never reaches stdout."""
    c = AmdCollector(amdsmi=NoLibdrmAmdsmi(), run=fake_runner({}), which=lambda n: None, **_real_machine(tmp_path))
    r = c.inventory()
    assert r.ok and r.source == "amdsmi+kfd", (r.source, r.reason)
    (gpu,) = r.accelerators
    assert (gpu["arch"], gpu["unified_memory"], gpu["model"], gpu["driver"]) == ("gfx1151", True, "AMD Radeon 8060S", "7.0.2")
    assert gpu["key"] == REAL_KEY and gpu["detail"]["source"] == "amdsmi+kfd"
    assert gpu["vram_total"] == 96 * GiB and gpu["vram_free"] == 33347 * MiB and gpu["temperature"] == 31
    assert gpu["detail"]["render_minor"] == 128 and gpu["detail"]["pool"] == "vram"
    (m,) = c.metrics()
    assert m["key"] == REAL_KEY and m["vram_free"] == 33347 * MiB  # live values keyed like the inventory
    c.close()
    out, err = capfd.readouterr()
    assert out == "" and "Fail to open libdrm.so.2" in err and "amdsmi python chatter" in err
    assert "shutting down" in err


def test_fy_col_061_the_next_tools_fill_what_amdsmi_could_not(tmp_path):
    """FY-COL-061: with rocm-smi there, it gives the address, target and IDs; amdsmi's memory and
    temperature stay (it came first). No single KFD guess is needed then."""
    run = fake_runner({" ".join(ROCM_SMI): (0, _real("rocm-smi.json"), "")})
    c = AmdCollector(amdsmi=NoLibdrmAmdsmi(), run=run, which=lambda n: "/x" if n == "rocm-smi" else None,
                     **_real_machine(tmp_path))
    r = c.inventory(raw=True)
    assert r.source == "amdsmi+rocm-smi" and r.raw["used"] == ["amdsmi", "rocm-smi"]
    (gpu,) = r.accelerators
    assert (gpu["arch"], gpu["unified_memory"], gpu["model"], gpu["driver"]) == ("gfx1151", True, "AMD Radeon 8060S", "7.0.2")
    assert gpu["key"] == REAL_KEY and "amd:0x0" in gpu["detail"]["aliases"]
    assert gpu["vram_used"] == 64957 * MiB and gpu["utilization"] == 4  # amdsmi's used; rocm-smi's use% where amdsmi had none
    assert gpu["detail"]["amdgpu_driver"] == "7.0.0-38-generic"


def test_fy_col_062_a_complete_answer_asks_nothing_else(tmp_path):
    """FY-COL-062: a source that gave everything is not followed by the CLIs."""
    run = fake_runner({})
    r = AmdCollector(amdsmi=FakeAmdsmi(load("amdsmi_gfx1151_apu.json")), run=run, which=lambda n: "/x/" + n,
                     **_real_machine(tmp_path)).inventory()
    assert r.source == "amdsmi" and r.reason is None and run.seen == []


def test_fy_col_063_what_cannot_be_filled_is_said(tmp_path):
    """FY-COL-063: nothing else to ask: the partial card is reported, and the reason names the gaps."""
    r = AmdCollector(amdsmi=NoLibdrmAmdsmi(), **no_tools()).inventory()
    assert r.ok and r.source == "amdsmi"
    assert r.reason.endswith("still unknown: #0: pci_bus_id, arch, model")
    assert r.accelerators[0]["arch"] is None


class PartialAmdsmi(FakeAmdsmi):
    """The real Strix Halo through amdsmi, with the named calls failing."""

    def __init__(self, fails=(), board="N/A"):
        super().__init__({"handles": [{
            "asic_info": {"market_name": "AMD Radeon Graphics", "target_graphics_version": "gfx1151",
                          "device_id": "0x1586", "asic_serial": "0x0000000000000000"},
            "board_info": {"product_name": board}, "driver_info": {"driver_version": "N/A"},
            "uuid": REAL_UUID, "bdf": "0000:c6:00.0",
            "memory": {"VRAM": [96 * GiB, 64957 * MiB], "GTT": [15862 * MiB, 764 * MiB]},
            "activity": {"gfx_activity": 2}, "temperature": {"EDGE": 31}}]})
        self._broken = set(fails)

    def __getattribute__(self, name):
        if name in object.__getattribute__(self, "_broken"):
            def fail(*a):
                raise RuntimeError("AMDSMI_STATUS_DRM_ERROR")
            return fail
        return super().__getattribute__(name)

    def amdsmi_get_gpu_board_info(self, h):
        return dict(self._h(h)["board_info"])


@pytest.mark.parametrize("gap, fails, board, kfd, cpu", [
    ("arch", {"amdsmi_get_gpu_asic_info"}, "AMD Radeon 8060S", False, False),
    ("model", {"amdsmi_get_gpu_asic_info", "amdsmi_get_gpu_board_info"}, "N/A", True, False),
    ("vram_total", {"amdsmi_get_gpu_memory_total"}, "N/A", True, True),
    ("pci_bus_id", {"amdsmi_get_gpu_device_bdf"}, "N/A", False, True),
])
def test_fy_col_065_each_missing_key_field_asks_the_next_source(tmp_path, gap, fails, board, kfd, cpu):
    """FY-COL-065: one key field missing at a time (address, target, memory size, name): the CLIs
    are asked and fill it."""
    machine = _real_machine(tmp_path)
    if not kfd:
        machine["kfd_root"] = tmp_path / "no-kfd"
    if not cpu:
        machine["cpuinfo"] = tmp_path / "no-cpuinfo"
    lib = PartialAmdsmi(fails, board)
    c = AmdCollector(amdsmi=lib, run=fake_runner({}), which=lambda n: None, **machine)
    assert gaps(c._lib_reading(lib, 0, 0, None), read_kfd(machine["kfd_root"]), read_cpu_model(machine["cpuinfo"])) == [gap]
    run = _real_amd_smi()
    r = AmdCollector(amdsmi=PartialAmdsmi(fails, board), run=run, which=lambda n: "/x/" + n, **machine).inventory()
    assert r.source == "amdsmi+amd-smi" and "amd-smi static --asic --bus --vram --driver --board --json" in run.seen
    (gpu,) = r.accelerators
    assert (gpu["key"], gpu["arch"], gpu["unified_memory"], gpu["vram_total"]) == (REAL_KEY, "gfx1151", True, 96 * GiB)
    assert gpu["model"] in ("AMD Radeon 8060S", "AMD Radeon Graphics") and "still unknown" not in (r.reason or "")


def test_fy_col_066_pairing_and_kfd_only_when_unambiguous(tmp_path):
    """FY-COL-066: two sources' cards pair by address, by position only if one lacks it; KFD gives
    an address only for a single card and a single GPU node, and its gfx target counts as a source."""
    a, b = reading(index=0, bdf="0000:c6:00.0"), reading(index=0, bdf="0000:c7:00.0")
    assert pair_up([a], [b]) == []
    assert pair_up([reading(index=0)], [b]) == [(reading(index=0), b)]
    assert pair_up([a, reading(index=1)], [b]) == []
    assert pair_up([reading(index=0), reading(index=1)], [b]) == []  # two cards, one reading: no guessing
    two = {"0000:c6:00.0": {"gfx": "gfx1151"}, "0000:c7:00.0": {"gfx": "gfx1151"}}
    lone = [reading(index=0)]
    assert not fill_from_kfd(lone, two) and lone[0]["bdf"] is None
    machine = _real_machine(tmp_path)
    run = fake_runner({})
    r = AmdCollector(amdsmi=PartialAmdsmi({"amdsmi_get_gpu_asic_info"}, "AMD Radeon 8060S"), run=run,
                     which=lambda n: "/x/" + n, **machine).inventory()
    assert r.source == "amdsmi+kfd" and r.accelerators[0]["arch"] == "gfx1151" and run.seen == []


def test_fy_col_064_c_output_goes_to_stderr_and_stdout_comes_back(capfd):
    """FY-COL-064"""
    import ctypes

    from forgeyard.agent.inventory.amd import c_stdout_to_stderr

    with c_stdout_to_stderr():
        os.write(1, b"fd one\n")
        ctypes.CDLL(None).printf(b"buffered C\n")
        print("python print")
    print("after")
    out, err = capfd.readouterr()
    assert out == "after\n" and "fd one" in err and "buffered C" in err and "python print" in err


# ------------------------------------------------------------------ CPU
def test_fy_col_030_cpuinfo_x86_two_sockets():
    """FY-COL-030"""
    info = parse_cpuinfo((FIXTURES / "cpuinfo_x86_2s.txt").read_text())
    assert info == {"model": "Intel(R) Xeon(R) Gold 6338 CPU @ 2.00GHz", "threads": 8, "cores": 4, "sockets": 2,
                    "isa": ["avx", "avx2", "fma", "f16c", "avx512f", "avx512_vnni"]}


def test_fy_col_031_cpuinfo_arm():
    """FY-COL-031"""
    info = parse_cpuinfo((FIXTURES / "cpuinfo_arm.txt").read_text())
    assert info["model"] == "NVIDIA Grace" and info["threads"] == 2 and info["cores"] == 2
    assert info["isa"] == ["asimd", "sve", "sve2", "i8mm", "bf16"]


def test_fy_col_032_cpu_row_from_proc(tmp_path):
    """FY-COL-032: RAM total / available from meminfo; utilisation from /proc/stat between samples."""
    (tmp_path / "cpuinfo").write_text((FIXTURES / "cpuinfo_x86_2s.txt").read_text())
    (tmp_path / "meminfo").write_text((FIXTURES / "meminfo.txt").read_text())
    (tmp_path / "stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    c = CpuCollector(tmp_path)
    (row,) = c.inventory().accelerators
    assert (row["vendor"], row["key"], row["vram_total"], row["vram_free"]) == ("cpu", "cpu:0", 263843488 * 1024,
                                                                                 201326592 * 1024)
    assert row["detail"]["cores"] == 4 and row["detail"]["threads"] == 8 and not row["unified_memory"]
    (tmp_path / "stat").write_text("cpu  150 0 150 900 0 0 0 0 0 0\n")
    assert c.metrics()[0]["utilization"] == 50
    assert parse_meminfo("MemTotal: 10 kB\nHugePages_Total: 0\n") == {"MemTotal": 10240, "HugePages_Total": 0}


def test_fy_col_033_cpu_collector_survives_a_missing_proc(tmp_path):
    """FY-COL-033"""
    r = CpuCollector(tmp_path / "nothing").inventory()
    assert r.ok and r.accelerators[0]["vendor"] == "cpu" and r.accelerators[0]["detail"]["threads"] >= 1


# ------------------------------------------------------------------ the whole inventory
def test_fy_col_040_inventory_combines_and_contains_crashes():
    """FY-COL-040: GPUs first, CPU last; a collector that raises becomes a reason."""
    class Boom:
        name = "boom"

        def inventory(self, raw=False):
            raise RuntimeError("kaboom")

        def metrics(self):
            raise RuntimeError("kaboom")

        def close(self):
            raise RuntimeError("kaboom")

    class Fixed:
        name = "cpu"

        def inventory(self, raw=False):
            return Result(accelerators=[{"key": "cpu:0", "vendor": "cpu", "index": 0, "model": "C"}], ok=True)

        def metrics(self):
            return [{"key": "cpu:0", "vram_free": 1}]

        def close(self):
            pass

    inv = Inventory([Fixed(), NvidiaCollector(nvml=FakeNvml(load("nvml_a100x8.json"))), Boom()])
    out = inv.full()
    assert [a["vendor"] for a in out["accelerators"]] == ["nvidia"] * 8 + ["cpu"]
    assert out["collectors"]["boom"] == {"ok": False, "count": 0, "reason": "collector crashed: kaboom"}
    assert out["collectors"]["nvidia"]["count"] == 8
    assert len(inv.metrics()) == 9
    inv.close()
    json.dumps(out)  # the wire format is plain JSON


def test_fy_col_041_raw_output_for_bug_reports():
    """FY-COL-041: --raw keeps what each tool returned, so Tony can send it back."""
    inv = Inventory([AmdCollector(amdsmi=FakeAmdsmi(load("amdsmi_gfx1151_apu.json")), **no_tools())])
    raw = inv.full(raw=True)["raw"]["amd"]
    assert raw["amdsmi"][0]["asic_info"]["target_graphics_version"] == "gfx1151"
    assert raw["amdsmi"][0]["memory"]["gtt_total"] == 110 * GiB and raw["kfd"] == {}
