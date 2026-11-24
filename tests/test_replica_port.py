"""scripts/replica_port.py: the acceptance scripts' direct calls to a replica's own port, made the way the gateway
reaches it (1.4.5, #163): TLS with Forgeyard's CA and the machine's binding name for an agent that serves TLS, plain
HTTP for an older one, and the checks a TLS port must pass (FY-RPORT-*). The ports here are real key guards
(forgeyard/agent/workloads/guard.py) with certificates the server signed."""

from __future__ import annotations

import importlib.util
import json
import re
import socket
import ssl
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeyard.config import get_settings
from forgeyard.security import replica_tls, tls
from tests.gateway_helpers import free_port
from tests.guard_helpers import KEY, GuardThread, OpenEngine
from tests.test_replica_tls import certified, fake_node

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def rp():
    mod = _load("replica_port")
    yield mod
    sys.modules.pop("replica_port", None)


@pytest.fixture()
def engine():
    e = OpenEngine()
    yield e
    e.close()


def machine(name: str, node=None, *, state="ok") -> dict:
    """A machine as /api/machines lists it: a TLS one with its binding name, or a plain (older) one."""
    if node is None:
        return {"name": name, "features": ["workloads"], "replica_tls": {"state": "plain"}, "runtime_info": {}}
    return {"name": name, "features": ["workloads", "guard_tls"], "replica_tls": {"state": state},
            "runtime_info": {"tls": {"name": replica_tls.binding_name(node)}}}


def ca_pem() -> str:
    return (get_settings().tls_dir / "ca.crt").read_text()


class TLSGuards:
    """Two machines a and b with TLS key guards (certificates from the server's CA) in front of one engine."""

    def __init__(self, tmp_path, engine):
        self.a, self.b = fake_node(1, "a" * 64), fake_node(2, "b" * 64)
        self.ga = GuardThread(engine.port, ssl_context=certified(self.a, tmp_path).context)
        self.gb = GuardThread(engine.port, ssl_context=certified(self.b, tmp_path).context)
        self.machines = [machine("a", self.a), machine("b", self.b), machine("old")]

    def close(self):
        self.ga.close()
        self.gb.close()


@pytest.fixture()
def guards(tmp_path, engine):
    g = TLSGuards(tmp_path, engine)
    yield g
    g.close()


def endpoint(port: int, machine_name: str, scheme: str = "https") -> dict:
    return {"replica": 0, "machine": machine_name, "url": f"{scheme}://127.0.0.1:{port}/", "encrypted": scheme == "https"}


# ------------------------------------------------------------------ TLS ports
def test_fy_rport_001_a_tls_port_is_called_over_tls_with_the_binding_name(rp, guards, engine):
    """FY-RPORT-001: an encrypted endpoint is reached over TLS to its address, the machine's binding name (from
    /api/machines runtime_info.tls.name) as SNI and host name, against Forgeyard's CA only: the guard answers 401
    replica_key_required without the internal key and with a client's key, and passes the internal key on (the
    request reaches the engine); the port and its URL are what the endpoint said."""
    r = rp.Replicas(guards.machines, rp.context_of([ca_pem()]), "test")
    p = r.endpoint(endpoint(guards.ga.port, "a"))
    assert (p.host, p.port, p.encrypted, p.tls_name) == ("127.0.0.1", guards.ga.port, True,
                                                         replica_tls.binding_name(guards.a))
    assert p.url == f"https://127.0.0.1:{guards.ga.port}"
    a = r.request(p, "POST", "/v1/chat/completions", b"{}", {"Content-Type": "application/json"})
    assert a.status == 401 and "replica_key_required" in a.text and a.failure == ""
    a = r.request(p, "GET", "/v1/models", headers={"Authorization": "Bearer fyk_client"})
    assert a.status == 401
    assert engine.seen == []
    a = r.request(p, "GET", "/v1/models", headers={"Authorization": f"Bearer {KEY}"})
    assert a.status == 200 and json.loads(a.body)["auth"] == f"Bearer {KEY}"
    assert r.post(p, "/v1/x", b"{}", "application/json")[0] == 401


