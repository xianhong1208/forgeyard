"""What the agent remembers about each replica, on disk, so a restarted agent adopts what it
started instead of starting it twice.

One JSON file per replica in <state>/replicas/, mode 0600, written atomically. Logs live in
<state>/logs/<replica>/ (owned by the agent; the process only inherits an open descriptor).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

log = logging.getLogger("forgeyard.agent")

ENDED = ("exited", "failed", "stopped")


@dataclass
class Record:
    id: str
    generation: int
    workload: str
    ordinal: int
    kind: str
    runtime: str
    spec: dict
    gpus: list = field(default_factory=list)
    state: str = "pending"
    reason: Optional[str] = None
    exit_code: Optional[int] = None
    attempt: int = 0          # tasks: attempts started (1 = the first run)
    restarts: int = 0         # services: restarts after an exit
    final: bool = False       # will not be started again for this generation
    pid: Optional[int] = None
    pgid: Optional[int] = None
    proc_start: Optional[int] = None   # /proc/<pid>/stat starttime, so a reused pid is not mistaken
    boot_id: Optional[str] = None
    container: Optional[str] = None
    ports: list = field(default_factory=list)  # [{name, port, host_port, address}]
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    next_start_at: Optional[float] = None
    stop_requested: bool = False
    deadline_hit: bool = False
    health_failures: int = 0
    healthy_seen: bool = False
    last_probe_at: Optional[float] = None
    # Model replicas (M3): what to fetch (never the token), the host paths once fetched, the
    # engine program for a process variant, download progress, and whether it is still loading
    # (a replica on a shared card starts only once the others there have loaded).
    model: Optional[dict] = None
    engine: Optional[dict] = None
    paths: Optional[dict] = None
    engine_path: Optional[str] = None
    download: Optional[dict] = None
    loading: bool = False
    # M4: the environment variable that carries the engine's internal key. Only the name is kept
    # here; the key itself stays in the agent's memory (Supervisor.keys) and comes with `desired`.
    key_env: Optional[str] = None
    # Behind the agent's key guard (guard.py): the server gave it an internal key. Its ports then
    # carry an `internal` loopback port for the engine; the guard listens on `host_port`.
    guarded: bool = False

    def public(self, *, with_tail: Optional[str] = None) -> dict:
        out = {"id": self.id, "generation": self.generation, "state": self.state, "reason": self.reason,
               "exit_code": self.exit_code, "attempt": self.attempt, "restarts": self.restarts, "final": self.final,
               "pid": self.pid, "container": self.container,
               "ports": [{k: p.get(k) for k in ("name", "port", "host_port", "address")} for p in self.ports],
               "started_at": self.started_at, "finished_at": self.finished_at}
        if self.download is not None:
            out["download"] = self.download
        if with_tail is not None:
            out["log_tail"] = with_tail
        return out

    @property
    def ended(self) -> bool:
        return self.state in ENDED


class RecordStore:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir) / "replicas"
        self.logs = Path(state_dir) / "logs"

    def ensure(self) -> None:
        for d in (self.dir, self.logs):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)

    def save(self, rec: Record) -> None:
        self.ensure()
        path = self.dir / f"{rec.id}.json"
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(rec), fh)
        os.replace(tmp, path)

    def delete(self, rec_id: str) -> None:
        try:
            (self.dir / f"{rec_id}.json").unlink()
        except FileNotFoundError:
            pass

    def load_all(self) -> dict[str, Record]:
        out: dict[str, Record] = {}
        if not self.dir.is_dir():
            return out
        known = {f.name for f in fields(Record)}
        for path in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                rec = Record(**{k: v for k, v in data.items() if k in known})
            except (OSError, ValueError, TypeError) as e:
                log.warning("Ignoring unreadable replica record %s: %s", path, e)
                continue
            out[rec.id] = rec
        return out

    def log_dir(self, rec_id: str) -> Path:
        return self.logs / rec_id
