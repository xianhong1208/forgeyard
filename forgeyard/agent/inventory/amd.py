"""AMD GPUs: the `amdsmi` Python library, else the `amd-smi` CLI, else `rocm-smi`.

All three are read-only management interfaces (no HIP context, no allocation), safe on cards
busy with other work.

Sources for the field names (checked 2026-11-09, then on a real Strix Halo with ROCm 7.0.2 and
amd-smi 26.0.2 on 2026-11-11):

* amdsmi Python API — https://rocm.docs.amd.com/projects/amdsmi/en/latest/reference/amdsmi-py-api.html
    amdsmi_init(), amdsmi_get_processor_handles()
    amdsmi_get_gpu_asic_info(h)    -> market_name, target_graphics_version, device_id, asic_serial, ...
    amdsmi_get_gpu_device_uuid(h), amdsmi_get_gpu_device_bdf(h)
    amdsmi_get_gpu_driver_info(h)  -> driver_name, driver_version (the kernel's amdgpu, not ROCm)
    amdsmi_get_gpu_memory_total(h, AmdSmiMemoryType.VRAM | GTT), amdsmi_get_gpu_memory_usage(...)  (bytes)
    amdsmi_get_gpu_activity(h)     -> gfx_activity (percent or "N/A")
    amdsmi_get_temp_metric(h, AmdSmiTemperatureType.EDGE, AmdSmiTemperatureMetric.CURRENT)
    amdsmi_get_rocm_version()      -> (ok, "7.0.2")
  ROCm installs the package, not into any Python, but as a source tree under
  <rocm>/share/amd_smi/amdsmi (with a copy of libamd_smi.so). Its loader looks for the library
  in $ROCM_PATH/lib, then the linker's paths, then next to itself (amdsmi_wrapper.py in ROCm 7.0).
  When `import amdsmi` fails, `import_amdsmi` tries that tree with ROCM_PATH pointing at its ROCm,
  which is what amd-smi itself does (amdsmi_cli/amdsmi_init.py appends ../../share/amd_smi to
  sys.path). On the real Strix Halo it loaded that way, but libamd_smi dlopen()s libdrm.so.2 and
  libdrm_amdgpu.so, which the image lacked: memory came back, ASIC information did not. The image
  now ships them, and a partial answer like that is completed from the other sources (inventory()).
* amd-smi CLI — https://github.com/ROCm/amdsmi/blob/amd-staging/amdsmi_cli/amdsmi_commands.py
    static --json: asic.market_name, asic.target_graphics_version, asic.device_id, asic.asic_serial,
                   bus.bdf, driver.version, vram.size {"value", "unit": "MB"}, board.product_name
    metric --json: mem_usage.total_vram / used_vram / free_vram / total_gtt / used_gtt / free_gtt
                   ({"value", "unit": "MB"}: MiB, computed as bytes // 1024**2), usage.gfx_activity,
                   temperature.edge / hotspot
    list --json:   gpu, bdf, uuid
    version --json: rocm_version
  amd-smi 26 wraps static and metric in {"gpu_data": [...]} but prints `list` as a bare list,
  and puts the string "N/A" wherever a value (or a whole section such as "usage") is not
  supported. Every section is read defensively; a missing value is recorded in detail.missing.
* rocm-smi --json — keys such as "Card Series", "GFX Version", "VRAM Total Memory (B)",
  "GTT Total Memory (B)", "PCI Bus", "Unique ID", "GPU use (%)", "Temperature (Sensor edge) (C)".
  Spelling varies between releases, so they are matched case-insensitively. Its "Driver version"
  is the kernel's version on in-tree amdgpu (e.g. "7.0.0-38-generic").
* KFD topology (Linux kernel, /sys/class/kfd/kfd/topology/nodes/*/properties): simd_count,
  cpu_cores_count, gfx_target_version (major*10000 + minor*100 + stepping), location_id, domain,
  unique_id, drm_render_minor.

Unified memory (APUs such as Strix Halo gfx1151, Strix Point gfx1150, Phoenix gfx1103, and
MI300A): the GPU has a VRAM carve-out set in the BIOS and can also use GTT (system RAM mapped
for the GPU). For APUs, amd-smi's own monitor "reports the larger of the VRAM and GTT pools"
(https://rocm.docs.amd.com/projects/amdsmi/en/latest/how-to/amdsmi-cli-tool.html), and Forgeyard
does the same: vram_total / vram_free describe that larger pool, with the carve-out and GTT sizes
kept in `detail`. A card counts as an APU when its KFD node also has CPU cores, or its gfx target
is a known APU target (on the real gfx1151 the KFD node reports cpu_cores_count 0, so the target
is what marks it).

Keys. A card's key must be unique on the machine and the same whichever tool read it. amdsmi's
UUID is built from the PCI device id and the card's serial number (unique_id; amdsmi_uuid_gen in
amd_smi_uuid.cc), so a card without a serial (every Strix Halo: "00ff1586-0000-1000-8000-
000000000000", rocm-smi "Unique ID: 0x0") has a UUID that is not unique. The key is then the
PCI address. The keys an older agent may have used for the same card are sent along in
detail.aliases, so the server can carry the card's allowlist entry over (machines/store.py).
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from forgeyard.agent.inventory.base import Result, error_text, integer, number, text

KFD_ROOT = "/sys/class/kfd/kfd/topology/nodes"
CPUINFO = "/proc/cpuinfo"
MIB = 1024 * 1024

# gfx targets that only exist as integrated GPUs (APUs). MI300A (gfx942) shares its target with
# the discrete MI300X, so it is recognised from the KFD topology instead.
APU_GFX = {"gfx902", "gfx909", "gfx90c", "gfx1033", "gfx1035", "gfx1036", "gfx1037", "gfx1103",
           "gfx1150", "gfx1151", "gfx1152", "gfx1153"}

# What the tools print for "not available".
_NA = {"n/a", "na", "none", "null", "unknown", "not supported", "unsupported"}
# A name that says nothing about the card ("AMD Radeon Graphics" is every APU's market name).
_GENERIC_NAME = re.compile(r"^(amd\s+)?(ati\s+)?radeon(\s*\((tm|r)\))?(\s+graphics)?$", re.I)
_HEX_ID = re.compile(r"^0x[0-9a-f]+$", re.I)
# The GPU named in an APU's CPU brand string: "AMD RYZEN AI MAX+ 395 w/ Radeon 8060S",
# "AMD Ryzen 7 8845HS w/ Radeon 780M Graphics", "AMD Ryzen 5 2400G with Radeon Vega 11 Graphics".
_CPU_GPU = re.compile(r"\bRadeon(?:\s*\((?:TM|R)\))?\s+((?:Pro\s+|Vega\s+)?\d{1,4}[A-Z]{0,2})\b", re.I)
_UUID = re.compile(r"^([0-9a-f]{8})-([0-9a-f]{4})-([0-9a-f]{4})-([0-9a-f]{4})-([0-9a-f]{12})$", re.I)
_VERSION = re.compile(r"\b(\d+\.\d+(?:\.\d+)?)")

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
def _d(value: Any) -> dict:
    """A section of tool output, or {} when the tool printed "N/A" (or anything else) there."""
    return value if isinstance(value, dict) else {}


def present(value: Any) -> Optional[str]:
    """Text, or None for missing / "N/A"-like values (also inside {"value": ..., "unit": ...})."""
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, (dict, list)):
        return None
    t = text(value)
    if t is None or t.strip().lower() in _NA:
        return None
    return t


def decode_gfx(value: Any) -> Optional[str]:
    """'gfx1151', 'gfx90a:sramecc+:xnack-', or KFD's integer 110501 -> 'gfx1151'."""
    if value is None or isinstance(value, (dict, list, bool)):
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
    v = present(value)
    if not v:
        return None
    v = v.lower()
    if v.count(":") == 1:  # "c3:00.0" without a domain
        v = "0000:" + v
    return v