def test_fy_rport_002_another_machines_name_or_an_address_is_refused(rp, guards, engine):
    """FY-RPORT-002: the certificate is checked against the machine the endpoint names: machine a's port called
    with machine b's binding name fails the TLS check (failure "tls", hostname mismatch), and so does a port whose
    machine has no binding name; neither reaches the engine nor counts as unreachable."""
    r = rp.Replicas(guards.machines, rp.context_of([ca_pem()]))
    wrong = r.endpoint(endpoint(guards.ga.port, "b"))
    a = r.request(wrong, "GET", "/v1/models", headers={"Authorization": f"Bearer {KEY}"})
    assert a.status == 0 and a.failure == "tls" and "mismatch" in a.error.lower()
    nameless = rp.Replicas([{**machine("a", guards.a), "runtime_info": {}}], r.ctx)
    p = nameless.endpoint(endpoint(guards.ga.port, "a"))
    assert p.encrypted and p.tls_name is None
    a = nameless.request(p, "GET", "/v1/models", headers={"Authorization": f"Bearer {KEY}"})
    assert a.status == 0 and a.failure == "tls" and "binding name" in a.error
    assert engine.seen == []


def test_fy_rport_003_another_ca_is_refused(rp, guards, tmp_path, engine, monkeypatch):
    """FY-RPORT-003: a context with another CA than the one that signed the guard's certificate refuses it (failure
    "tls", certificate verify failed); the system's trust store is never used."""
    other = tmp_path / "other-ca"
    tls.make_ca(other)
    r = rp.Replicas(guards.machines, rp.context_of([(other / "ca.crt").read_text()]))
    a = r.request(r.endpoint(endpoint(guards.ga.port, "a")), "GET", "/v1/models",
                  headers={"Authorization": f"Bearer {KEY}"})
    assert a.status == 0 and a.failure == "tls" and "SSLCertVerificationError" in a.error
    assert engine.seen == []
    assert rp.context_of([]) is None
    loaded = []
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", lambda self, *a: loaded.append("default"))
    monkeypatch.setattr(ssl.SSLContext, "set_default_verify_paths", lambda self: loaded.append("paths"))
    ctx = rp.context_of([ca_pem()])
    assert loaded == []
    assert len(ctx.get_ca_certs()) == 1 and ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED


def test_fy_rport_004_transport_checks_on_a_tls_port_all_pass(rp, guards):
    """FY-RPORT-004: on a TLS port the four transport checks pass: plain HTTP refused (no HTTP answer), the
    certificate verifies with this machine's binding name (its only name, shown), the other TLS machine's binding
    name refused, an invented binding name refused."""
    r = rp.Replicas(guards.machines, rp.context_of([ca_pem()]), "--ca")
    p = r.endpoint(endpoint(guards.ga.port, "a"))
    out = r.transport_checks(p, "dep #0 on a")
    assert [o for _, o, _ in out] == [rp.PASS] * 4, out
    names = [n for n, _, _ in out]
    assert names == ["dep #0 on a: plain HTTP to the TLS port is refused",
                     "dep #0 on a: the certificate verifies against Forgeyard's CA with a's binding name",
                     "dep #0 on a: another machine's binding name is refused",
                     "dep #0 on a: a binding name no machine has is refused"]
    assert f"names {replica_tls.binding_name(guards.a)}," in out[1][2] and "(CA from --ca)" in out[1][2]
    assert replica_tls.binding_name(guards.b) in out[2][2] and "mismatch" in out[2][2].lower()
    assert r.other_name(p) == replica_tls.binding_name(guards.b)


def test_fy_rport_005_plain_http_to_a_tls_port_is_refused_not_unreachable(rp, guards):
    """FY-RPORT-005: plain HTTP to a TLS guard gets no HTTP answer: plain_refused says so, and a plain request to
    it (an endpoint still listed as http://) fails as "closed", never "unreachable" (the scripts skip only an
    unreachable port as another network's)."""
    r = rp.Replicas(guards.machines, rp.context_of([ca_pem()]))
    ok, how = r.plain_refused(r.endpoint(endpoint(guards.ga.port, "a")))
    assert ok is True and "answer" in how
    stale = r.endpoint(endpoint(guards.ga.port, "a", "http"))
    assert not stale.encrypted
    a = r.request(stale, "GET", "/v1/models")
    assert a.status == 0 and a.failure == "closed"


