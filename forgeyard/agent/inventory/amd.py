"""AMD GPUs: the `amdsmi` Python library, else the `amd-smi` CLI, else `rocm-smi`.

All three are read-only management interfaces (no HIP context, no allocation), safe on cards
busy with other work.

Sources for the field names (checked 2026-11-09; anything marked UNCONFIRMED has not been
seen on real hardware by Forgeyard yet, see docs/acceptance-m1.md):

* amdsmi Python API — https://rocm.docs.amd.com/projects/amdsmi/en/latest/reference/amdsmi-py-api.html
    amdsmi_init(), amdsmi_get_processor_handles()
    amdsmi_get_gpu_asic_info(h)    -> market_name, target_graphics_version, device_id, ...
    amdsmi_get_gpu_device_uuid(h), amdsmi_get_gpu_device_bdf(h)
    amdsmi_get_gpu_driver_info(h)  -> driver_name, driver_version
    amdsmi_get_gpu_memory_total(h, AmdSmiMemoryType.VRAM | GTT), amdsmi_get_gpu_memory_usage(...)  (bytes)
    amdsmi_get_gpu_activity(h)     -> gfx_activity (percent or "N/A")
    amdsmi_get_temp_metric(h, AmdSmiTemperatureType.EDGE, AmdSmiTemperatureMetric.CURRENT)
  UNCONFIRMED: the exact form of target_graphics_version on every ROCm release ("gfx1151"
  is expected; an integer like 110501 is decoded too) and whether the temperature comes back
  in degrees or millidegrees (values above 1000 are divided by 1000).
* amd-smi CLI — https://github.com/ROCm/amdsmi/blob/amd-staging/amdsmi_cli/amdsmi_commands.py
    static --json: asic.market_name, asic.target_graphics_version, bus.bdf, driver.version,
                   vram.size {"value", "unit": "MB"}, board.product_name
    metric --json: mem_usage.total_vram / used_vram / free_vram / total_gtt / used_gtt / free_gtt
                   ({"value", "unit": "MB"}, MiB computed as bytes // 1024**2), usage.gfx_activity,
                   temperature.edge / hotspot
    list --json:   gpu, bdf, uuid
  Newer releases wrap the per-GPU list in {"gpu_data": [...]}; older ones print a bare list.
  Both are read.
* rocm-smi --json — keys such as "Card Series", "GFX Version", "VRAM Total Memory (B)",
  "GTT Total Memory (B)", "PCI Bus", "Unique ID", "GPU use (%)", "Temperature (Sensor edge) (C)".
  Spelling varies between releases, so they are matched case-insensitively. UNCONFIRMED.
* KFD topology (Linux kernel, /sys/class/kfd/kfd/topology/nodes/*/properties): simd_count,
  cpu_cores_count, gfx_target_version (major*10000 + minor*100 + stepping), location_id, domain.

Unified memory (APUs such as Strix Halo gfx1151, Strix Point gfx1150, Phoenix gfx1103, and
MI300A): the GPU has only a small VRAM carve-out and works mostly out of GTT (system RAM
mapped for the GPU). For APUs, amd-smi's own monitor "reports the larger of the VRAM and GTT
pools" (https://rocm.docs.amd.com/projects/amdsmi/en/latest/how-to/amdsmi-cli-tool.html), and
Forgeyard does the same: vram_total / vram_free describe that larger pool, with the carve-out
and GTT sizes kept in `detail`. A card counts as an APU when its KFD node also has CPU cores,
or its gfx target is a known APU target. UNCONFIRMED on real gfx1151 hardware: whether the KFD
node reports cpu_cores_count there, and how amdsmi splits VRAM/GTT on it.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from forgeyard.agent.inventory.base import Result, error_text, integer, number, text

KFD_ROOT = "/sys/class/kfd/kfd/topology/nodes"
MIB = 1024 * 1024

# gfx targets that only exist as integrated GPUs (APUs). MI300A (gfx942) shares its target with
# the discrete MI300X, so it is recognised from the KFD topology instead.
APU_GFX = {"gfx902", "gfx909", "gfx90c", "gfx1033", "gfx1035", "gfx1036", "gfx1037", "gfx1103",
           "gfx1150", "gfx1151", "gfx1152", "gfx1153"}

Runner = Callable[[list[str]], tuple[int, str, str]]


def run_command(cmd: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=None)
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out after {timeout:.0f} s"
    except OSError as e:
        return 126, "", error_text(e)
    return p.returncode, p.stdout, p.stderr


# ------------------------------------------------------------------ helpers
def decode_gfx(value: Any) -> Optional[str]:
    """'gfx1151', 'gfx90a:sramecc+:xnack-', or KFD's integer 110501 -> 'gfx1151'."""
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip().lower()
        if v.startswith("gfx"):
            return v.split(":", 1)[0]
        if not v.isdigit():
            return None
        value = int(v)
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    major, minor, step = n // 10000, (n // 100) % 100, n % 100
    return f"gfx{major}{minor:x}{step:x}"


