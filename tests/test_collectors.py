"""Inventory collectors on recorded and representative tool output (FY-COL-*)."""

import json

import pytest

from forgeyard.agent.inventory import Inventory
from forgeyard.agent.inventory.amd import ROCM_SMI, AmdCollector, decode_gfx, parse_amd_smi, parse_rocm_smi, read_kfd
from forgeyard.agent.inventory.base import Result
from forgeyard.agent.inventory.cpu import CpuCollector, parse_cpuinfo, parse_meminfo
from forgeyard.agent.inventory.nvidia import NvidiaCollector
from tests.fakes import FIXTURES, FakeAmdsmi, FakeNvml, fake_runner, kfd_location, load, make_kfd

GiB, MiB = 1024**3, 1024**2
A100_FREE = [4023779328, 7219380224, 6169165824, 9480110080, 48294330368, 15321071616, 9689825280, 7716405248]


def no_tools(which=None):
    return dict(run=fake_runner({}), which=which or (lambda name: None), kfd_root="/nonexistent")


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
    lib = FakeAmdsmi(load("amdsmi_gfx1151_apu.json"))
    r = AmdCollector(amdsmi=lib, **no_tools()).inventory()
    assert r.ok and r.source == "amdsmi"
    (gpu,) = r.accelerators
    assert (gpu["model"], gpu["arch"], gpu["unified_memory"], gpu["driver"]) == ("AMD Radeon 8060S", "gfx1151", True, "6.14.0")
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
    assert (a["arch"], a["driver"], a["utilization"], a["temperature"]) == ("gfx1100", "6.10.5", 88, 61)
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
