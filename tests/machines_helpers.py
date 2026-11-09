"""Shared steps for the machine tests: join tokens, joining, a hello message."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

SIM = Path(__file__).resolve().parent.parent / "forgeyard" / "agent" / "fixtures"


def new_token(admin) -> dict:
    r = admin.post("/api/machines/join-tokens")
    assert r.status_code == 201, r.text
    return r.json()


def agent_client(app_client, host: str = "localhost") -> TestClient:
    """A client like the agent: no cookies, no X-Forgeyard header."""
    return TestClient(app_client.app, base_url=f"http://{host}")


def join(app_client, token: str, name: str = "gpu-box", **extra):
    body = {"join_token": token, "protocol": 1, "name": name, "hostname": name, "agent_version": "0.1.0", **extra}
    return agent_client(app_client).post("/agent/v1/join", json=body)


def joined(admin, name: str = "gpu-box") -> dict:
    r = join(admin, new_token(admin)["token"], name)
    assert r.status_code == 200, r.text
    return r.json()


def hello(fixture: str = "nvidia-a100x8", **over) -> dict:
    data = json.loads((SIM / f"{fixture}.json").read_text())
    msg = {"v": 1, "type": "hello", "agent_version": "0.1.0", "hostname": "box", "labels": {"rack": "a"},
           "platform": {"os": "Linux", "arch": "x86_64"}, "simulated": True,
           "inventory": {"accelerators": data["accelerators"], "collectors": data["collectors"]}}
    msg.update(over)
    return msg


def bearer(credential: str) -> dict:
    return {"Authorization": f"Bearer {credential}"}


def connect(app_client, credential: str, **kw):
    headers = {**bearer(credential), **kw.pop("headers", {})}
    host = kw.pop("host", "localhost")
    # An absolute URL: TestClient would otherwise send Host: testserver for WebSockets.
    return agent_client(app_client, host).websocket_connect(f"ws://{host}/agent/v1/stream", headers=headers)


def machine(admin, machine_id: int) -> dict:
    return next(m for m in admin.get("/api/machines").json()["machines"] if m["id"] == machine_id)


def expect_close(ws, timeout: float = 5.0) -> int:
    """Wait for the server to close the socket; return the close code. Fails (never hangs) if the
    server keeps it open: TestClient's receive has no timeout of its own."""
    import concurrent.futures

    from starlette.websockets import WebSocketDisconnect

    def wait():
        while True:
            try:
                ws.receive_json()
            except WebSocketDisconnect as e:
                return e.code

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(wait).result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise AssertionError(f"the server did not close the socket within {timeout} s") from None
    finally:
        pool.shutdown(wait=False)