def normalise_bdf(value: Any) -> Optional[str]:
    v = text(value)
    if not v:
        return None
    v = v.lower()
    if v.count(":") == 1:  # "c3:00.0" without a domain
        v = "0000:" + v
    return v


def read_kfd(root: str | Path = KFD_ROOT) -> dict[str, dict]:
    """GPU nodes of the KFD topology, by PCI address. Empty when there is no amdgpu/KFD."""
    out: dict[str, dict] = {}
    base = Path(root)
    try:
        nodes = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        return out
    for node in nodes:
        try:
            props = {}
            for line in (node / "properties").read_text().splitlines():
                key, _, value = line.strip().partition(" ")
                if value.strip().lstrip("-").isdigit():
                    props[key] = int(value.strip())
        except OSError:
            continue
        if props.get("simd_count", 0) <= 0:
            continue  # a CPU-only node
        loc, domain = props.get("location_id", 0), props.get("domain", 0)
        bdf = f"{domain:04x}:{loc >> 8:02x}:{(loc >> 3) & 0x1f:02x}.{loc & 0x7}"
        out[bdf] = {"node": node.name, "gfx": decode_gfx(props.get("gfx_target_version")),
                    "cpu_cores": props.get("cpu_cores_count", 0), "simd": props.get("simd_count", 0)}
    return out


def unified_reason(gfx: Optional[str], kfd_node: Optional[dict]) -> Optional[str]:
    if kfd_node and kfd_node.get("cpu_cores", 0) > 0:
        return "kfd: the GPU node also has CPU cores"
    if gfx in APU_GFX:
        return f"{gfx} is an APU target"
    return None


