"""Agents pin the server's CA: the fingerprint in the join command, wss://, a machine in the middle,
and `agent trust` for agents that joined over plain HTTP (FY-TRUST-*)."""

import asyncio
import json
import logging
import shutil
import time

import pytest

from forgeyard.agent import state, trust
from forgeyard.agent.backoff import Backoff
from forgeyard.agent.client import EXIT_JOIN_FAILED, Agent, JoinError, join
from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory
from forgeyard.cli import main as cli_main
from forgeyard.config import get_settings
from forgeyard.db import reset_engine
from forgeyard.security import crypto, tls
from tests.conftest import OWNER, SETUP_TOKEN
from tests.test_agent import LiveServer, _http
from tests.tls_helpers import LiveTLS, ca_of, free_port, https


def test_fy_trust_001_fingerprint_formats():
    """FY-TRUST-001: the join command's form, a certificate dialog's colon form and bare hex are
    the same pin; anything else is refused."""
    h = "ab" * 32
    want = "sha256:" + h
    assert trust.normalise_fingerprint(want) == want
    assert trust.normalise_fingerprint(":".join(h[i:i + 2].upper() for i in range(0, 64, 2))) == want
    assert trust.normalise_fingerprint(h) == want
    for bad in ("sha256:abc", "sha1:" + h, "", "zz" * 32):
        with pytest.raises(trust.TrustError):
            trust.normalise_fingerprint(bad)