def test_fy_rport_006_wrong_certificates_fail_the_checks(rp, guards, engine, tmp_path):
    """FY-RPORT-006: the transport checks FAIL when the port does not do what a TLS port must: a port that
    answers plain HTTP (an engine behind no TLS), a certificate that names another machine (b's port listed as a's),
    and a certificate the CA did not sign."""
    r = rp.Replicas(guards.machines, rp.context_of([ca_pem()]))
    plain_port = rp.Port("127.0.0.1", engine.port, "a", True, replica_tls.binding_name(guards.a))
    ok, how = r.plain_refused(plain_port)
    assert ok is False and how.startswith("answered: HTTP/")
    swapped = rp.Port("127.0.0.1", guards.gb.port, "a", True, replica_tls.binding_name(guards.a))
    out = dict((n.split(": ", 1)[1], (o, d)) for n, o, d in r.transport_checks(swapped, "x"))
    assert out["the certificate verifies against Forgeyard's CA with a's binding name"][0] == rp.FAIL
    other = tmp_path / "other-ca"
    tls.make_ca(other)
    foreign = rp.Replicas(guards.machines, rp.context_of([(other / "ca.crt").read_text()]))
    out = foreign.transport_checks(foreign.endpoint(endpoint(guards.ga.port, "a")), "x")
    assert [o for _, o, _ in out] == [rp.PASS, rp.FAIL], out  # stops after the certificate fails


def test_fy_rport_007_without_a_ca_the_calls_run_and_the_certificate_checks_skip(rp, guards, monkeypatch):
    """FY-RPORT-007: with no CA to verify with (no --ca, no FORGEYARD_REPLICA_CA, the server serves none) the
    direct calls still run over TLS (401 from the guard), and the certificate check is SKIPPED with how to give the
    CA; with only one TLS machine the other-machine check is skipped, the invented name still checked."""
    monkeypatch.delenv("FORGEYARD_REPLICA_CA", raising=False)
    ctx, where = rp.find_ca("http://127.0.0.1:1", None)
    assert ctx is None and where == ""
    r = rp.Replicas(guards.machines, None)
    p = r.endpoint(endpoint(guards.ga.port, "a"))
    assert r.request(p, "GET", "/v1/models").status == 401
    out = r.transport_checks(p, "x")
    assert [o for _, o, _ in out] == [rp.PASS, rp.SKIP] and "--ca" in out[1][2]
    single = rp.Replicas([machine("a", guards.a), machine("old")], rp.context_of([ca_pem()]))
    out = single.transport_checks(single.endpoint(endpoint(guards.ga.port, "a")), "x")
    assert [o for _, o, _ in out] == [rp.PASS, rp.PASS, rp.SKIP, rp.PASS], out
    assert out[2][2] == "only one machine serves TLS"


def test_fy_rport_008_where_the_ca_comes_from(rp, tmp_path, monkeypatch, guards):
    """FY-RPORT-008: Forgeyard's CA comes from the scripts' --ca context (its CA certificates only), then
    FORGEYARD_REPLICA_CA (for a server with its own certificate or TLS=off), both when given; the context trusts
    nothing else."""
    monkeypatch.delenv("FORGEYARD_REPLICA_CA", raising=False)
    path = get_settings().tls_dir / "ca.crt"
    given = ssl.create_default_context(cafile=str(path))
    ctx, where = rp.find_ca("http://127.0.0.1:1", given)
    assert where == "--ca" and len(ctx.get_ca_certs()) == 1
    monkeypatch.setenv("FORGEYARD_REPLICA_CA", str(path))
    ctx, where = rp.find_ca("http://127.0.0.1:1", None)
    assert where == "FORGEYARD_REPLICA_CA"
    r = rp.Replicas(guards.machines, ctx)
    assert r.request(r.endpoint(endpoint(guards.ga.port, "a")), "GET", "/v1/models").status == 401
    other = tmp_path / "other-ca"
    tls.make_ca(other)
    ctx, where = rp.find_ca("http://127.0.0.1:1", ssl.create_default_context(cafile=str(other / "ca.crt")))
    assert where == "--ca + FORGEYARD_REPLICA_CA" and len(ctx.get_ca_certs()) == 2