def _ci(d: dict, *names: str) -> Any:
    """Case-insensitive lookup of the first present key (rocm-smi's spelling varies)."""
    lowered = {str(k).strip().lower(): v for k, v in d.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _celsius(value: Any) -> Optional[int]:
    n = number(value)
    if n is None:
        return None
    if n > 1000:  # millidegrees
        n = n / 1000
    return round(n)


def _gpu_list(data: Any) -> list[dict]:
    if isinstance(data, dict) and "gpu_data" in data:
        data = data["gpu_data"]
    if isinstance(data, dict):
        data = [data] if "gpu" in data else []
    return [g for g in data if isinstance(g, dict)] if isinstance(data, list) else []


def make_card(*, index: int, model: Optional[str], gfx: Optional[str], uuid: Optional[str], bdf: Optional[str],
              driver: Optional[str], vram_total: Optional[int], vram_used: Optional[int], gtt_total: Optional[int],
              gtt_used: Optional[int], utilization: Optional[int], temperature: Optional[int],
              kfd: dict[str, dict], source: str) -> dict:
    bdf = normalise_bdf(bdf)
    node = kfd.get(bdf) if bdf else None
    gfx = decode_gfx(gfx) or (node or {}).get("gfx")
    why = unified_reason(gfx, node)
    total, used = vram_total, vram_used
    detail: dict[str, Any] = {"pci_bus_id": bdf, "source": source}
    if gtt_total is not None:
        detail["gtt_total"], detail["gtt_used"] = gtt_total, gtt_used
    if why:
        detail["vram_carveout"] = vram_total
        detail["unified_by"] = why
        if gtt_total is not None and (vram_total is None or gtt_total > vram_total):
            total, used, detail["pool"] = gtt_total, gtt_used, "gtt"
        else:
            detail["pool"] = "vram"
    free = total - used if total is not None and used is not None else None
    return {
        "key": f"amd:{uuid or bdf or index}", "vendor": "amd", "index": index,
        "model": model or "AMD GPU", "arch": gfx, "uuid": uuid,
        "vram_total": total, "vram_free": free, "vram_used": used, "unified_memory": bool(why),
        "driver": driver, "utilization": utilization, "temperature": temperature,
        "detail": {k: v for k, v in detail.items() if v is not None},
    }


# ------------------------------------------------------------------ amd-smi / rocm-smi text
def parse_amd_smi(static: Any, metric: Any, listing: Any, kfd: dict[str, dict]) -> list[dict]:
    metrics = {integer(m.get("gpu")): m for m in _gpu_list(metric)}
    listed = {integer(m.get("gpu")): m for m in _gpu_list(listing)}
    cards = []
    for position, g in enumerate(_gpu_list(static)):
        idx = integer(g.get("gpu"))
        idx = position if idx is None else idx
        asic, bus, vram = g.get("asic") or {}, g.get("bus") or {}, g.get("vram") or {}
        driver, board = g.get("driver") or {}, g.get("board") or {}
        m = (metrics.get(idx) or {}).get("mem_usage") or {}
        usage, temps = (metrics.get(idx) or {}).get("usage") or {}, (metrics.get(idx) or {}).get("temperature") or {}
        info = listed.get(idx) or {}

        def mib(v):
            n = number(v)
            return int(n * MIB) if n is not None else None

        vram_total = mib(m.get("total_vram")) or mib(vram.get("size"))
        model = asic.get("market_name") if text(asic.get("market_name")) not in (None, "N/A") else board.get("product_name")
        cards.append(make_card(
            index=idx, model=text(model), gfx=asic.get("target_graphics_version"),
            uuid=text(info.get("uuid")) if text(info.get("uuid")) != "N/A" else None,
            bdf=bus.get("bdf") or info.get("bdf"),
            driver=text(driver.get("version")) if isinstance(driver, dict) else text(driver),
            vram_total=vram_total, vram_used=mib(m.get("used_vram")),
            gtt_total=mib(m.get("total_gtt")), gtt_used=mib(m.get("used_gtt")),
            utilization=integer(usage.get("gfx_activity")),
            temperature=_celsius(temps.get("edge")) or _celsius(temps.get("hotspot")),
            kfd=kfd, source="amd-smi"))
    return cards


def parse_rocm_smi(data: Any, kfd: dict[str, dict]) -> list[dict]:
    if not isinstance(data, dict):
        return []
    system = data.get("system") or {}
    driver = text(_ci(system, "Driver version"))
    cards = []
    for name in sorted((k for k in data if str(k).lower().startswith("card")), key=lambda k: int(str(k)[4:] or 0)):
        c = data[name] or {}
        uid = text(_ci(c, "Unique ID"))
        cards.append(make_card(
            index=int(str(name)[4:] or 0),
            model=text(_ci(c, "Card Series", "Card series", "Card Model", "Card model")),
            gfx=_ci(c, "GFX Version"), uuid=uid if uid and uid.lower() != "n/a" else None,
            bdf=_ci(c, "PCI Bus"), driver=driver,
            vram_total=integer(_ci(c, "VRAM Total Memory (B)")), vram_used=integer(_ci(c, "VRAM Total Used Memory (B)")),
            gtt_total=integer(_ci(c, "GTT Total Memory (B)")), gtt_used=integer(_ci(c, "GTT Total Used Memory (B)")),
            utilization=integer(_ci(c, "GPU use (%)")),
            temperature=_celsius(_ci(c, "Temperature (Sensor edge) (C)", "Temperature (Sensor junction) (C)")),
            kfd=kfd, source="rocm-smi"))
    return cards


def _json(out: str) -> Any:
    out = out.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        start = min((i for i in (out.find("{"), out.find("[")) if i >= 0), default=-1)
        if start <= 0:
            return None
        try:  # some releases print a warning line before the JSON
            return json.loads(out[start:])
        except ValueError:
            return None


AMD_SMI_STATIC = [["amd-smi", "static", "--asic", "--bus", "--vram", "--driver", "--board", "--json"],
                  ["amd-smi", "static", "--json"]]
AMD_SMI_METRIC = [["amd-smi", "metric", "--mem-usage", "--usage", "--temperature", "--json"],
                  ["amd-smi", "metric", "--json"]]
AMD_SMI_LIST = [["amd-smi", "list", "--json"]]
ROCM_SMI = ["rocm-smi", "--showproductname", "--showmeminfo", "vram", "gtt", "--showdriverversion", "--showbus",
            "--showuniqueid", "--showuse", "--showtemp", "--json"]


class AmdCollector:
    name = "amd"

    def __init__(self, amdsmi: Any = None, run: Optional[Runner] = None, kfd_root: str | Path = KFD_ROOT,
                 which: Callable[[str], Optional[str]] = shutil.which):
        self._lib_module = amdsmi
        self._run = run or run_command
        self._kfd_root = kfd_root
        self._which = which
        self._backend: Optional[str] = None
        self._lib: Any = None
        self._lib_started = False
        self._handles: list[tuple[int, Any]] = []
        self._kfd: dict[str, dict] = {}
        self._static: Any = None
        self._listing: Any = None

    # -------------------------------------------------------------- amdsmi library
    def _load_lib(self) -> tuple[Any, Optional[str]]:
        if self._lib is not None:
            return self._lib, None
        try:
            lib = self._lib_module or importlib.import_module("amdsmi")
        except ImportError as e:
            return None, f"amdsmi Python library not importable ({error_text(e)})"
        except Exception as e:  # a broken libamd_smi.so can raise OSError at import time
            return None, f"amdsmi failed to load: {error_text(e)}"
        try:
            lib.amdsmi_init()
        except Exception as e:
            return None, f"amdsmi_init failed: {error_text(e)}"
        self._lib, self._lib_started = lib, True
        return lib, None

    @staticmethod
    def _call(fn, *args, default=None):
        try:
            return fn(*args)
        except Exception:
            return default

    def _lib_memory(self, lib, h) -> dict:
        mt = lib.AmdSmiMemoryType
        return {
            "vram_total": integer(self._call(lib.amdsmi_get_gpu_memory_total, h, mt.VRAM)),
            "vram_used": integer(self._call(lib.amdsmi_get_gpu_memory_usage, h, mt.VRAM)),
            "gtt_total": integer(self._call(lib.amdsmi_get_gpu_memory_total, h, mt.GTT)),
            "gtt_used": integer(self._call(lib.amdsmi_get_gpu_memory_usage, h, mt.GTT)),
        }

    def _lib_live(self, lib, h) -> dict:
        activity = self._call(lib.amdsmi_get_gpu_activity, h) or {}
        temp = None
        for sensor in ("EDGE", "HOTSPOT"):
            t = getattr(lib.AmdSmiTemperatureType, sensor, None)
            if t is not None:
                temp = _celsius(self._call(lib.amdsmi_get_temp_metric, h, t, lib.AmdSmiTemperatureMetric.CURRENT))
            if temp:
                break
        return {"utilization": integer(activity.get("gfx_activity")) if isinstance(activity, dict) else None,
                "temperature": temp}

    def _lib_card(self, lib, index: int, h, raw_out: Optional[list]) -> dict:
        asic = self._call(lib.amdsmi_get_gpu_asic_info, h) or {}
        driver = self._call(lib.amdsmi_get_gpu_driver_info, h) or {}
        uuid = text(self._call(lib.amdsmi_get_gpu_device_uuid, h))
        bdf = text(self._call(lib.amdsmi_get_gpu_device_bdf, h))
        mem, live = self._lib_memory(lib, h), self._lib_live(lib, h)
        if raw_out is not None:
            raw_out.append({"index": index, "asic_info": asic, "driver_info": driver, "uuid": uuid, "bdf": bdf,
                            "memory": mem, **live})
        name = text(asic.get("market_name"))
        return make_card(index=index, model=name if name != "N/A" else None, gfx=asic.get("target_graphics_version"),
                         uuid=uuid, bdf=bdf, driver=text(driver.get("driver_version")), **mem, **live,
                         kfd=self._kfd, source="amdsmi")

    def _from_lib(self, raw: bool) -> Result:
        lib, problem = self._load_lib()
        if lib is None:
            return Result(ok=False, source="amdsmi", reason=problem)
        try:
            handles = list(lib.amdsmi_get_processor_handles())
        except Exception as e:
            return Result(ok=False, source="amdsmi", reason=f"amdsmi could not list GPUs: {error_text(e)}")
        if not handles:
            return Result(ok=False, source="amdsmi", reason="amdsmi found no GPUs")
        raw_out: Optional[list] = [] if raw else None
        self._handles = list(enumerate(handles))
        cards = [self._lib_card(lib, i, h, raw_out) for i, h in self._handles]
        return Result(accelerators=cards, ok=True, source="amdsmi", raw={"amdsmi": raw_out} if raw else None)

    # -------------------------------------------------------------- CLIs
    def _first_json(self, commands: list[list[str]]) -> tuple[Any, Optional[str]]:
        problem = None
        for cmd in commands:
            code, out, err = self._run(cmd)
            data = _json(out)
            if data is not None and (code == 0 or cmd[0] == "rocm-smi"):
                return data, None
            problem = (err.strip() or out.strip() or f"exit {code}")[:200]
        return None, problem

    def _from_amd_smi(self, raw: bool) -> Result:
        if not self._which("amd-smi"):
            return Result(ok=False, source="amd-smi", reason="amd-smi not on PATH")
        static, problem = self._first_json(AMD_SMI_STATIC)
        if static is None:
            return Result(ok=False, source="amd-smi", reason=f"amd-smi static failed: {problem}")
        metric, _ = self._first_json(AMD_SMI_METRIC)
        listing, _ = self._first_json(AMD_SMI_LIST)
        cards = parse_amd_smi(static, metric, listing, self._kfd)
        if not cards:
            return Result(ok=False, source="amd-smi", reason="amd-smi listed no GPUs")
        self._static, self._listing = static, listing
        return Result(accelerators=cards, ok=True, source="amd-smi",
                      raw={"static": static, "metric": metric, "list": listing} if raw else None)

    def _from_rocm_smi(self, raw: bool) -> Result:
        if not self._which("rocm-smi"):
            return Result(ok=False, source="rocm-smi", reason="rocm-smi not on PATH")
        data, problem = self._first_json([ROCM_SMI])
        if data is None:
            return Result(ok=False, source="rocm-smi", reason=f"rocm-smi failed: {problem}")
        cards = parse_rocm_smi(data, self._kfd)
        if not cards:
            return Result(ok=False, source="rocm-smi", reason="rocm-smi listed no GPUs")
        return Result(accelerators=cards, ok=True, source="rocm-smi", raw={"rocm-smi": data} if raw else None)

    # -------------------------------------------------------------- collector interface
    def inventory(self, raw: bool = False) -> Result:
        self._kfd = read_kfd(self._kfd_root)
        tried = []
        for backend, fn in (("amdsmi", self._from_lib), ("amd-smi", self._from_amd_smi), ("rocm-smi", self._from_rocm_smi)):
            try:
                result = fn(raw)
            except Exception as e:  # a parser surprise must not take the agent down
                result = Result(ok=False, source=backend, reason=f"{backend}: {error_text(e)}")
            if result.ok:
                self._backend = backend
                if raw:
                    result.raw = {**(result.raw or {}), "kfd": self._kfd, "skipped": tried}
                if tried:
                    result.reason = "used " + backend + " (" + "; ".join(tried) + ")"
                return result
            tried.append(result.reason or backend)
        self._backend = None
        return Result(ok=False, source=None, reason="; ".join(tried),
                      raw={"kfd": self._kfd, "skipped": tried} if raw else None)

    def metrics(self) -> list[dict]:
        try:
            if self._backend == "amdsmi":
                out = []
                for i, h in self._handles:
                    card = self._lib_card(self._lib, i, h, None)
                    out.append({k: card[k] for k in ("key", "vram_free", "vram_used", "utilization", "temperature")})
                return out
            if self._backend == "amd-smi":
                metric, _ = self._first_json(AMD_SMI_METRIC)
                cards = parse_amd_smi(self._static, metric, self._listing, self._kfd)
            elif self._backend == "rocm-smi":
                data, _ = self._first_json([ROCM_SMI])
                cards = parse_rocm_smi(data, self._kfd)
            else:
                return []
        except Exception:
            return []
        return [{k: c[k] for k in ("key", "vram_free", "vram_used", "utilization", "temperature")} for c in cards]

    def close(self) -> None:
        if self._lib_started:
            self._call(self._lib.amdsmi_shut_down)
            self._lib_started = False
            self._lib = None
