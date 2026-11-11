"""Shared pieces for the workload tests: specs, machines in the database, a fake docker CLI,
and a way to drive the agent's supervisor round by round."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import time
from pathlib import Path
from typing import Optional

from forgeyard.db import new_session
from forgeyard.machines import store
from forgeyard.machines.messages import InventoryIn
from tests.leaks import cleanup as cleanup_world, world_dir  # noqa: F401 (the tests import them from here)
from tests.machines_helpers import SIM

PROCESS_OK = {"process": {"ok": True, "user": "nobody", "uid": 65534, "limits": "rlimit"},
              "container": {"ok": True, "engine": "docker", "nvidia": True, "gpu_mode": "gpus"}}


def fixture(name: str) -> dict:
    return json.loads((SIM / f"{name}.json").read_text())


def gpus_of(name: str) -> list[dict]:
    return [a for a in fixture(name)["accelerators"] if a["vendor"] != "cpu"]


def make_node(name: str = "box", inv: str = "nvidia-a100x8", *, status: str = "online", allowlist=None,
              runtime_info: Optional[dict] = None, features=("workloads",)) -> int:
    """A machine straight in the database, as if its agent had said hello."""
    db = new_session()
    try:
        _, node = store.create_node(db, name=name, hostname=name, agent_version="0.1.0", protocol=1, ip="10.0.0.9")
        data = fixture(inv)
        store.apply_inventory(db, node, InventoryIn.model_validate({"accelerators": data["accelerators"],
                                                                    "collectors": data["collectors"]}))
        node.status = status
        node.features = list(features)
        node.runtime_info = PROCESS_OK if runtime_info is None else runtime_info
        node.runtimes = [k for k, v in node.runtime_info.items() if v.get("ok")]
        if allowlist is not None:
            keys = {a.index: a.key for a in node.accelerators if a.vendor != "cpu"}
            node.gpu_allowlist = [keys[i] for i in allowlist]
        db.commit()
        return node.id
    finally:
        db.close()


def service(name: str = "web", **over) -> dict:
    spec = {"name": name, "kind": "service",
            "runtime": {"type": "process", "command": "python3", "args": ["-m", "http.server", "${PORT}"]},
            "ports": [{"name": "http"}]}
    spec.update(over)
    return spec


def task(name: str = "job", script: str = "echo hi", **over) -> dict:
    spec = {"name": name, "kind": "task", "runtime": {"type": "process", "command": "sh", "args": ["-c", script]}}
    spec.update(over)
    return spec


def container(name: str = "box", image: str = "python:3.13-slim", **over) -> dict:
    spec = {"name": name, "kind": "service", "runtime": {"type": "container", "image": image,
                                                         "args": ["python", "-m", "http.server", "8000"]},
            "ports": [{"name": "http", "port": 8000}]}
    spec.update(over)
    return spec


# ------------------------------------------------------------------ the agent side
def replica(rid: str, spec: dict, *, generation: int = 1, gpus: Optional[list] = None, ordinal: int = 0) -> dict:
    """One entry of a desired message, normalised the way the server stores specs."""
    from forgeyard.workloads.spec import WorkloadSpec

    data = WorkloadSpec.model_validate(spec).model_dump(mode="json")
    return {"id": rid, "workload_id": 1, "workload": data["name"], "ordinal": ordinal, "generation": generation,
            "kind": data["kind"], "spec": data, "gpus": gpus or []}


def desired(*replicas: dict, revision: int = 0) -> dict:
    return {"v": 1, "type": "desired", "revision": revision, "replicas": list(replicas)}


async def converge(sup, until=None, timeout: float = 10.0, step: float = 0.05):
    """Run reconcile rounds (and wait for their operations) until `until(sup)` holds."""
    deadline = time.monotonic() + timeout
    while True:
        await sup.reconcile_once()
        await sup.settle()
        if until is None or until(sup):
            return
        if time.monotonic() > deadline:
            states = {r.id: (r.state, r.reason) for r in sup.records.values()}
            raise AssertionError(f"did not converge within {timeout} s: {states}")
        await asyncio.sleep(step)


def alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        st = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()
    except OSError:
        return False
    return st[0] != "Z"


# ------------------------------------------------------------------ a fake docker CLI
FAKE_DOCKER = r'''#!PYTHON
"""A stand-in for the docker CLI: records every argv, keeps containers in state.json.
Calls can run at the same time (the agent starts replicas concurrently): one lock around it all,
as the real daemon serialises them."""
import fcntl, json, sys, uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
CALLS = HERE / "calls.jsonl"
INFO = HERE / "info.json"
_lock = open(HERE / ".lock", "w")
fcntl.flock(_lock, fcntl.LOCK_EX)
args = sys.argv[1:]
with CALLS.open("a") as fh:
    fh.write(json.dumps(args) + "\n")
state = json.loads(STATE.read_text()) if STATE.exists() else {"containers": {}}
cs = state["containers"]

def save():
    STATE.write_text(json.dumps(state, indent=1))

def find(ref):
    for cid, c in cs.items():
        if ref in (cid, c["name"]) or (len(ref) >= 6 and cid.startswith(ref)):
            return cid, c
    return None, None

cmd = args[0] if args else ""
if cmd == "info":
    print(INFO.read_text() if INFO.exists() else json.dumps({"ServerVersion": "27.0.0-fake",
          "Runtimes": {"runc": {}, "nvidia": {}}, "DefaultRuntime": "runc"}))
elif cmd == "run":
    NOVALUE = {"-d", "--rm", "--init", "-i", "-t"}
    i, opts, labels = 1, {}, {}
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a in NOVALUE:
            i += 1
            continue
        if "=" in a and a.startswith("--"):
            k, v = a.split("=", 1)
            i += 1
        else:
            k, v = a, args[i + 1]
            i += 2
        opts.setdefault(k, []).append(v)
        if k == "--label":
            lk, lv = v.split("=", 1)
            labels[lk] = lv
    image, rest = args[i], args[i + 1:]
    if image.startswith("fail/"):
        print("Error: pull access denied for " + image, file=sys.stderr)
        sys.exit(125)
    if image.startswith("credfail/") and image not in state.get("pulled", []):
        # not local, and the configured credential helper is broken (as under the agent)
        print(f"Unable to find image '{image}' locally", file=sys.stderr)
        print("docker: error getting credentials - err: exit status 255, out: ``", file=sys.stderr)
        sys.exit(125)
    env = {}
    for f in opts.get("--env-file", []):
        for line in Path(f).read_text().splitlines():
            k, _, v = line.partition("=")
            env[k] = v
    name = opts["--name"][0]
    if find(name)[0]:
        print(f"Error: the container name {name} is already in use", file=sys.stderr)
        sys.exit(125)
    cid = uuid.uuid4().hex
    cs[cid] = {"name": name, "labels": labels, "status": "running", "exit": 0, "pid": 4242, "image": image,
               "args": rest, "opts": opts, "env": env, "logs": [f"{name} line {n}" for n in range(1, 6)]}
    save()
    print(cid)
elif cmd == "pull":
    import os
    conf = os.environ.get("DOCKER_CONFIG")
    cfg = json.loads((Path(conf) / "config.json").read_text()) if conf else {"credsStore": "broken"}
    if "credsStore" in cfg or "credHelpers" in cfg:
        print("Error response from daemon: error getting credentials - err: exit status 255, out: ``",
              file=sys.stderr)
        sys.exit(1)
    state.setdefault("pulled", []).append(args[-1])
    save()
    print("Status: Downloaded newer image for " + args[-1])
elif cmd == "inspect":
    if args[1] == "--format":
        cid, c = find(args[3])
        if not c:
            print("Error: No such object: " + args[3], file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"Status": c["status"], "ExitCode": c["exit"], "Pid": c["pid"] if c["status"] == "running" else 0}))
    else:
        out = []
        for ref in args[1:]:
            cid, c = find(ref)
            if c:
                out.append({"Id": cid, "Name": "/" + c["name"], "Config": {"Labels": c["labels"]}})
        print(json.dumps(out))
elif cmd == "ps":
    flt = args[args.index("--filter") + 1].removeprefix("label=")
    k, v = flt.split("=", 1)
    print("\n".join(cid[:12] for cid, c in cs.items() if c["labels"].get(k) == v))
elif cmd in ("stop", "kill"):
    cid, c = find(args[-1])
    if c:
        c["status"], c["exit"] = "exited", 143 if cmd == "stop" else 137
        save()
elif cmd == "rm":
    cid, c = find(args[-1])
    if c:
        del cs[cid]
        save()
elif cmd == "logs":
    cid, c = find(args[-1])
    n = int(args[args.index("--tail") + 1])
    print("\n".join(c["logs"][-n:]) if c else "")
else:
    print("fake docker: unsupported " + " ".join(args), file=sys.stderr)
    sys.exit(2)
'''


class FakeDocker:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "docker"
        self.path.write_text(FAKE_DOCKER.replace("#!PYTHON", "#!" + sys.executable))
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def calls(self) -> list[list[str]]:
        f = self.root / "calls.jsonl"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []

    def runs(self) -> list[list[str]]:
        return [c for c in self.calls() if c and c[0] == "run"]

    def state(self) -> dict:
        f = self.root / "state.json"
        return json.loads(f.read_text())["containers"] if f.exists() else {}

    def set(self, name: str, **fields) -> None:
        import fcntl

        f = self.root / "state.json"
        with open(self.root / ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = json.loads(f.read_text())
            for c in data["containers"].values():
                if c["name"] == name:
                    c.update(fields)
            f.write_text(json.dumps(data))

    def info(self, data: dict) -> None:
        (self.root / "info.json").write_text(json.dumps(data))