def _owner_and_token(port: int, ca: str) -> tuple[str, str]:
    _, headers = https(port, "POST", "/api/auth/setup", ca,
                       {"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
    cookie = headers.get("set-cookie", "").split(";")[0]
    issued = https(port, "POST", "/api/machines/join-tokens", ca, {}, cookie)[0]
    return cookie, issued


async def _wait(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await asyncio.to_thread(predicate):
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.fixture()
def tls_server(monkeypatch):
    monkeypatch.setenv(SIMULATION_ENV, "1")
    monkeypatch.setenv("AGENT_HEARTBEAT_SECONDS", "1")
    port = free_port()
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", str(port))
    get_settings.cache_clear()
    return port


def test_fy_trust_002_join_with_the_fingerprint_then_wss_with_the_pinned_ca_only(tls_server, tmp_path, monkeypatch):
    """FY-TRUST-002: the join command (CLI) fetches the CA, checks its fingerprint, joins over
    https verified against that CA only, and stores it; the agent then connects over wss:// with
    it and the machine comes online. A wrong fingerprint is refused before anything is sent."""
    port = tls_server
    with LiveTLS(port) as srv:
        ca = ca_of(get_settings().data_dir)
        cookie, issued = _owner_and_token(port, ca)
        fp = issued["ca_fingerprint"]
        assert fp == tls.fingerprint(ca) and issued["command"].endswith(f"--ca-fingerprint {fp}")

        state_dir = tmp_path / "agent"
        wrong = "sha256:" + "00" * 32
        assert cli_main(["agent", "--server", srv.url, "--join-token", issued["token"], "--ca-fingerprint", wrong,
                         "--state-dir", str(state_dir), "--no-workloads",
                         "--simulate-inventory", "nvidia-a100x8"]) == EXIT_JOIN_FAILED
        assert state.load(state_dir) is None  # the token is not spent either: it was never sent

        seen = {}

        async def fake_run(self):
            seen.update(server=self.server, ctx=self.ssl_context)
            return 0

        monkeypatch.setattr(Agent, "run", fake_run)
        assert cli_main(["agent", "--server", srv.url, "--join-token", issued["token"], "--ca-fingerprint", fp,
                         "--state-dir", str(state_dir), "--no-workloads", "--simulate-inventory", "nvidia-a100x8"]) == 0
        saved = state.load(state_dir)
        assert saved["server"] == srv.url and saved["ca_fingerprint"] == fp
        assert trust.load_ca(state_dir) == tls.first_pem(ca)
        # Later starts read the pinned CA: the system store is not used.
        assert seen["server"] == srv.url and seen["ctx"].get_ca_certs()[0]["subject"] == trust.context(ca).get_ca_certs()[0]["subject"]
        assert len(seen["ctx"].get_ca_certs()) == 1
        monkeypatch.undo()
        monkeypatch.setenv(SIMULATION_ENV, "1")

        def status():
            return https(port, "GET", "/api/machines", ca, cookie=cookie)[0]["machines"][0]["status"]

        async def scenario():
            agent = Agent(srv.url, saved["credential"], SimulatedInventory("nvidia-a100x8"),
                          backoff=Backoff(base=0.05, cap=0.2), ca_pem=trust.load_ca(state_dir))
            run = asyncio.create_task(agent.run())
            assert await _wait(lambda: status() == "online")
            agent.stop()
            assert await asyncio.wait_for(run, 5) == 0
            return agent

        agent = asyncio.run(scenario())
        assert agent.sessions == 1


def test_fy_trust_003_a_machine_in_the_middle_with_another_ca_is_refused(tls_server, tmp_path, monkeypatch, caplog):
    """FY-TRUST-003: the pinned CA decides, not the network. A server with another CA is refused
    whether it offers its own CA (fingerprint mismatch) or the real CA's certificate next to a
    certificate the real CA never signed (verification fails); the join token is never sent."""
    port = tls_server
    real_dir = get_settings().data_dir
    with LiveTLS(port):
        real_ca = ca_of(real_dir)
        _, issued = _owner_and_token(port, real_ca)
    real_fp = tls.fingerprint(real_ca)

    # The impostor: another data dir, so another CA, on the same address. (Same database and secret
    # key: with PostgreSQL the database is not in the data dir.)
    monkeypatch.setenv("SECRET_KEY_FILE", str(real_dir / "secret.key"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "mitm"))
    get_settings.cache_clear()
    reset_engine()
    crypto.reset()
    with LiveTLS(port) as mitm:
        with pytest.raises(trust.TrustError, match="Refused: the CA .* has fingerprint"):
            trust.fetch_pinned_ca(mitm.url, real_fp)
        with pytest.raises(trust.TrustError, match="is not the server this agent trusts"):
            trust.verify(mitm.url, real_ca)
        with pytest.raises(JoinError, match="is not the server this agent trusts"):
            join(mitm.url, issued["token"], ssl_context=trust.context(real_ca))

        async def connect_once():
            agent = Agent(mitm.url, "fym_x", SimulatedInventory("nvidia-a100x8"), backoff=Backoff(base=0.05, cap=0.1),
                          ca_pem=real_ca)
            run = asyncio.create_task(agent.run())
            await asyncio.sleep(0.6)
            agent.stop()
            await asyncio.wait_for(run, 5)
            return agent.sessions

        caplog.set_level(logging.ERROR, logger="forgeyard.agent")
        assert asyncio.run(connect_once()) == 0
        assert f"{mitm.url} is not the server this agent trusts" in caplog.text  # said plainly, over wss too

    # An impostor that serves the real CA (it is public) at /api/tls/ca.pem, with its own certificate.
    shutil.copy(real_dir / "tls" / "ca.crt", tmp_path / "real-ca.crt")
    monkeypatch.setenv("TLS_CERT_FILE", str(tmp_path / "mitm" / "tls" / "server.crt"))
    monkeypatch.setenv("TLS_KEY_FILE", str(tmp_path / "mitm" / "tls" / "server.key"))
    monkeypatch.setenv("TLS_CA_FILE", str(tmp_path / "real-ca.crt"))
    get_settings.cache_clear()
    with LiveTLS(port) as mitm:
        with pytest.raises(trust.TrustError, match="is not the server this agent trusts"):
            trust.fetch_pinned_ca(mitm.url, real_fp)  # the fingerprint matches; the chain does not


def test_fy_trust_004_an_http_agent_upgrades_with_trust_and_keeps_its_credential(tls_server, tmp_path, monkeypatch, caplog):
    """FY-TRUST-004: an agent joined over plain HTTP (1.0). The server restarts with HTTPS on the
    same port: the old agent cannot connect and says what to run. `agent trust` with the
    fingerprint switches it to https and pins the CA, keeping the credential; it comes back
    online without a new join token. A wrong fingerprint changes nothing."""
    port = tls_server
    plain_url = f"http://127.0.0.1:{port}"
    with LiveServer(port):  # what 1.0 ran: plain HTTP
        _, cookie = _http(port, "POST", "/api/auth/setup",
                          {"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
        token = _http(port, "POST", "/api/machines/join-tokens", {}, cookie)[0]["token"]
        joined = join(plain_url, token, name="old-agent")
    state_dir = tmp_path / "agent"
    state.save(state_dir, {"server": plain_url, "node_id": joined["node_id"], "name": joined["name"],
                           "credential": joined["credential"]})

    with LiveTLS(port) as srv:
        ca = ca_of(get_settings().data_dir)
        fp = tls.fingerprint(ca)
        caplog.set_level(logging.WARNING, logger="forgeyard.agent")

        async def old_agent():
            agent = Agent(plain_url, joined["credential"], SimulatedInventory("nvidia-a100x8"),
                          backoff=Backoff(base=0.05, cap=0.1))
            run = asyncio.create_task(agent.run())
            await asyncio.sleep(0.8)
            agent.stop()
            await asyncio.wait_for(run, 5)
            return agent.sessions

        assert asyncio.run(old_agent()) == 0
        assert "forgeyard agent trust --server https://127.0.0.1:" in caplog.text

        before = json.loads((state_dir / "credential.json").read_text())
        assert cli_main(["agent", "trust", "--server", srv.url, "--ca-fingerprint", "sha256:" + "11" * 32,
                         "--state-dir", str(state_dir)]) == EXIT_JOIN_FAILED
        assert json.loads((state_dir / "credential.json").read_text()) == before
        assert cli_main(["agent", "trust", "--server", srv.url, "--ca-fingerprint", fp,
                         "--state-dir", str(state_dir)]) == 0
        after = state.load(state_dir)
        assert after["credential"] == before["credential"] and after["node_id"] == before["node_id"]
        assert after["server"] == srv.url and after["ca_fingerprint"] == fp and trust.load_ca(state_dir)

        cookie = https(port, "POST", "/api/auth/login", ca, {"username": OWNER[0], "password": OWNER[1]})[1] \
            .get("set-cookie").split(";")[0]

        def status():
            return https(port, "GET", "/api/machines", ca, cookie=cookie)[0]["machines"][0]["status"]

        async def upgraded():
            agent = Agent(after["server"], after["credential"], SimulatedInventory("nvidia-a100x8"),
                          backoff=Backoff(base=0.05, cap=0.2), ca_pem=trust.load_ca(state_dir))
            run = asyncio.create_task(agent.run())
            assert await _wait(lambda: status() == "online")
            agent.stop()
            await asyncio.wait_for(run, 5)

        asyncio.run(upgraded())
    assert cli_main(["agent", "trust", "--server", "https://x", "--state-dir", str(tmp_path / "never")]) == 2


def test_fy_trust_005_without_a_pin_https_means_the_system_store(tls_server):
    """FY-TRUST-005: an https server without a fingerprint is checked against the system trust
    store, which does not know Forgeyard's CA: refused, never accepted unverified."""
    with LiveTLS(tls_server) as srv:
        with pytest.raises(trust.TrustError, match="is not the server this agent trusts"):
            trust.resolve(srv.url, fingerprint=None, ca_file=None)
        with pytest.raises(JoinError, match="is not the server this agent trusts"):
            join(srv.url, "fyj_x")
        assert trust.resolve(srv.url, fingerprint=None, ca_file=str(get_settings().tls_dir / "ca.crt")) == \
            tls.first_pem(ca_of(get_settings().data_dir))
        with pytest.raises(trust.TrustError, match="needs an https:// server address"):
            trust.resolve(srv.url.replace("https", "http"), fingerprint="sha256:" + "ab" * 32, ca_file=None)
