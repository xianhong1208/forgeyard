"""NVIDIA cards through NVML (the `nvidia-ml-py` package, imported as `pynvml`).

NVML is the read-only management library behind nvidia-smi: nvmlInit() does not create a
CUDA context and nothing here allocates GPU memory, so it is safe on cards busy with other
work. API reference: https://docs.nvidia.com/deploy/nvml-api/

Fields used (all from nvidia-ml-py, confirmed against a real 8 x A100 80GB PCIe host with
driver 580.178.04):
  nvmlSystemGetDriverVersion()              -> "580.178.04" (str; bytes in old pynvml)
  nvmlSystemGetCudaDriverVersion_v2()       -> 13000  (major*1000 + minor*10 = CUDA 13.0)
  nvmlDeviceGetName(h)                      -> "NVIDIA A100 80GB PCIe"
  nvmlDeviceGetUUID(h)                      -> "GPU-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  nvmlDeviceGetCudaComputeCapability(h)     -> (8, 0), reported as arch "8.0"
  nvmlDeviceGetMemoryInfo(h[, nvmlMemory_v2]) -> .total / .free / .used (bytes); v2 adds .reserved
  nvmlDeviceGetUtilizationRates(h).gpu      -> percent
  nvmlDeviceGetTemperature(h, NVML_TEMPERATURE_GPU) -> degrees C
  nvmlDeviceGetPciInfo(h).busId             -> "00000000:17:00.0"
"""

from __future__ import annotations

import importlib
from typing import Any, Optional

from forgeyard.agent.inventory.base import Result, error_text, integer, text

INSTALL_HINT = "nvidia-ml-py is not installed (uv sync --extra nvidia)"


class NvidiaCollector:
    name = "nvidia"

    def __init__(self, nvml: Any = None):
        self._nvml = nvml  # tests pass a fake module
        self._started = False
        self._handles: dict[str, Any] = {}

    def _start(self) -> tuple[Any, Optional[str]]:
        try:
            nv = self._nvml or importlib.import_module("pynvml")
        except ImportError:
            return None, INSTALL_HINT
        if not self._started:
            try:
                nv.nvmlInit()
            except Exception as e:  # NVMLError_LibraryNotFound, _DriverNotLoaded, ...
                return None, f"NVML did not start (no NVIDIA driver?): {error_text(e)}"
            self._started = True
        return nv, None

    @staticmethod
    def _call(fn, *args, default=None):
        try:
            return fn(*args)
        except Exception:
            return default

    def _memory(self, nv, handle) -> dict:
        info = None
        v2 = getattr(nv, "nvmlMemory_v2", None)
        if v2 is not None:
            try:
                info = nv.nvmlDeviceGetMemoryInfo(handle, version=v2)
            except Exception:
                info = None
        if info is None:
            info = self._call(nv.nvmlDeviceGetMemoryInfo, handle)
        if info is None:  # e.g. NOT_SUPPORTED on some unified-memory systems
            return {"vram_total": None, "vram_free": None, "vram_used": None, "reserved": None}
        total, free = integer(getattr(info, "total", None)), integer(getattr(info, "free", None))
        used = integer(getattr(info, "used", None))
        if used is None and total is not None and free is not None:
            used = total - free
        return {"vram_total": total, "vram_free": free, "vram_used": used,
                "reserved": integer(getattr(info, "reserved", None))}

    def _live(self, nv, handle) -> dict:
        util = self._call(nv.nvmlDeviceGetUtilizationRates, handle)
        temp_sensor = getattr(nv, "NVML_TEMPERATURE_GPU", 0)
        return {
            "utilization": integer(getattr(util, "gpu", None)) if util is not None else None,
            "temperature": integer(self._call(nv.nvmlDeviceGetTemperature, handle, temp_sensor)),
        }

    def inventory(self, raw: bool = False) -> Result:
        nv, problem = self._start()
        if nv is None:
            return Result(ok=False, source="nvml", reason=problem)
        try:
            count = nv.nvmlDeviceGetCount()
        except Exception as e:
            return Result(ok=False, source="nvml", reason=f"NVML could not count GPUs: {error_text(e)}")
        driver = text(self._call(nv.nvmlSystemGetDriverVersion))
        get_cuda = getattr(nv, "nvmlSystemGetCudaDriverVersion_v2", None) or getattr(nv, "nvmlSystemGetCudaDriverVersion", None)
        cuda_raw = self._call(get_cuda) if get_cuda else None
        cuda = f"{cuda_raw // 1000}.{(cuda_raw % 1000) // 10}" if isinstance(cuda_raw, int) and cuda_raw > 0 else None
        cards, skipped, raw_cards = [], [], []
        self._handles = {}
        for i in range(count):
            try:
                h = nv.nvmlDeviceGetHandleByIndex(i)
            except Exception as e:
                skipped.append(f"GPU {i}: {error_text(e)}")
                continue
            uuid = text(self._call(nv.nvmlDeviceGetUUID, h))
            cc = self._call(nv.nvmlDeviceGetCudaComputeCapability, h)
            arch = f"{cc[0]}.{cc[1]}" if cc and len(cc) == 2 else None
            mem = self._memory(nv, h)
            key = f"nvidia:{uuid or i}"
            self._handles[key] = h
            detail = {"sm": f"sm_{cc[0]}{cc[1]}" if arch else None, "cuda": cuda,
                      "pci_bus_id": text(getattr(self._call(nv.nvmlDeviceGetPciInfo, h), "busId", None)),
                      "vram_reserved": mem["reserved"]}
            card = {
                "key": key, "vendor": "nvidia", "index": i,
                "model": text(self._call(nv.nvmlDeviceGetName, h)) or "NVIDIA GPU", "arch": arch, "uuid": uuid,
                "vram_total": mem["vram_total"], "vram_free": mem["vram_free"], "vram_used": mem["vram_used"],
                "unified_memory": False, "driver": driver, **self._live(nv, h),
                "detail": {k: v for k, v in detail.items() if v is not None},
            }
            cards.append(card)
            if raw:
                raw_cards.append({"index": i, "uuid": uuid, "compute_capability": list(cc) if cc else None, **mem})
        reason = "; ".join(skipped) if skipped else (None if cards else "NVML found no GPUs")
        return Result(accelerators=cards, ok=bool(cards), source="nvml", reason=reason,
                      raw={"driver": driver, "cuda_driver": cuda_raw, "count": count, "devices": raw_cards} if raw else None)

    def metrics(self) -> list[dict]:
        if not self._handles:
            return []
        nv = self._nvml or importlib.import_module("pynvml")
        out = []
        for key, h in self._handles.items():
            mem = self._memory(nv, h)
            out.append({"key": key, "vram_free": mem["vram_free"], "vram_used": mem["vram_used"], **self._live(nv, h)})
        return out

    def close(self) -> None:
        if self._started:
            nv = self._nvml or importlib.import_module("pynvml")
            self._call(nv.nvmlShutdown)
            self._started = False