def test_fy_rport_009_the_ca_the_server_serves(rp, monkeypatch):
    """FY-RPORT-009: without --ca or FORGEYARD_REPLICA_CA, an https:// server's /api/tls/ca.pem is used when it serves
    a PEM certificate (fetched over the verified connection to the server); a plain http:// server is never asked
    (nothing would vouch for what it sends), and an answer that is no certificate or a failed fetch gives no CA."""
    import io

    monkeypatch.delenv("FORGEYARD_REPLICA_CA", raising=False)
    tls.make_ca(get_settings().tls_dir)
    pem = (get_settings().tls_dir / "ca.crt").read_bytes()
    asked = []

    def urlopen(answer):
        def fn(req, timeout, context):
            asked.append(req.full_url)
            if isinstance(answer, Exception):
                raise answer
            return io.BytesIO(answer)
        return fn

    monkeypatch.setattr(rp.urllib.request, "urlopen", urlopen(pem))
    assert rp.find_ca("http://srv:7860", None) == (None, "") and asked == []
    ctx, where = rp.find_ca("https://srv:7860/", None)
    assert where == "/api/tls/ca.pem" and len(ctx.get_ca_certs()) == 1 and asked == ["https://srv:7860/api/tls/ca.pem"]
    monkeypatch.setattr(rp.urllib.request, "urlopen", urlopen(b'{"detail": {"code": "no_ca"}}'))
    assert rp.find_ca("https://srv:7860", None) == (None, "")
    monkeypatch.setattr(rp.urllib.request, "urlopen", urlopen(OSError("refused")))
    assert rp.find_ca("https://srv:7860", None) == (None, "")


# ------------------------------------------------------------------ plain ports (older agents)
def test_fy_rport_010_a_plain_port_stays_plain_http(rp, engine):
    """FY-RPORT-010: an older agent's port (endpoint http://, machine not serving TLS) is called with plain HTTP as
    before (the guard's 401), and its transport check is one information line saying its traffic is not encrypted;
    a workload's published port on such a machine is plain too."""
    g = GuardThread(engine.port)
    try:
        r = rp.Replicas([machine("old")], rp.context_of([ca_pem()]) if (get_settings().tls_dir / "ca.crt").exists()
                        else None)
        p = r.endpoint(endpoint(g.port, "old", "http"))
        assert (p.encrypted, p.tls_name, p.url) == (False, None, f"http://127.0.0.1:{g.port}")
        a = r.request(p, "POST", "/v1/chat/completions", b"{}", {"Content-Type": "application/json"})
        assert a.status == 401 and "replica_key_required" in a.text
        out = r.transport_checks(p, "dep #0 on old")
        assert out == [("dep #0 on old: traffic to old is plain HTTP", rp.INFO,
                        "not encrypted: its agent is older than 1.4.5 (no guard_tls); update it")]
        w = r.published("old", {"address": "127.0.0.1", "host_port": g.port})
        assert not w.encrypted and r.request(w, "GET", "/echo").status == 401
    finally:
        g.close()


def test_fy_rport_011_workload_ports_follow_their_machine(rp, guards):
    """FY-RPORT-011: a workload replica's published port (address, host_port) on a TLS machine is encrypted with that
    machine's binding name; machines are TLS by replica_tls state (not "plain"), or by guard_tls when the server
    does not say; a TLS machine without a recorded name maps to None."""
    r = rp.Replicas(guards.machines, rp.context_of([ca_pem()]))
    w = r.published("a", {"address": "127.0.0.1", "host_port": guards.ga.port})
    assert w.encrypted and w.tls_name == replica_tls.binding_name(guards.a)
    assert r.request(w, "GET", "/echo").status == 401
    names = rp.binding_names([machine("old"), {"name": "n", "features": ["guard_tls"], "runtime_info": {}},
                              {"name": "i", "features": [], "replica_tls": None},
                              {"name": "bad", "replica_tls": {"state": "ok"}, "runtime_info": {"tls": {"name": "x.example"}}}])
    assert names == {"n": None, "bad": None}


