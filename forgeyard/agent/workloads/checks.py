"""Health checks (forgeyard/agent/workloads/supervisor.py, step 4): a service's probe, its start period, and a
guarded process engine that listens beyond loopback stopped. `HealthChecks` is that part of the
`Supervisor`."""

from __future__ import annotations

import asyncio

from forgeyard.agent.workloads import plan, starts
from forgeyard.agent.workloads.lifecycle import LIVE
from forgeyard.agent.workloads.process import beyond_loopback
from forgeyard.agent.workloads.records import Record
from forgeyard.machines.protocol import FEATURE_START_TIMES


class HealthChecks:
    """The `Supervisor`'s health probes, each its task in `probes`."""

    # ------------------------------------------------------------------ health
    def _maybe_probe(self, rec: Record, now: float) -> None:
        h = rec.spec["health"]
        running = self.probes.get(rec.id)
        if running is not None and not running.done():
            return
        if rec.started_at and now - rec.started_at < int(h.get("initial_delay") or 0):
            return
        # 1.5.0 ("start_times"): every second while it starts, then at the recipe's interval.
        fast = FEATURE_START_TIMES in self.server_features
        if rec.last_probe_at and now - rec.last_probe_at < starts.probe_interval(rec, h, now, fast):
            return
        port = next((p for p in rec.ports if p["name"] == h.get("port")), rec.ports[0] if rec.ports else None)
        if port is None:
            return
        if self._guarded(rec) and rec.id not in self.keys:
            return  # adopted after an agent restart; probed again once the server sent its key
        rec.last_probe_at = now
        if port.get("internal"):
            # Behind the guard: the engine itself, on loopback. The guard has nothing open, not even
            # the health path; that it listens is known here without asking it.
            host, number = plan.LOOPBACK, int(port["internal"])
        elif self.address in ("0.0.0.0", "::") or (rec.runtime == "process" and rec.model is None):
            # Processes usually listen on every address (probe loopback); a model engine run as a
            # process binds only the agent's address, like a container's published port.
            host, number = "127.0.0.1", int(port["host_port"])
        else:
            host, number = self.address, int(port["host_port"])
        self.probes[rec.id] = asyncio.create_task(self._probe(rec, h, host, number))

    async def _probe(self, rec: Record, h: dict, host: str, port: int) -> None:
        args = (h.get("type", "http"), host, port, h.get("path") or "/", float(h.get("timeout") or 3))
        # An exposed workload's program never sees the internal key, not even from its health check.
        key = self.keys.get(rec.id) if self._guarded(rec) and rec.id not in self.strip else None
        if key is not None:  # speaches guards /health too; vLLM and llama.cpp ignore it there
            err = await asyncio.to_thread(self.prober, *args, headers={"Authorization": f"Bearer {key[1]}"})
        else:
            err = await asyncio.to_thread(self.prober, *args)
        if self.records.get(rec.id) is not rec or rec.state not in LIVE or rec.id in self.ops:
            return
        if err is None and rec.runtime == "process" and plan.guarded(rec.ports):
            open_on = await asyncio.to_thread(beyond_loopback, [int(p["internal"]) for p in rec.ports])
            if open_on:
                # The engine ignored ${BIND_ADDRESS} and listens beyond loopback: it could be used
                # around the guard. Stopped, never served.
                self.stopping.add(rec.id)
                self._launch(rec.id, self._exposed(rec, open_on))
                return
        if err is None and self._guarded(rec) and rec.id in self.guard_errors:
            err = self.guard_errors[rec.id]
        before = (rec.state, rec.reason)
        if err is None:
            starts.finish(rec, self.clock())  # 1.5.0: the start is over (once: it clears its marks)
            rec.health_failures = 0
            rec.healthy_seen = True
            rec.loading = False
            rec.state, rec.reason = "healthy", None
        elif (not rec.healthy_seen and rec.started_at
              and self.clock() - rec.started_at < int(h.get("start_period") or 0)):
            # Still within the start period (a model loading): failures do not count yet.
            rec.reason = "loading; waiting for the health check to pass"
        else:
            rec.health_failures += 1
            if rec.health_failures >= int(h.get("failure_threshold") or 3):
                rec.state, rec.reason = "unhealthy", err[:300]
                rec.loading = False
        if (rec.state, rec.reason) != before:
            self._save(rec)
            self._notify(rec)