def hex_int(value: Any) -> Optional[int]:
    """0x1586, "0x1586", "0x0000000000000000" -> int; None for N/A and junk."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    v = present(value)
    if v is None:
        return None
    try:
        n = int(v, 16) if v.lower().startswith("0x") else int(v)
    except ValueError:
        return None
    return n if n >= 0 else None


def amdsmi_uuid(serial: int, device_id: int) -> str:
    """The UUID amdsmi gives a GPU (amdsmi_uuid_gen in ROCm's amd_smi_uuid.cc, fcn 0xff):
    the serial's top byte, 0xff and the device id; version 1, variant 2; then the serial's
    bytes 6, 5-4 and 3-0. Lets a rocm-smi reading produce the same key as amd-smi."""
    serial &= (1 << 64) - 1
    time_low = ((serial >> 56) & 0xFF) << 24 | 0xFF << 16 | (device_id & 0xFFFF)
    clk = 0x80 << 8 | ((serial >> 48) & 0xFF)
    return f"{time_low:08x}-0000-1000-{clk:04x}-{(serial >> 32) & 0xFFFF:04x}{serial & 0xFFFFFFFF:08x}"


def uuid_serial(uuid: Optional[str]) -> Optional[int]:
    """The serial number inside an amdsmi UUID; None if it is not shaped like one."""
    m = _UUID.match(uuid or "")
    if not m:
        return None
    time_low, clk, node = int(m.group(1), 16), int(m.group(4), 16), m.group(5)
    return (time_low >> 24) << 56 | (clk & 0xFF) << 48 | int(node[:4], 16) << 32 | int(node[4:], 16)


def unique_uuid(uuid: Optional[str]) -> bool:
    """Does this UUID tell cards apart? Not when it is missing, all zero, or an amdsmi UUID
    without a serial number (the same string on every card of that model)."""
    if not uuid:
        return False
    serial = uuid_serial(uuid)
    if serial is not None:
        return serial != 0
    digits = re.sub(r"[^0-9a-f]", "", uuid.lower().removeprefix("0x"))
    return bool(digits.strip("0"))


def useful_name(value: Any) -> Optional[str]:
    t = present(value)
    if t is None or _HEX_ID.match(t):
        return None  # rocm-smi's "Card Model" is the PCI device id on newer releases
    return t


def gpu_from_cpu_brand(cpu_model: Optional[str]) -> Optional[str]:
    m = _CPU_GPU.search(cpu_model or "")
    return f"AMD Radeon {m.group(1)}" if m else None


def pick_model(names: Iterable[Any], unified: bool, cpu_model: Optional[str]) -> Optional[str]:
    """The most specific name for the card:

    1. the first name the tools gave that is not generic ("AMD Radeon 8060S", a board name);
    2. for an APU whose names are all generic ("AMD Radeon Graphics", "N/A"), the GPU named in the
       CPU's brand string ("... w/ Radeon 8060S" -> "AMD Radeon 8060S"): it is the same chip;
    3. the generic name; None when there is nothing at all (shown as "AMD GPU").
    """
    found = [n for n in (useful_name(v) for v in names) if n]
    specific = [n for n in found if not _GENERIC_NAME.match(n)]
    if specific:
        return specific[0]
    if unified:
        from_cpu = gpu_from_cpu_brand(cpu_model)
        if from_cpu:
            return from_cpu
    return found[0] if found else None


def parse_version(value: Any) -> Optional[str]:
    """'7.0.2-56' or 'ROCm 7.0.2' -> '7.0.2'."""
    t = present(value)
    m = _VERSION.search(t or "")
    return m.group(1) if m else None


def rocm_roots(environ: Optional[dict] = None) -> list[Path]:
    """Where ROCm may be: $ROCM_PATH, $ROCM_HOME, /opt/rocm (the agent image mounts the host's there)."""
    env = os.environ if environ is None else environ
    out: list[Path] = []
    for value in (env.get("ROCM_PATH"), env.get("ROCM_HOME"), "/opt/rocm"):
        if value and Path(value) not in out:
            out.append(Path(value))
    return out


def read_rocm_version(roots: Iterable[Path]) -> Optional[str]:
    """ROCm's own version file, <rocm>/.info/version ("7.0.2-56")."""
    for root in roots:
        try:
            v = parse_version((Path(root) / ".info" / "version").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if v:
            return v
    return None


def _purge_amdsmi() -> None:
    for name in [m for m in sys.modules if m == "amdsmi" or m.startswith("amdsmi.")]:
        del sys.modules[name]


def import_amdsmi(roots: Iterable[Path], importer: Callable[[str], Any] = importlib.import_module
                  ) -> tuple[Any, Optional[str], Optional[str]]:
    """(module, where it came from, why not). Tries the agent's own Python first, then each ROCm's
    <rocm>/share/amd_smi. Fails soft: a wrong Python, a missing or mismatched libamd_smi.so or any
    other error at import time is a reason, never an exception. Nothing the import prints reaches
    stdout (amdsmi_wrapper prints when it cannot find the library, which would corrupt --json)."""
    problems = []
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return importer("amdsmi"), "python", None
    except ImportError as e:
        problems.append(f"amdsmi Python library not importable ({error_text(e)})")
    except Exception as e:  # a broken libamd_smi.so can raise OSError (or KeyError) at import time
        problems.append(f"amdsmi failed to load: {error_text(e)}")
    _purge_amdsmi()
    for root in roots:
        share = Path(root) / "share" / "amd_smi"
        if not (share / "amdsmi" / "__init__.py").is_file():
            continue
        entry = str(share)
        sys.path.insert(0, entry)
        # The wrapper looks for $ROCM_PATH/lib/libamd_smi.so first, when it is imported.
        old = os.environ.get("ROCM_PATH")
        if old is None:
            os.environ["ROCM_PATH"] = str(root)
        printed = io.StringIO()
        try:
            with contextlib.redirect_stdout(printed):
                module = importer("amdsmi")
            return module, entry, None
        except Exception as e:  # incl. SyntaxError from a package newer than this Python
            said = " ".join(printed.getvalue().split())[:160]
            problems.append(f"amdsmi from {entry} failed to load: {error_text(e)}" + (f" ({said})" if said else ""))
            _purge_amdsmi()
            with contextlib.suppress(ValueError):
                sys.path.remove(entry)
        finally:
            if old is None:
                os.environ.pop("ROCM_PATH", None)
    return None, None, "; ".join(problems)


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
                    "cpu_cores": props.get("cpu_cores_count", 0), "simd": props.get("simd_count", 0),
                    # What ROCr calls the agent's UUID (rocminfo "Uuid: GPU-…"), and the render
                    # node (/dev/dri/renderD<minor>) that a container needs for this card only.
                    "unique_id": props.get("unique_id"), "render_minor": props.get("drm_render_minor")}
    return out


def read_cpu_model(path: str | Path = CPUINFO) -> Optional[str]:
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition(":")
            if key.strip().lower() == "model name" and value.strip():
                return value.strip()
    except OSError:
        pass
    return None


def unified_reason(gfx: Optional[str], kfd_node: Optional[dict]) -> Optional[str]:
    if kfd_node and kfd_node.get("cpu_cores", 0) > 0:
        return "kfd: the GPU node also has CPU cores"
    if gfx in APU_GFX:
        return f"{gfx} is an APU target"
    return None


def _ci(d: Any, *names: str) -> Any:
    """Case-insensitive lookup of the first present key (rocm-smi's spelling varies)."""
    lowered = {str(k).strip().lower(): v for k, v in _d(d).items()}
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


def _mib(value: Any) -> Optional[int]:
    """amd-smi's {"value": 98304, "unit": "MB"}: MB there are MiB (bytes // 1024**2)."""
    n = number(value)
    return int(n * MIB) if n is not None else None


REPORTED = ("model", "arch", "vram_total", "vram_used", "driver", "utilization", "temperature")


def make_card(*, index: int, names: Iterable[Any], gfx: Any, uuid: Any, bdf: Any, driver: Optional[str],
              vram_total: Optional[int], vram_used: Optional[int], gtt_total: Optional[int],
              gtt_used: Optional[int], utilization: Optional[int], temperature: Optional[int],
              kfd: dict[str, dict], source: str, cpu_model: Optional[str] = None, amdgpu_driver: Any = None,
              serial: Optional[int] = None, device_id: Optional[int] = None,
              legacy_ids: Iterable[Any] = ()) -> dict:
    """One card. `driver` is the ROCm version; `amdgpu_driver` (the kernel driver's version, as the
    tool printed it) only goes into detail. `legacy_ids` are other identifiers a tool printed for
    the card (rocm-smi's Unique ID) that an older agent may have used in its key."""
    bdf = normalise_bdf(bdf)
    node = kfd.get(bdf) if bdf else None
    gfx = decode_gfx(gfx) or (node or {}).get("gfx")
    why = unified_reason(gfx, node)

    reported_uuid = present(uuid)
    if serial is None:
        serial = uuid_serial(reported_uuid)
    if serial is None and node is not None and node.get("unique_id") is not None:
        serial = node["unique_id"]
    full_uuid = reported_uuid or (amdsmi_uuid(serial, device_id) if serial is not None and device_id is not None
                                  else None)
    unique = unique_uuid(full_uuid)
    key = f"amd:{full_uuid if unique else bdf or index}"
    # The keys Forgeyard <= 1.1.0 may have given this card: amd-smi/amdsmi used the UUID as
    # printed (even a shared one), rocm-smi its Unique ID ("0x0"), both the PCI address without one.
    old = {f"amd:{v}" for v in (reported_uuid, full_uuid, bdf, *(present(i) for i in legacy_ids)) if v}
    if serial is not None:
        old.add(f"amd:{hex(serial)}")
    aliases = sorted(old - {key})

    total, used = vram_total, vram_used
    detail: dict[str, Any] = {"pci_bus_id": bdf, "source": source}
    if node and node.get("unique_id"):
        detail["rocr_uuid"] = f"GPU-{node['unique_id']:016x}"
    if node and node.get("render_minor"):
        detail["render_minor"] = node["render_minor"]
    if full_uuid and not unique:
        detail["shared_uuid"] = full_uuid  # no serial number: the same UUID on every card of this model
    if aliases:
        detail["aliases"] = aliases
    kernel = present(amdgpu_driver)
    if kernel:
        detail["amdgpu_driver"] = kernel[:160]
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
    model = pick_model(names, bool(why), cpu_model)
    card = {
        "key": key, "vendor": "amd", "index": index,
        "model": model or "AMD GPU", "arch": gfx, "uuid": full_uuid if unique else None,
        "vram_total": total, "vram_free": free, "vram_used": used, "unified_memory": bool(why),
        "driver": driver, "utilization": utilization, "temperature": temperature,
    }
    missing = [f for f in REPORTED if (model if f == "model" else card[f]) is None]
    if missing:
        detail["missing"] = missing
    card["detail"] = {k: v for k, v in detail.items() if v is not None}
    return card


# ------------------------------------------------------------------ what each source read, merged
# Each source yields one "reading" per card: make_card's arguments without the shared context
# (KFD, CPU model, ROCm version). Readings of the same card from several sources are merged field
# by field, the first source winning, so a source that answered only in part (amdsmi without
# libdrm: memory but no ASIC info) never stands alone.
LIVE = ("vram_used", "gtt_used", "utilization", "temperature")


def reading(*, index: int, names: Iterable[Any] = (), gfx: Any = None, uuid: Any = None, bdf: Any = None,
            vram_total: Optional[int] = None, vram_used: Optional[int] = None, gtt_total: Optional[int] = None,
            gtt_used: Optional[int] = None, utilization: Optional[int] = None, temperature: Optional[int] = None,
            amdgpu_driver: Any = None, serial: Optional[int] = None, device_id: Optional[int] = None,
            legacy_ids: Iterable[Any] = ()) -> dict:
    """A card as one source saw it, with "N/A"-like values already turned into None."""
    return {"index": index, "names": tuple(n for n in (useful_name(v) for v in names) if n), "gfx": decode_gfx(gfx),
            "uuid": present(uuid), "bdf": normalise_bdf(bdf), "vram_total": vram_total, "vram_used": vram_used,
            "gtt_total": gtt_total, "gtt_used": gtt_used, "utilization": utilization, "temperature": temperature,
            "amdgpu_driver": present(amdgpu_driver), "serial": serial, "device_id": device_id,
            "legacy_ids": tuple(i for i in (present(v) for v in legacy_ids) if i)}


def card_of(r: dict, kfd: dict[str, dict], *, source: str, cpu_model: Optional[str] = None,
            rocm: Optional[str] = None) -> dict:
    return make_card(**r, driver=rocm, kfd=kfd, source=source, cpu_model=cpu_model)


def gaps(r: dict, kfd: dict[str, dict], cpu_model: Optional[str] = None) -> list[str]:
    """What a card still lacks that another source could give: its PCI address (the key, the KFD
    node), its gfx target (which also decides unified memory), its memory size, a name."""
    node = kfd.get(r["bdf"]) if r["bdf"] else None
    gfx = r["gfx"] or (node or {}).get("gfx")
    out = [] if r["bdf"] else ["pci_bus_id"]
    if not gfx:
        out.append("arch")
    if r["vram_total"] is None:
        out.append("vram_total")
    if pick_model(r["names"], bool(unified_reason(gfx, node)), cpu_model) is None:
        out.append("model")
    return out


def fill(base: dict, extra: dict) -> bool:
    """Fill what `base` lacks from `extra` (the same card from another source). True if it gave anything."""
    gave = False
    for k, v in extra.items():
        if k == "index":
            continue
        if k == "names":
            new = tuple(n for n in v if n not in base["names"])
            if new:
                base["names"], gave = base["names"] + new, True
        elif k == "legacy_ids":
            base[k] = base[k] + tuple(i for i in v if i not in base[k])
        elif base.get(k) is None and v is not None:
            base[k], gave = v, True
    return gave


def pair_up(base: list[dict], extra: list[dict]) -> list[tuple[dict, dict]]:
    """The same card in two sources: by PCI address; by position only when one side has no
    address and both list the same number of cards."""
    by_bdf = {e["bdf"]: e for e in extra if e["bdf"]}
    out = []
    for i, b in enumerate(base):
        e = by_bdf.get(b["bdf"]) if b["bdf"] else None
        if e is None and len(base) == len(extra) and not (b["bdf"] and extra[i]["bdf"]):
            e = extra[i]
        if e is not None:
            out.append((b, e))
    return out


def fill_from_kfd(readings: list[dict], kfd: dict[str, dict]) -> bool:
    """The KFD topology names every GPU by PCI address with its gfx target. One card without an
    address and one GPU node: they are the same. True if KFD gave anything (an address or the target)."""
    gave = False
    if len(readings) == 1 and len(kfd) == 1 and not readings[0]["bdf"]:
        readings[0]["bdf"], gave = next(iter(kfd)), True
    for r in readings:
        node = kfd.get(r["bdf"]) if r["bdf"] else None
        if not r["gfx"] and node and node.get("gfx"):
            gave = True  # make_card takes it from the node
    return gave


# ------------------------------------------------------------------ amd-smi / rocm-smi text
def amd_smi_readings(static: Any, metric: Any, listing: Any) -> list[dict]:
    metrics = {integer(m.get("gpu")): m for m in _gpu_list(metric)}
    listed = {integer(m.get("gpu")): m for m in _gpu_list(listing)}
    out = []
    for position, g in enumerate(_gpu_list(static)):
        idx = integer(g.get("gpu"))
        idx = position if idx is None else idx
        asic, bus, vram = _d(g.get("asic")), _d(g.get("bus")), _d(g.get("vram"))
        board, live = _d(g.get("board")), _d(metrics.get(idx))
        m, usage, temps = _d(live.get("mem_usage")), _d(live.get("usage")), _d(live.get("temperature"))
        info = _d(listed.get(idx))
        driver = g.get("driver")
        out.append(reading(
            index=idx, names=(asic.get("market_name"), board.get("product_name")),
            gfx=asic.get("target_graphics_version"), uuid=info.get("uuid"),
            bdf=present(bus.get("bdf")) or present(info.get("bdf")),
            amdgpu_driver=_d(driver).get("version") if isinstance(driver, dict) else driver,
            vram_total=_mib(m.get("total_vram")) or _mib(vram.get("size")), vram_used=_mib(m.get("used_vram")),
            gtt_total=_mib(m.get("total_gtt")), gtt_used=_mib(m.get("used_gtt")),
            utilization=integer(usage.get("gfx_activity")),
            temperature=_celsius(temps.get("edge")) or _celsius(temps.get("hotspot")),
            serial=hex_int(asic.get("asic_serial")), device_id=hex_int(asic.get("device_id"))))
    return out


def rocm_smi_readings(data: Any) -> list[dict]:
    if not isinstance(data, dict):
        return []
    kernel = _ci(data.get("system"), "Driver version")
    out = []
    for name in sorted((k for k in data if re.fullmatch(r"card\d+", str(k).lower())), key=lambda k: int(str(k)[4:])):
        c = _d(data[name])
        uid = present(_ci(c, "Unique ID"))
        out.append(reading(
            index=int(str(name)[4:]),
            names=(_ci(c, "Card Series", "Card series"), _ci(c, "Card Model", "Card model")),
            gfx=_ci(c, "GFX Version"), serial=hex_int(uid), legacy_ids=(uid,),
            device_id=hex_int(_ci(c, "Device ID", "Card Model", "Card model")),
            bdf=_ci(c, "PCI Bus"), amdgpu_driver=kernel,
            vram_total=integer(_ci(c, "VRAM Total Memory (B)")), vram_used=integer(_ci(c, "VRAM Total Used Memory (B)")),
            gtt_total=integer(_ci(c, "GTT Total Memory (B)")), gtt_used=integer(_ci(c, "GTT Total Used Memory (B)")),
            utilization=integer(_ci(c, "GPU use (%)")),
            temperature=_celsius(_ci(c, "Temperature (Sensor edge) (C)", "Temperature (Sensor junction) (C)"))))
    return out


def parse_amd_smi(static: Any, metric: Any, listing: Any, kfd: dict[str, dict], *, cpu_model: Optional[str] = None,
                  rocm: Optional[str] = None) -> list[dict]:
    return [card_of(r, kfd, source="amd-smi", cpu_model=cpu_model, rocm=rocm)
            for r in amd_smi_readings(static, metric, listing)]


def parse_rocm_smi(data: Any, kfd: dict[str, dict], *, cpu_model: Optional[str] = None,
                   rocm: Optional[str] = None) -> list[dict]:
    return [card_of(r, kfd, source="rocm-smi", cpu_model=cpu_model, rocm=rocm) for r in rocm_smi_readings(data)]


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


def _flush_c_stdio() -> None:
    try:
        ctypes.CDLL(None).fflush(None)
    except Exception:
        pass


@contextlib.contextmanager
def c_stdout_to_stderr():
    """libamd_smi is C++: what it prints goes straight to file descriptor 1, past sys.stdout, and
    would land in the middle of `inventory --json`. While the library runs, descriptor 1 is the
    process's stderr (and Python's own prints go there too); C's buffer is flushed before it is put back."""
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        saved = os.dup(1)
        os.dup2(2, 1)
    except OSError:  # no descriptors to juggle (closed stdout): nothing to protect
        saved = None
    try:
        with contextlib.redirect_stdout(sys.stderr):
            yield
    finally:
        if saved is not None:
            _flush_c_stdio()
            os.dup2(saved, 1)
            os.close(saved)


AMD_SMI_STATIC = [["amd-smi", "static", "--asic", "--bus", "--vram", "--driver", "--board", "--json"],
                  ["amd-smi", "static", "--json"]]
AMD_SMI_METRIC = [["amd-smi", "metric", "--mem-usage", "--usage", "--temperature", "--json"],
                  ["amd-smi", "metric", "--json"]]
AMD_SMI_LIST = [["amd-smi", "list", "--json"]]
AMD_SMI_VERSION = [["amd-smi", "version", "--json"]]
ROCM_SMI = ["rocm-smi", "--showproductname", "--showmeminfo", "vram", "gtt", "--showdriverversion", "--showbus",
            "--showuniqueid", "--showuse", "--showtemp", "--json"]

# A source's answer: its readings (empty: nothing), why not, and its raw output for --raw.
Answer = tuple[list[dict], Optional[str], Optional[dict]]


class AmdCollector:
    name = "amd"

    def __init__(self, amdsmi: Any = None, run: Optional[Runner] = None, kfd_root: str | Path = KFD_ROOT,
                 which: Callable[[str], Optional[str]] = shutil.which, rocm_root: Optional[Iterable[str | Path]] = None,
                 cpuinfo: str | Path = CPUINFO, importer: Callable[[str], Any] = importlib.import_module):
        self._lib_module = amdsmi
        self._run = run or run_command
        self._kfd_root = kfd_root
        self._which = which
        self._roots = [Path(r) for r in rocm_root] if rocm_root is not None else rocm_roots()
        self._cpuinfo = cpuinfo
        self._importer = importer
        self._backend: Optional[str] = None
        self._label: Optional[str] = None
        self._merged: list[dict] = []
        self._lib: Any = None
        self._lib_from: Optional[str] = None
        self._lib_started = False
        self._handles: list[tuple[int, Any]] = []
        self._kfd: dict[str, dict] = {}
        self._cpu_model: Optional[str] = None
        self._rocm: Optional[str] = None
        self._static: Any = None
        self._listing: Any = None

    # -------------------------------------------------------------- amdsmi library
    def _load_lib(self) -> tuple[Any, Optional[str]]:
        if self._lib is not None:
            return self._lib, None
        if self._lib_module is not None:
            lib, where = self._lib_module, "python"
        else:
            lib, where, problem = import_amdsmi(self._roots, self._importer)
            if lib is None:
                return None, problem
        try:
            lib.amdsmi_init()
        except Exception as e:
            return None, f"amdsmi_init failed: {error_text(e)}"
        self._lib, self._lib_from, self._lib_started = lib, where, True
        return lib, None

    @staticmethod
    def _call(fn, *args, default=None):
        try:
            return fn(*args)
        except Exception:
            return default

    def _get(self, lib, name: str, *args, default=None):
        """A library call that may not exist in this amdsmi version."""
        fn = getattr(lib, name, None)
        return self._call(fn, *args, default=default) if fn is not None else default

    def _lib_memory(self, lib, h) -> dict:
        mt = lib.AmdSmiMemoryType
        return {
            "vram_total": integer(self._get(lib, "amdsmi_get_gpu_memory_total", h, mt.VRAM)),
            "vram_used": integer(self._get(lib, "amdsmi_get_gpu_memory_usage", h, mt.VRAM)),
            "gtt_total": integer(self._get(lib, "amdsmi_get_gpu_memory_total", h, mt.GTT)),
            "gtt_used": integer(self._get(lib, "amdsmi_get_gpu_memory_usage", h, mt.GTT)),
        }

    def _lib_live(self, lib, h) -> dict:
        activity = self._get(lib, "amdsmi_get_gpu_activity", h)
        temp = None
        for sensor in ("EDGE", "HOTSPOT"):
            t = getattr(lib.AmdSmiTemperatureType, sensor, None)
            if t is not None:
                temp = _celsius(self._get(lib, "amdsmi_get_temp_metric", h, t, lib.AmdSmiTemperatureMetric.CURRENT))
            if temp:
                break
        return {"utilization": integer(_d(activity).get("gfx_activity")), "temperature": temp}

    def _lib_reading(self, lib, index: int, h, raw_out: Optional[list]) -> dict:
        asic = _d(self._get(lib, "amdsmi_get_gpu_asic_info", h))
        board = _d(self._get(lib, "amdsmi_get_gpu_board_info", h))
        driver = _d(self._get(lib, "amdsmi_get_gpu_driver_info", h))
        uuid = text(self._get(lib, "amdsmi_get_gpu_device_uuid", h))
        bdf = text(self._get(lib, "amdsmi_get_gpu_device_bdf", h))
        mem, live = self._lib_memory(lib, h), self._lib_live(lib, h)
        if raw_out is not None:
            raw_out.append({"index": index, "asic_info": asic, "board_info": board, "driver_info": driver,
                            "uuid": uuid, "bdf": bdf, "memory": mem, **live})
        return reading(index=index, names=(asic.get("market_name"), board.get("product_name")),
                       gfx=asic.get("target_graphics_version"), uuid=uuid, bdf=bdf,
                       amdgpu_driver=driver.get("driver_version"), serial=hex_int(asic.get("asic_serial")),
                       device_id=hex_int(asic.get("device_id")), **mem, **live)

    def _from_lib(self, raw: bool) -> Answer:
        with c_stdout_to_stderr():
            lib, problem = self._load_lib()
            if lib is None:
                return [], problem, None
            try:
                handles = list(lib.amdsmi_get_processor_handles())
            except Exception as e:
                return [], f"amdsmi could not list GPUs: {error_text(e)}", None
            if not handles:
                return [], "amdsmi found no GPUs", None
            if self._rocm is None:
                ok_version = self._get(lib, "amdsmi_get_rocm_version")
                if isinstance(ok_version, (tuple, list)) and len(ok_version) == 2 and ok_version[0] is True:
                    self._rocm = parse_version(ok_version[1])
            raw_out: Optional[list] = [] if raw else None
            self._handles = list(enumerate(handles))
            readings = [self._lib_reading(lib, i, h, raw_out) for i, h in self._handles]
        return readings, None, {"amdsmi": raw_out} if raw else None

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

    def _cli_rocm_version(self) -> Optional[str]:
        if self._rocm is None and self._which("amd-smi"):
            data, _ = self._first_json(AMD_SMI_VERSION)
            for item in (data if isinstance(data, list) else [data]):
                self._rocm = self._rocm or parse_version(_d(item).get("rocm_version"))
        return self._rocm

    def _from_amd_smi(self, raw: bool) -> Answer:
        if not self._which("amd-smi"):
            return [], "amd-smi not on PATH", None
        static, problem = self._first_json(AMD_SMI_STATIC)
        if static is None:
            return [], f"amd-smi static failed: {problem}", None
        metric, _ = self._first_json(AMD_SMI_METRIC)
        listing, _ = self._first_json(AMD_SMI_LIST)
        readings = amd_smi_readings(static, metric, listing)
        if not readings:
            return [], "amd-smi listed no GPUs", None
        self._cli_rocm_version()
        self._static, self._listing = static, listing
        return readings, None, {"static": static, "metric": metric, "list": listing} if raw else None

    def _from_rocm_smi(self, raw: bool) -> Answer:
        if not self._which("rocm-smi"):
            return [], "rocm-smi not on PATH", None
        data, problem = self._first_json([ROCM_SMI])
        if data is None:
            return [], f"rocm-smi failed: {problem}", None
        readings = rocm_smi_readings(data)
        if not readings:
            return [], "rocm-smi listed no GPUs", None
        self._cli_rocm_version()
        return readings, None, {"rocm-smi": data} if raw else None

    # -------------------------------------------------------------- collector interface
    def _incomplete(self, readings: list[dict]) -> bool:
        return any(gaps(r, self._kfd, self._cpu_model) for r in readings)

    def _cards(self, readings: list[dict]) -> list[dict]:
        return [card_of(r, self._kfd, source=self._label or "-", cpu_model=self._cpu_model, rocm=self._rocm)
                for r in readings]

    def inventory(self, raw: bool = False) -> Result:
        """The first source that lists the cards is the base. While a card still lacks its address,
        gfx target, memory size or a name, the next sources (then the KFD topology) fill the gaps;
        a partial answer never stands alone. The source label names every one that contributed."""
        self._kfd = read_kfd(self._kfd_root)
        self._cpu_model = read_cpu_model(self._cpuinfo)
        self._rocm = read_rocm_version(self._roots)
        base: Optional[list[dict]] = None
        used, tried, raws = [], [], {}
        for backend, fn in (("amdsmi", self._from_lib), ("amd-smi", self._from_amd_smi), ("rocm-smi", self._from_rocm_smi)):
            if base is not None and not self._incomplete(base):
                break
            try:
                readings, problem, raw_part = fn(raw)
            except Exception as e:  # a parser surprise must not take the agent down
                readings, problem, raw_part = [], f"{backend}: {error_text(e)}", None
            raws.update(raw_part or {})
            if not readings:
                tried.append(problem or backend)
            elif base is None:
                base, used, self._backend = readings, [backend], backend
            elif any([fill(b, e) for b, e in pair_up(base, readings)]):
                used.append(backend)
        if base is None:
            self._backend, self._merged = None, []
            return Result(ok=False, source=None, reason="; ".join(tried),
                          raw={"kfd": self._kfd, "skipped": tried} if raw else None)
        if fill_from_kfd(base, self._kfd):
            used.append("kfd")
        self._label, self._merged = "+".join(used), [dict(r) for r in base]
        cards = self._cards(base)
        notes = []
        if tried:
            notes.append(("used " + self._label + " (" + "; ".join(tried) + ")"))
        elif self._lib_from not in (None, "python") and self._backend == "amdsmi":
            notes.append(f"amdsmi from {self._lib_from}")
        left = [f"#{r['index']}: {', '.join(g)}" for r in base if (g := gaps(r, self._kfd, self._cpu_model))]
        if left:
            notes.append("still unknown: " + "; ".join(left))
        out = {**raws, "kfd": self._kfd, "skipped": tried, "used": used, "rocm_version": self._rocm,
               "cpu_model": self._cpu_model} if raw else None
        return Result(accelerators=cards, ok=True, source=self._label, reason="; ".join(notes) or None, raw=out)

    def _fresh(self) -> list[dict]:
        """The base source's current readings (for the live fields)."""
        if self._backend == "amdsmi":
            with c_stdout_to_stderr():
                return [self._lib_reading(self._lib, i, h, None) for i, h in self._handles]
        if self._backend == "amd-smi":
            metric, _ = self._first_json(AMD_SMI_METRIC)
            return amd_smi_readings(self._static, metric, self._listing)
        if self._backend == "rocm-smi":
            data, _ = self._first_json([ROCM_SMI])
            return rocm_smi_readings(data)
        return []

    def metrics(self) -> list[dict]:
        """Live values from the base source on top of what the inventory merged, so the keys match it."""
        try:
            fresh = self._fresh()
            if len(fresh) == len(self._merged):
                readings = []
                for merged, now in zip(self._merged, fresh):
                    r = dict(merged)
                    r.update({k: now[k] for k in LIVE if now[k] is not None})
                    readings.append(r)
            else:
                readings = fresh
            cards = self._cards(readings)
        except Exception:
            return []
        return [{k: c[k] for k in ("key", "vram_free", "vram_used", "utilization", "temperature")} for c in cards]

    def close(self) -> None:
        if self._lib_started:
            with c_stdout_to_stderr():
                self._call(self._lib.amdsmi_shut_down)
            self._lib_started = False
            self._lib = None