def test_fy_rport_012_unreachable_is_its_own_failure(rp):
    """FY-RPORT-012: a port nothing listens on is "unreachable" (the only failure the scripts may skip as another
    network's), and the plain-HTTP check is skipped there rather than passed."""
    r = rp.Replicas([], None)
    port = free_port()
    a = r.request(rp.Port("127.0.0.1", port, "x", False), "GET", "/")
    assert a.status == 0 and a.failure == "unreachable"
    out = r.transport_checks(rp.Port("127.0.0.1", port, "x", True, "m1-0" + rp.BINDING_SUFFIX), "x")
    assert out[0][1] == rp.SKIP and "not reachable" in out[0][2]


def test_fy_rport_013_record_gives_each_outcome_to_check(rp):
    """FY-RPORT-013: record() hands PASS/FAIL to check(name, ok, detail), SKIP as skip=True and INFO as info=True."""
    calls = []
    rp.record([("a", rp.PASS, "1"), ("b", rp.FAIL, "2"), ("c", rp.SKIP, "3"), ("d", rp.INFO, "4")],
              lambda name, ok, detail="", info=False, skip=False: calls.append((name, ok, detail, info, skip)))
    assert calls == [("a", True, "1", False, False), ("b", False, "2", False, False), ("c", True, "3", False, True),
                     ("d", True, "4", True, False)]


# ------------------------------------------------------------------ the scripts use it
def test_fy_rport_014_accept_all_guard_checks_over_tls(rp, guards, engine, monkeypatch):
    """FY-RPORT-014: accept_all's guard_checks on a deployment with a TLS replica runs the transport checks and the
    key guard's refusals over TLS (all pass); a replica still listed as http:// on a TLS port FAILS (closed without
    an answer) instead of being skipped as unreachable; only a port that cannot be reached is skipped."""
    acc = _load("accept_all")
    try:
        results = []
        listing = {"machines": guards.machines}
        run = SimpleNamespace(
            admin=SimpleNamespace(ok=lambda method, path, *a, **k: listing),
            args=SimpleNamespace(server="http://127.0.0.1:1"), tls=rp.context_of([ca_pem()]),
            check=lambda name, ok, detail="", info=False, skip=False: results.append(
                ("skip" if skip or info else "pass" if ok else "fail", name, detail)) or bool(ok),
            skip=lambda name, reason: results.append(("skip", name, reason)),
            deps={"chat": {"name": "dep", "recipe": "vllm", "route": {"api": "openai-chat", "upstream_model": "m"},
                           "endpoints": [endpoint(guards.ga.port, "a"), endpoint(guards.gb.port, "b", "http"),
                                         endpoint(free_port(), "a")]}})
        acc.guard_checks(run, "fyk_client")
        fails = [r for r in results if r[0] == "fail"]
        passes = [r[1] for r in results if r[0] == "pass"]
        assert "dep #0 on a: a direct call without the internal key gets 401 from the key guard" in passes
        assert "dep #0 on a: vLLM's open paths (/invocations, /rerank, /pooling, /classify) are guarded" in passes
        assert "dep #0 on a: the certificate verifies against Forgeyard's CA with a's binding name" in passes
        assert len([p for p in passes if p.startswith("dep #0 on a")]) == 7
        assert fails and all(f[1].startswith("dep #0 on b") for f in fails), fails
        assert any("RemoteDisconnected" in f[2] or "closed" in f[2].lower() or "Reset" in f[2] for f in fails), fails
        skipped = [r for r in results if r[0] == "skip"]
        assert [s[1] for s in skipped] == ["dep #0 on b: traffic to b is plain HTTP", "direct calls to some replica ports"]
        assert engine.seen == []
    finally:
        sys.modules.pop("accept_all", None)


