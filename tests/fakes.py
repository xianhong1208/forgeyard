"""Stand-ins for pynvml and amdsmi, driven by recorded values in tests/fixtures.

Both refuse any function a read-only collector has no business calling (setters, process
control), so a test fails if the agent ever reaches for one.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

FIXTURES = Path(__file__).with_name("fixtures")


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


class FakeNvml:
    NVML_TEMPERATURE_GPU = 0
    READ_ONLY = ("nvmlInit", "nvmlShutdown", "nvmlSystemGet", "nvmlDeviceGet")

    def __init__(self, data: dict, *, bytes_strings: bool = False, v2: bool = True, init_error: str | None = None):
        self._data = data
        self._bytes = bytes_strings
        self._init_error = init_error
        self.calls: list[str] = []
        if v2:
            self.nvmlMemory_v2 = 0x02000028
        if "cuda_driver" in data:
            self.nvmlSystemGetCudaDriverVersion_v2 = self._cuda

    def __getattribute__(self, name):
        if name.startswith("nvml") and not name.startswith("nvmlMemory"):
            if not name.startswith(FakeNvml.READ_ONLY):
                raise AssertionError(f"the collector called {name}, which is not read-only")
            object.__getattribute__(self, "calls").append(name)
        return object.__getattribute__(self, name)

    def _s(self, value: str):
        return value.encode() if self._bytes else value

    def _cuda(self):
        return self._data["cuda_driver"]

    def nvmlInit(self):
        if self._init_error:
            raise RuntimeError(self._init_error)

    def nvmlShutdown(self):
        pass

    def nvmlSystemGetDriverVersion(self):
        return self._s(self._data["driver"])

    def nvmlDeviceGetCount(self):
        return len(self._data["devices"])

    def nvmlDeviceGetHandleByIndex(self, i):
        dev = self._data["devices"][i]
        if dev.get("lost"):
            raise RuntimeError("GPU is lost")
        return i

    def _dev(self, h):
        return self._data["devices"][h]

    def nvmlDeviceGetName(self, h):
        return self._s(self._dev(h)["name"])

    def nvmlDeviceGetUUID(self, h):
        return self._s(self._dev(h)["uuid"])

    def nvmlDeviceGetCudaComputeCapability(self, h):
        return tuple(self._dev(h)["compute_capability"])

    def nvmlDeviceGetMemoryInfo(self, h, version=None):
        m = self._dev(h)["memory"]
        if m is None:
            raise RuntimeError("Not Supported")
        if version:
            return SimpleNamespace(total=m["total"], free=m["free"], used=m["used"], reserved=m["reserved"])
        return SimpleNamespace(total=m["total"], free=m["free"], used=m["total"] - m["free"])

    def nvmlDeviceGetUtilizationRates(self, h):
        return SimpleNamespace(gpu=self._dev(h)["utilization"], memory=0)

    def nvmlDeviceGetTemperature(self, h, sensor):
        return self._dev(h)["temperature"]

    def nvmlDeviceGetPciInfo(self, h):
        return SimpleNamespace(busId=self._s(self._dev(h)["pci_bus_id"]))


class FakeAmdsmi:
    AmdSmiMemoryType = SimpleNamespace(VRAM=0, VIS_VRAM=1, GTT=2)
    AmdSmiTemperatureType = SimpleNamespace(EDGE=0, HOTSPOT=1, VRAM=2)
    AmdSmiTemperatureMetric = SimpleNamespace(CURRENT=0)
    _MEM = {0: "VRAM", 1: "VIS_VRAM", 2: "GTT"}
    _TEMP = {0: "EDGE", 1: "HOTSPOT", 2: "VRAM"}

    def __init__(self, data: dict, init_error: str | None = None):
        self._data = data
        self._init_error = init_error
        self.calls: list[str] = []

    def __getattribute__(self, name):
        if name.startswith("amdsmi_"):
            if not (name.startswith("amdsmi_get_") or name in ("amdsmi_init", "amdsmi_shut_down")):
                raise AssertionError(f"the collector called {name}, which is not read-only")
            object.__getattribute__(self, "calls").append(name)
        return object.__getattribute__(self, name)

    def amdsmi_init(self, *flags):
        if self._init_error:
            raise RuntimeError(self._init_error)

    def amdsmi_shut_down(self):
        pass

    def amdsmi_get_processor_handles(self):
        return list(range(len(self._data["handles"])))

    def _h(self, h):
        return self._data["handles"][h]

    def amdsmi_get_gpu_asic_info(self, h):
        return dict(self._h(h)["asic_info"])

    def amdsmi_get_gpu_driver_info(self, h):
        return dict(self._h(h)["driver_info"])

    def amdsmi_get_gpu_device_uuid(self, h):
        return self._h(h)["uuid"]

    def amdsmi_get_gpu_device_bdf(self, h):
        return self._h(h)["bdf"]

    def amdsmi_get_gpu_memory_total(self, h, kind):
        return self._h(h)["memory"][self._MEM[kind]][0]

    def amdsmi_get_gpu_memory_usage(self, h, kind):
        return self._h(h)["memory"][self._MEM[kind]][1]

    def amdsmi_get_gpu_activity(self, h):
        return dict(self._h(h)["activity"])

    def amdsmi_get_rocm_version(self):
        if "rocm_version" not in self._data:
            raise RuntimeError("librocm-core.so not found")
        return True, self._data["rocm_version"]

    def amdsmi_get_temp_metric(self, h, sensor, metric):
        value = self._h(h)["temperature"].get(self._TEMP[sensor])
        if value is None:
            raise RuntimeError("AMDSMI_STATUS_NOT_SUPPORTED")
        return value


def fake_runner(outputs: dict):
    """A subprocess stand-in: maps a command (joined by spaces) to (exit code, stdout, stderr)."""
    seen: list[str] = []

    def run(cmd):
        key = " ".join(cmd)
        seen.append(key)
        if key in outputs:
            return outputs[key]
        return 2, "", f"unknown command {key}"

    run.seen = seen
    return run


def make_kfd(root: Path, nodes: list[dict]) -> Path:
    """Write a KFD topology tree: nodes/<n>/properties with 'key value' lines."""
    for n, props in enumerate(nodes):
        d = root / str(n)
        d.mkdir(parents=True)
        (d / "properties").write_text("".join(f"{k} {v}\n" for k, v in props.items()))
    return root


def kfd_location(bus: int, dev: int = 0, fn: int = 0) -> int:
    return (bus << 8) | (dev << 3) | fn
