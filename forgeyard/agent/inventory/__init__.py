"""The machine's inventory: every collector's cards plus a status per collector."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from forgeyard.agent.inventory.amd import AmdCollector
from forgeyard.agent.inventory.base import Result, error_text
from forgeyard.agent.inventory.cpu import CpuCollector
from forgeyard.agent.inventory.nvidia import NvidiaCollector

METRIC_FIELDS = ("key", "vram_free", "vram_used", "utilization", "temperature")

# Simulated inventory replaces real hardware with a fixture. It exists for development and
# tests on machines without the hardware, and needs this variable set to 1 on top of the flag.
SIMULATION_ENV = "FORGEYARD_AGENT_SIMULATION"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class SimulationNotAllowed(RuntimeError):
    pass


def fingerprint(inventory: dict) -> list:
    """What counts as a hardware change: cards appearing, disappearing or changing size."""
    return sorted((a.get("key"), a.get("model"), a.get("arch"), a.get("vram_total"), a.get("unified_memory"))
                  for a in inventory.get("accelerators", []))


class Inventory:
    simulated = False

    def __init__(self, collectors: Optional[list] = None):
        self.collectors = collectors if collectors is not None else [NvidiaCollector(), AmdCollector(), CpuCollector()]

    def full(self, raw: bool = False) -> dict:
        cards, status, raws = [], {}, {}
        for c in self.collectors:
            try:
                result = c.inventory(raw=raw)
            except Exception as e:  # collectors fail soft already; this is the last net
                result = Result(ok=False, reason=f"collector crashed: {error_text(e)}")
            cards.extend(result.accelerators)
            status[c.name] = result.status()
            if raw and result.raw is not None:
                raws[c.name] = result.raw
        cards.sort(key=lambda a: (a["vendor"] == "cpu", a["vendor"], a.get("index", 0)))
        out = {"accelerators": cards, "collectors": status}
        if raw:
            out["raw"] = raws
        return out

    def metrics(self) -> list[dict]:
        out = []
        for c in self.collectors:
            try:
                out.extend(c.metrics())
            except Exception:
                continue
        return out

    def close(self) -> None:
        for c in self.collectors:
            try:
                c.close()
            except Exception:
                pass


def fixture_path(name_or_path: str) -> Path:
    p = Path(name_or_path)
    if p.suffix == ".json" and p.is_file():
        return p
    candidate = FIXTURES / f"{name_or_path}.json"
    if candidate.is_file():
        return candidate
    known = ", ".join(sorted(f.stem for f in FIXTURES.glob("*.json")))
    raise FileNotFoundError(f"No inventory fixture {name_or_path!r}. Built in: {known}; or give a path to a .json file.")


class SimulatedInventory(Inventory):
    """Inventory read from a fixture (the output of `forgeyard agent inventory --json`)."""

    simulated = True

    def __init__(self, name_or_path: str, environ: Optional[dict] = None):
        env = os.environ if environ is None else environ
        if env.get(SIMULATION_ENV) != "1":
            raise SimulationNotAllowed(
                f"Simulated inventory is for development and tests only. Set {SIMULATION_ENV}=1 to allow it; "
                "the server will then show this machine as simulated.")
        self.path = fixture_path(name_or_path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._accelerators = list(data.get("accelerators", []))
        self._collectors = dict(data.get("collectors", {}))
        super().__init__(collectors=[])

    def full(self, raw: bool = False) -> dict:
        status = dict(self._collectors)
        status["simulation"] = {"ok": True, "count": len(self._accelerators), "source": f"fixture {self.path.name}"}
        return {"accelerators": [dict(a) for a in self._accelerators], "collectors": status}

    def metrics(self) -> list[dict]:
        return [{k: a.get(k) for k in METRIC_FIELDS} for a in self._accelerators]
