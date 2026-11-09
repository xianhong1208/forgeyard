"""The CPU and system RAM, from /proc (Linux). Always reported, as one accelerator row of
vendor `cpu`, so CPU-only machines go through the same scheduling later."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Optional

from forgeyard.agent.inventory.base import Result, error_text

# Instruction-set extensions that matter for CPU inference (llama.cpp and friends).
ISA_FLAGS = ("avx", "avx2", "fma", "f16c", "avx512f", "avx512_bf16", "avx512_vnni", "avx_vnni", "amx_tile", "amx_bf16",
             "amx_int8", "asimd", "sve", "sve2", "i8mm", "bf16")


def parse_cpuinfo(content: str) -> dict:
    blocks = [b for b in content.split("\n\n") if b.strip()]
    processors = []
    common: dict[str, str] = {}
    for block in blocks:
        entry: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            entry[key.strip().lower()] = value.strip()
        if "processor" in entry and entry["processor"].isdigit():
            processors.append(entry)
        else:
            common.update(entry)  # ARM puts "Hardware" / "Model" in a trailing block
    first = processors[0] if processors else {}
    model = first.get("model name") or common.get("model") or common.get("hardware") or first.get("cpu model")
    flags = set((first.get("flags") or first.get("features") or "").split())
    cores = {(p.get("physical id", "0"), p.get("core id")) for p in processors if p.get("core id") is not None}
    sockets = {p.get("physical id") for p in processors if p.get("physical id") is not None}
    return {
        "model": model,
        "threads": len(processors),
        "cores": len(cores) or len(processors),
        "sockets": len(sockets) or 1,
        "isa": [f for f in ISA_FLAGS if f in flags],
    }


def parse_meminfo(content: str) -> dict:
    out = {}
    for line in content.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key.strip()] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1].lower() == "kb" else 1)
    return out


def parse_stat(content: str) -> Optional[tuple[int, int]]:
    """(busy, total) jiffies from the aggregate cpu line."""
    for line in content.splitlines():
        if line.startswith("cpu "):
            values = [int(v) for v in line.split()[1:] if v.isdigit()]
            if len(values) < 4:
                return None
            idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
            total = sum(values[:8])
            return total - idle, total
    return None


class CpuCollector:
    name = "cpu"

    def __init__(self, proc: str | Path = "/proc"):
        self.proc = Path(proc)
        self._last_stat: Optional[tuple[int, int]] = None

    def _read(self, name: str) -> str:
        return (self.proc / name).read_text(encoding="utf-8", errors="replace")

    def _utilization(self) -> Optional[int]:
        try:
            now = parse_stat(self._read("stat"))
        except OSError:
            return None
        before, self._last_stat = self._last_stat, now
        if not now or not before or now[1] <= before[1]:
            return None
        return round(100 * (now[0] - before[0]) / (now[1] - before[1]))

    def _memory(self) -> tuple[Optional[int], Optional[int]]:
        try:
            mem = parse_meminfo(self._read("meminfo"))
            return mem.get("MemTotal"), mem.get("MemAvailable", mem.get("MemFree"))
        except OSError:
            try:
                pages, size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
                return pages * size, None
            except (ValueError, OSError, AttributeError):
                return None, None

    def inventory(self, raw: bool = False) -> Result:
        try:
            info = parse_cpuinfo(self._read("cpuinfo"))
        except OSError:
            info = {"model": None, "threads": os.cpu_count() or 0, "cores": os.cpu_count() or 0, "sockets": 1, "isa": []}
        try:
            total, free = self._memory()
            self._utilization()  # first sample; heartbeats report the difference
            row = {
                "key": "cpu:0", "vendor": "cpu", "index": 0,
                "model": info["model"] or platform.processor() or platform.machine() or "CPU",
                "arch": platform.machine() or None,
                "vram_total": total, "vram_free": free,
                "vram_used": total - free if total is not None and free is not None else None,
                "unified_memory": False, "driver": None, "utilization": None, "temperature": None,
                "detail": {"cores": info["cores"], "threads": info["threads"], "sockets": info["sockets"],
                           "isa": info["isa"]},
            }
        except Exception as e:  # never let the CPU row take the agent down
            return Result(ok=False, source="/proc", reason=f"Could not read the CPU: {error_text(e)}")
        return Result(accelerators=[row], ok=True, source="/proc",
                      raw={"cpuinfo": info} if raw else None)

    def metrics(self) -> list[dict]:
        total, free = self._memory()
        return [{"key": "cpu:0", "vram_free": free,
                 "vram_used": total - free if total is not None and free is not None else None,
                 "utilization": self._utilization()}]

    def close(self) -> None:
        pass
