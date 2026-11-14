"""The Playground's Vision tab (1.3): exposed vision workloads, tried from the console as the signed-in user.

A vision workload is an exposed service (forgeyard/workloads/expose.py) whose program is started with
`--task detect|segment|depth`, as every vision template's is (forgeyard/workloads/templates.py, the vision
server in images/vision). The console reaches it through its own routes, never through /x/ (which takes
API keys only):

    GET  /api/playground/vision/options            the exposed vision workloads: name, task, model, licence,
                                                   ready replicas (or why none), the largest request
    POST /api/playground/vision/<name>/<task>      the image (multipart, as the browser sends it) to the
                                                   workload's own /v1/<task>

Both need the console session; the POST also the CSRF rule of every console write (X-Forgeyard: 1 and our
own Origin). Any signed-in person may use them, as anyone may give their own API key every exposed
workload. Only the path `/v1/<task>` of the workload's declared task is reached, with POST and no query.

The request then goes the way an /x/ request does (forgeyard/gateway/passthrough.py): the same guarded
replicas, request and answer header rules (the console's cookie never reaches the workload), the
workload's body limit (413), per-replica `concurrency` (429 workload_busy) and `key_concurrency`, counted
per person here (429 key_busy), timeouts, and usage under the model `x:<name>`, as "console".
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

from forgeyard.db import Workload, new_session
from forgeyard.gateway import passthrough
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.keys import KeyCaller
from forgeyard.workloads import expose, templates

# The vision server's tasks, with the template that runs each (its title, model and licence).
TASKS = {t.task: t for t in templates.TEMPLATES}
_TASK_FLAG = re.compile(r"^--task=(.+)$")


def declared_task(spec: Optional[dict]) -> Optional[str]:
    """The vision task a workload's program is started with (`--task detect` or `--task=detect`), or None."""
    runtime = (spec or {}).get("runtime") or {}
    words: list = []
    if isinstance(runtime.get("command"), list):  # a container's entrypoint override
        words += runtime["command"]
    words += runtime.get("args") or []
    for i, word in enumerate(words):
        if not isinstance(word, str):
            continue
        if word == "--task":
            value = words[i + 1] if i + 1 < len(words) else None
        elif m := _TASK_FLAG.match(word):
            value = m.group(1)
        else:
            continue
        return value if value in TASKS else None
    return None


def vision_task(w: Workload) -> Optional[str]:
    """The task of an exposed vision workload, else None (not exposed, or no vision task). Only services can
    be exposed."""
    if expose.of(w) is None:
        return None
    return declared_task(w.spec)


def options(db: DbSession) -> dict:
    """The exposed vision workloads, for the Playground's Vision tab."""
    out = []
    for w in db.scalars(select(Workload).where(Workload.kind == "service").order_by(Workload.name)).all():
        task = vision_task(w)
        if task is None:
            continue
        t = TASKS[task]
        route = passthrough.load(db, w.name)
        ready = len(route.replicas) if route is not None else 0
        out.append({"name": w.name, "workload_id": w.id, "task": task, "title": t.title, "model": t.model,
                    "licence": t.licence, "endpoint": t.endpoint, "path": expose.url_path(w.name),
                    "ready": ready, "reason": None if ready else (route.reason if route else None),
                    "max_body_mb": expose.of(w).max_body_mb})
    return {"workloads": out, "tasks": [{"task": t.task, "title": t.title, "template": t.id, "model": t.model}
                                        for t in templates.TEMPLATES]}


def _load(name: str, task: str) -> Optional[passthrough.XRoute]:
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == name))
        if w is None or vision_task(w) != task:
            return None
        return passthrough.load(db, name)
    finally:
        db.close()


async def handle(request: Request, name: str, task: str, caller: KeyCaller) -> Response:
    """One Playground request to an exposed vision workload, as the signed-in `caller`."""
    started = time.monotonic()
    request_id = uuid.uuid4().hex
    ids = {passthrough.REQUEST_ID: request_id}
    try:
        if task not in TASKS:
            raise GatewayError(404, "unknown_task", f"No vision task {task!r}: the tasks are "
                                                    f"{', '.join(sorted(TASKS))}.")
        route = await run_in_threadpool(_load, name, task)
        if route is None:
            raise GatewayError(404, "workload_not_found", f"No exposed vision workload {name!r} does {task}. "
                                                          "Start one from a template on the Workloads page.")
        return await passthrough.forward(request, caller, route, f"/v1/{task}".encode("ascii"), b"", started,
                                         request_id)
    except GatewayError as e:
        return passthrough.forced_error(e, ids)