def test_fy_rport_015_every_direct_port_check_uses_the_helper(rp):
    """FY-RPORT-015: no acceptance script builds a replica URL with http:// itself or calls a replica's endpoint URL
    with its own HTTP client any more: every direct-port check goes through replica_port (accept_all,
    accept_m4, accept_mm, accept_img, accept_tts, accept_video, accept_vision; accept_x went with /x/ in 1.5.3)."""
    for name in ("accept_all", "accept_m4", "accept_mm", "accept_img", "accept_tts", "accept_video",
                 "accept_vision"):
        text = (SCRIPTS / f"{name}.py").read_text()
        assert "replica_port.Replicas.of(" in text and ("rp.endpoint(" in text or "rp.published(" in text), name
        assert "transport_checks(" in text, name
        assert "f\"http://{port" not in text and re.search(r"""\be\[['"]url['"]\]""", text) is None, name
    # accept_vision: a port that does not answer is no longer a refusal.
    assert "s in (0, 401)" not in (SCRIPTS / "accept_vision.py").read_text()


def test_fy_rport_016_plain_refused_reads_a_tls_alert_as_refused(rp):
    """FY-RPORT-016: a port that answers plain HTTP with TLS alert bytes and closes is refused (no HTTP answer); one
    that answers "HTTP/1.1 ..." is not."""
    for reply, want in ((b"\x15\x03\x01\x00\x02\x02\x46", True), (b"HTTP/1.1 400 Bad Request\r\n\r\n", False), (b"", True)):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)

        def serve(reply=reply, srv=srv):
            conn, _ = srv.accept()
            conn.recv(1024)
            if reply:
                conn.sendall(reply)
            conn.close()

        th = threading.Thread(target=serve, daemon=True)
        th.start()
        try:
            ok, how = rp.Replicas([], None).plain_refused(rp.Port("127.0.0.1", srv.getsockname()[1], "x", True, "n"))
            assert ok is want, (reply, how)
        finally:
            th.join(5)
            srv.close()


def test_fy_rport_017_a_certificate_naming_other_machines_too_fails(rp, guards, engine, tmp_path):
    """FY-RPORT-017: a certificate the CA signed that names machine b and an invented binding name as well as
    machine a verifies for a, but the name checks FAIL: they pass only on a host name mismatch."""
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    d = get_settings().tls_dir
    ca_key = serialization.load_pem_private_key((d / "ca.key").read_bytes(), None)
    ca_cert = x509.load_pem_x509_certificate((d / "ca.crt").read_bytes())
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    names = [replica_tls.binding_name(guards.a), replica_tls.binding_name(guards.b), rp.INVENTED]
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    (tmp_path / "wide.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM)
                                        + ca_cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "wide.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                          serialization.NoEncryption()))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(tmp_path / "wide.pem", tmp_path / "wide.key")
    g = GuardThread(engine.port, ssl_context=server)
    try:
        r = rp.Replicas(guards.machines, rp.context_of([ca_pem()]))
        out = r.transport_checks(r.endpoint(endpoint(g.port, "a")), "x")
        assert [o for _, o, _ in out] == [rp.PASS, rp.PASS, rp.FAIL, rp.FAIL], out
    finally:
        g.close()


def test_fy_rport_018_a_name_check_passes_only_on_a_host_name_mismatch(rp, monkeypatch):
    """FY-RPORT-018: another name is "refused" only when the handshake failed on the host name check
    (X509_V_ERR_HOSTNAME_MISMATCH); a refusal for any other reason (another verify error, a TLS error) is a FAIL, an
    unreachable port a SKIP, and a handshake that succeeds a FAIL."""
    r = rp.Replicas([], rp.context_of([]) or ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    p = rp.Port("127.0.0.1", 1, "a", True, "m1-x" + rp.BINDING_SUFFIX)
    for result, want in (((False, "refused: Hostname mismatch", rp.HOSTNAME_MISMATCH), rp.PASS),
                         ((False, "refused: self-signed certificate", 19), rp.FAIL),
                         ((False, "refused: SSLError", None), rp.FAIL),
                         ((True, "names n", None), rp.FAIL),
                         ((None, "ConnectionRefusedError", None), rp.SKIP)):
        monkeypatch.setattr(r, "handshake", lambda *a, result=result, **k: result)
        assert r._name_refused(p, "other", "w")[1] == want, result
