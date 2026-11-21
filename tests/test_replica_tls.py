"""TLS from the gateway to every replica (1.4.5, issue #163): certificates (CSR, machine binding, validity, renewal),
the guard serving TLS, the gateway checking chain and machine, plain HTTP only for older agents, the setup advisor
and the machine's page (FY-RTLS-*)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import socket
import ssl
import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from forgeyard.agent import replica_tls as agent_tls
from forgeyard.agent.replica_tls import CertificateRefused, ReplicaTls
from forgeyard.db import Node, Replica, new_session
from forgeyard.gateway import proxy
from forgeyard.machines.protocol import FEATURE_GUARD_TLS
from forgeyard.security import replica_tls, tls
from tests.gateway_helpers import FakeEngine, api_client, free_port, make_ready, new_key
from tests.guard_helpers import KEY, GuardThread, OpenEngine
from tests.machines_helpers import connect, hello, joined
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import make_node

TLS_FEATURES = MODELS + ("replica_keys", "replica_guard", FEATURE_GUARD_TLS)
OLD_FEATURES = MODELS + ("replica_keys", "replica_guard")


def node_of(node_id: int):
    db = new_session()
    try:
        n = db.get(Node, node_id)
        db.expunge(n)
        return n
    finally:
        db.close()


def fake_node(node_id: int = 7, credential: str = "c" * 64, features=("workloads", FEATURE_GUARD_TLS), **more):
    fields = {"status": "online", "connected_at": None, **more}
    return SimpleNamespace(id=node_id, credential_hash=credential, features=list(features), runtime_info={}, **fields)


def certified(n, tmp_path, *, now=None) -> ReplicaTls:
    """An agent's guards' TLS with a certificate the server signed for machine `n`."""
    t = ReplicaTls(tmp_path / f"agent-{n.id}-{n.credential_hash[:4]}")
    request_id, csr = t.request()
    chain, info = replica_tls.issue(n, csr, now=now)
    t.install(request_id, chain, name=replica_tls.binding_name(n), ca=replica_tls.ca_fingerprint())
    return t


def leaf(chain: str):
    from cryptography import x509

    return x509.load_pem_x509_certificate(agent_tls._pems(chain)[0].encode())


def csr_with(*, cn="evil", sans=(), key=None):
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = key or ec.generate_private_key(ec.SECP256R1())
    b = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
    if sans:
        b = b.add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(s)) if s[0].isdigit()
                                                         else x509.DNSName(s) for s in sans]), critical=False)
    return b.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode()


async def get(url: str, name, key: str = KEY, path: str = "/x"):
    transport = replica_tls.ReplicaTransport(limits=httpx.Limits())
    async with httpx.AsyncClient(transport=transport, timeout=5) as c:
        return await c.get(url + path, headers={"Authorization": f"Bearer {key}"}, extensions=replica_tls.ext(name))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def engine():
    e = OpenEngine()
    yield e
    e.close()


# ------------------------------------------------------------------ certificates
def test_fy_rtls_001_the_certificate_names_the_machine_and_nothing_the_agent_chose(tmp_path):
    """FY-RTLS-001: the server signs only the CSR's public key: subject and SAN are the machine's binding name alone (no
    address, nothing of the CSR's own subject or SANs, which here ask for 127.0.0.1 and the server's name), valid
    VALIDITY_DAYS, serverAuth, not a CA, chained to Forgeyard's CA (made on first use); the chain is the leaf then the
    CA; what is recorded names serial, expiry, name and CA."""
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    n = fake_node()
    now = dt.datetime(2026, 11, 21, 12, 0, tzinfo=dt.timezone.utc)
    chain, info = replica_tls.issue(n, csr_with(cn="localhost", sans=("127.0.0.1", "localhost", "forgeyard.example")),
                                    now=now)
    cert = leaf(chain)
    name = replica_tls.binding_name(n)
    assert name.startswith("m7-") and name.endswith(".replica.forgeyard.invalid")
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == [name] and san.get_values_for_type(x509.IPAddress) == []
    assert len(list(san)) == 1
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == name
    assert cert.not_valid_after_utc - now == dt.timedelta(days=replica_tls.VALIDITY_DAYS)
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == [ExtendedKeyUsageOID.SERVER_AUTH]
    ca_pem = (tmp_path / "data" / "tls" / "ca.crt").read_text()
    assert agent_tls._pems(chain)[1] == tls.first_pem(ca_pem)
    cert.verify_directly_issued_by(x509.load_pem_x509_certificate(ca_pem.encode()))
    assert info == {"serial": f"{cert.serial_number:x}", "not_after": "2026-12-21T12:00:00+00:00", "name": name,
                    "ca": tls.fingerprint(ca_pem), "issued_at": "2026-11-21T12:00:00+00:00"}


def test_fy_rtls_002_requests_that_are_refused(tmp_path):
    """FY-RTLS-002: no CSR, an unreadable one, one signed by another key than it carries, a weak key, a machine without
    a credential: refused, nothing signed. Each is IssueError `refused`."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    n = fake_node()
    good = csr_with()
    der = bytearray(ssl.PEM_cert_to_DER_cert(good.replace("CERTIFICATE REQUEST", "CERTIFICATE")))
    der[-1] ^= 0x01   # the last byte of the signature: still a readable CSR, no longer signed by its key
    tampered = ssl.DER_cert_to_PEM_cert(bytes(der)).replace("CERTIFICATE", "CERTIFICATE REQUEST")
    weak = csr_with(key=rsa.generate_private_key(public_exponent=65537, key_size=1024))
    for csr, why in (("", "no CSR"), ("not a csr", "cannot be read"), (tampered, "not signed by its own key"),
                     (weak, "too short")):
        with pytest.raises(replica_tls.IssueError) as e:
            replica_tls.issue(n, csr)
        assert e.value.code == "refused" and why in e.value.reason
    with pytest.raises(replica_tls.IssueError, match="no credential"):
        replica_tls.issue(fake_node(credential=None), good)


def test_fy_rtls_003_the_binding_name_follows_the_machine_not_its_row_id():
    """FY-RTLS-003: the name depends on the machine's credential too: a machine joined again (SQLite may give the new
    row the same id) gets another name; another machine another name; a revoked one (no credential) none, and
    is never dialled (`dial` None)."""
    a = replica_tls.binding_name(fake_node(7, "a" * 64))
    assert a == replica_tls.binding_name(fake_node(7, "a" * 64))
    assert a != replica_tls.binding_name(fake_node(7, "b" * 64)) and a != replica_tls.binding_name(fake_node(8, "a" * 64))
    assert replica_tls.binding_name(fake_node(7, None)) is None
    port = {"address": "10.0.0.5", "host_port": 30001}
    assert replica_tls.dial(SimpleNamespace(node=fake_node(7, None)), port) is None
    assert replica_tls.dial(SimpleNamespace(node=fake_node(7, "a" * 64)), port) == ("https://10.0.0.5:30001", a)
    old = fake_node(7, "a" * 64, features=("workloads", "replica_guard"))
    assert replica_tls.dial(SimpleNamespace(node=old), port) == ("http://10.0.0.5:30001", None)
    assert replica_tls.dial(SimpleNamespace(node=old), {"address": "fd00::5", "host_port": 9})[0] == "http://[fd00::5]:9"


def test_fy_rtls_004_the_agent_keeps_its_key_and_installs_only_what_answers_it(tmp_path):
    """FY-RTLS-004: the agent installs a chain only for the key of its pending request, naming the machine and CA the
    welcome gave; key (0600) and chain are kept, and a restarted agent serves them at once. A chain for another key,
    machine or CA, or an answer to no request, is refused and nothing changes."""
    n, other = fake_node(7, "a" * 64), fake_node(8, "b" * 64)
    name, ca = replica_tls.binding_name(n), replica_tls.ca_fingerprint
    t = ReplicaTls(tmp_path / "state")
    assert t.info is None and t.wants(name, None) == "there is none yet"
    rid, csr = t.request()
    chain, info = replica_tls.issue(n, csr)
    with pytest.raises(CertificateRefused, match="no request"):
        t.install("another-id", chain, name=name, ca=ca())
    _, other_csr = ReplicaTls(None).request()
    foreign, _ = replica_tls.issue(n, other_csr)
    with pytest.raises(CertificateRefused, match="not for the key"):
        t.install(rid, foreign, name=name, ca=ca())
    with pytest.raises(CertificateRefused, match="names"):
        t.install(rid, chain, name=replica_tls.binding_name(other), ca=ca())
    with pytest.raises(CertificateRefused, match="another CA"):
        t.install(rid, chain, name=name, ca="sha256:" + "0" * 64)
    assert t.info is None and not (tmp_path / "state" / "replica-tls" / "chain.pem").exists()
    got = t.install(rid, chain, name=name, ca=ca())
    assert got["serial"] == info["serial"] and got["name"] == name and got["ca"] == ca()
    key = tmp_path / "state" / "replica-tls" / "key.pem"
    assert key.stat().st_mode & 0o777 == 0o600 and (tmp_path / "state" / "replica-tls").stat().st_mode & 0o777 == 0o700
    assert t.wants(name, ca()) is None and t.report() == {k: info[k] for k in ("serial", "not_after", "name", "ca")}
    again = ReplicaTls(tmp_path / "state")
    assert again.info == t.info and again.wants(name, ca()) is None
    with pytest.raises(CertificateRefused):
        again.install(rid, chain)  # the restarted agent has no request in flight


def test_fy_rtls_005_when_the_agent_asks_again(tmp_path):
    """FY-RTLS-005: renewal: nothing to do with RENEW_BEFORE or more left; a new one with less, once expired, for
    another machine name (joined again) or another CA (a new CA on the server)."""
    n = fake_node()
    t = certified(n, tmp_path)
    name, ca = replica_tls.binding_name(n), replica_tls.ca_fingerprint()
    end = t.not_after()
    assert t.wants(name, ca, now=end - agent_tls.RENEW_BEFORE - dt.timedelta(minutes=1)) is None
    assert t.wants(name, ca, now=end - agent_tls.RENEW_BEFORE + dt.timedelta(minutes=1)) == "it expires in 9 days"
    assert t.wants(name, ca, now=end + dt.timedelta(seconds=1)) == "it has expired"
    assert t.wants(replica_tls.binding_name(fake_node(7, "d" * 64)), ca) == "it names another machine"
    assert t.wants(name, "sha256:" + "1" * 64) == "another CA signed it"
    assert agent_tls.RENEW_BEFORE == dt.timedelta(days=replica_tls.RENEW_BEFORE_DAYS)


# ------------------------------------------------------------------ the guard and the gateway's client
def test_fy_rtls_006_the_gateway_reaches_the_guard_only_over_tls_with_the_machines_name(tmp_path, engine):
    """FY-RTLS-006: through a guard serving machine A's certificate: dialled as A, the request reaches the engine with
    the key checked inside TLS. Dialled as machine B, or with no machine name at all (refused before any connection),
    it never gets there; plain HTTP to that port does not either (the handshake fails, the key check never runs)."""
    a, b = fake_node(1, "a" * 64), fake_node(2, "b" * 64)
    t = certified(a, tmp_path)
    g = GuardThread(engine.port, ssl_context=t.context)
    url = f"https://127.0.0.1:{g.port}"
    try:
        r = run(get(url, replica_tls.binding_name(a)))
        assert r.status_code == 200 and json.loads(r.text)["auth"] == f"Bearer {KEY}"
        with pytest.raises(httpx.ConnectError, match="Hostname mismatch"):
            run(get(url, replica_tls.binding_name(b)))
        with pytest.raises(httpx.ConnectError, match="binding name"):
            run(get(url, None))
        with pytest.raises(httpx.ConnectError, match="binding name"):
            run(get(url, "127.0.0.1"))  # an address is never enough
        with pytest.raises(httpx.HTTPError):
            run(get(f"http://127.0.0.1:{g.port}", None))
        assert len(engine.seen) == 1 and g.guard.forwarded == 1 and g.guard.refused == 0
        r = run(get(url, replica_tls.binding_name(a), key="wrong"))
        assert r.status_code == 401 and g.guard.refused == 1  # the key is still checked, now inside TLS
    finally:
        g.close()


def test_fy_rtls_007_expired_or_foreign_certificates_are_refused(tmp_path, engine):
    """FY-RTLS-007: a certificate that has expired, or one signed by another CA for the same name (another server's,
    or a forged one), is refused: the request never reaches the engine."""
    a = fake_node(1, "a" * 64)
    old = certified(a, tmp_path, now=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40))
    g = GuardThread(engine.port, ssl_context=old.context)
    try:
        with pytest.raises(httpx.ConnectError, match="expired"):
            run(get(f"https://127.0.0.1:{g.port}", replica_tls.binding_name(a)))
    finally:
        g.close()
    # Another CA, same machine name.
    other = tmp_path / "other-ca"
    tls.make_ca(other)
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization

    forged = ReplicaTls(tmp_path / "forged")
    rid, csr = forged.request()
    ca_key = serialization.load_pem_private_key((other / "ca.key").read_bytes(), None)
    ca_cert = x509.load_pem_x509_certificate((other / "ca.crt").read_bytes())
    now = dt.datetime.now(dt.timezone.utc)
    name = replica_tls.binding_name(a)
    cert = (x509.CertificateBuilder().subject_name(x509.Name([])).issuer_name(ca_cert.subject)
            .public_key(x509.load_pem_x509_csr(csr.encode()).public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=1)).not_valid_after(now + dt.timedelta(days=5))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    forged.install(rid, (cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM)).decode())
    g = GuardThread(engine.port, ssl_context=forged.context)
    try:
        with pytest.raises(httpx.ConnectError, match="certificate verify failed"):
            run(get(f"https://127.0.0.1:{g.port}", name))
        assert engine.seen == []
    finally:
        g.close()


def test_fy_rtls_008_a_guard_without_a_certificate_refuses_then_switches_in_place(tmp_path, engine):
    """FY-RTLS-008: a TLS guard whose agent has no certificate yet refuses every connection (as a connect error, which
    moves a request to the next replica) and never answers plain HTTP; the certificate installed later is served by
    the same listener without a restart, and a renewal reaches new connections while open ones keep theirs."""
    a = fake_node(1, "a" * 64)
    name = replica_tls.binding_name(a)
    t = ReplicaTls(tmp_path / "state")
    g = GuardThread(engine.port, ssl_context=t.context)
    url = f"https://127.0.0.1:{g.port}"
    try:
        with pytest.raises(httpx.ConnectError):
            run(get(url, name))
        rid, csr = t.request()
        chain, first = replica_tls.issue(a, csr)
        t.install(rid, chain, name=name)

        def serial_seen() -> str:
            ctx, _ = replica_tls.client_context()
            with socket.create_connection(("127.0.0.1", g.port), timeout=5) as raw:
                with ctx.wrap_socket(raw, server_hostname=name) as s:
                    return f"{int(s.getpeercert()['serialNumber'], 16):x}"

        assert serial_seen() == first["serial"]
        assert run(get(url, name)).status_code == 200
        ctx, _ = replica_tls.client_context()
        kept = ctx.wrap_socket(socket.create_connection(("127.0.0.1", g.port), timeout=5), server_hostname=name)
        rid, csr = t.request()
        chain, second = replica_tls.issue(a, csr)
        t.install(rid, chain, name=name)
        assert serial_seen() == second["serial"] != first["serial"]
        assert f"{int(kept.getpeercert()['serialNumber'], 16):x}" == first["serial"]
        kept.sendall(b"GET /k HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + KEY.encode() + b"\r\n\r\n")
        assert kept.recv(64).startswith(b"HTTP/1.1 200")
        kept.close()
    finally:
        g.close()


def test_fy_rtls_009_engine_down_still_says_not_taken_over_tls(tmp_path):
    """FY-RTLS-009: with its engine gone, a TLS guard still answers 503 with X-Forgeyard-Not-Taken (1.4.5), inside TLS,
    so the gateway tries the next replica."""
    a = fake_node(1, "a" * 64)
    t = certified(a, tmp_path)
    g = GuardThread(free_port(), ssl_context=t.context)
    try:
        r = run(get(f"https://127.0.0.1:{g.port}", replica_tls.binding_name(a)))
        assert r.status_code == 503 and r.headers["x-forgeyard-not-taken"] == "engine-unreachable"
        assert proxy.not_taken(r)
    finally:
        g.close()


# ------------------------------------------------------------------ the gateway end to end
@pytest.fixture()
def tls_gw(owner, tmp_path):
    """Chat with two replicas, each behind a real guard: on two TLS machines (their own certificates), or on two
    machines with agents before 1.4.5 (plain guards)."""
    seed_catalog()
    made = []

    def build(features, *, engines=("a", "b"), dead=()):
        ids = [make_node(f"m-{i}", features=features) for i in range(len(engines))]
        deploy(owner, "chat", "qwen3-1.7b-gguf", "llama-cpp", replicas=len(engines), params={"gpus": 0, "threads": 2},
               wait=True)
        guards, fakes = [], []
        for i, (nid, label) in enumerate(zip(ids, engines)):
            e = FakeEngine(label, KEY)
            fakes.append(e)
            ctx = certified(node_of(nid), tmp_path).context if FEATURE_GUARD_TLS in features else None
            g = GuardThread(free_port() if i in dead else e.port, ssl_context=ctx)
            guards.append(g)
        made.extend(guards + fakes)
        make_ready("chat", [(nid, g.port, KEY) for nid, g in zip(ids, guards)])
        key = new_key(owner, "laptop")["key"]
        return SimpleNamespace(ids=ids, guards=guards, engines=fakes, api=api_client(owner, key))

    yield build
    for x in made:
        x.close()


def chat(c):
    return c.post("/v1/chat/completions", json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]})


def test_fy_rtls_010_chat_over_tls_to_each_machine_and_failover(tls_gw):
    """FY-RTLS-010: /v1 to replicas on TLS machines: answered over https:// with each machine's own certificate (one
    pool per machine); with one engine down, its guard's 503 Not-Taken moves every request to the other replica."""
    gw = tls_gw(TLS_FEATURES, dead=(0,))
    for _ in range(4):
        r = chat(gw.api)
        assert r.status_code == 200, r.text
        assert r.json()["choices"][0]["message"]["content"] == "hello from b"
    assert gw.guards[0].guard.forwarded == 0 and len(gw.engines[1].posts("/v1/chat/completions")) == 4


def test_fy_rtls_011_older_agents_stay_plain_and_tls_agents_are_never_downgraded(tls_gw):
    """FY-RTLS-011: machines whose agents are older than 1.4.5 (no guard_tls) are dialled over plain HTTP and serve
    (Tony's decision: upgrading never interrupts serving). A TLS machine is never dialled over plain HTTP: when its
    guard does not speak TLS (here: plain, as a stand-in for a broken certificate), its replicas get nothing at all,
    not plain HTTP, and the request fails over or fails."""
    gw = tls_gw(OLD_FEATURES)
    for _ in range(2):
        r = chat(gw.api)
        assert r.status_code == 200, r.text
    assert sum(len(e.posts("/v1/chat/completions")) for e in gw.engines) == 2
    # Both machines now say they serve TLS, but their guards still speak plain HTTP.
    db = new_session()
    try:
        for nid in gw.ids:
            db.get(Node, nid).features = list(TLS_FEATURES)
        db.commit()
    finally:
        db.close()
    r = chat(gw.api)
    assert r.status_code in (502, 503), r.text
    assert sum(len(e.posts("/v1/chat/completions")) for e in gw.engines) == 2  # nothing more reached them
    assert all(g.guard.forwarded == 1 for g in gw.guards)


def test_fy_rtls_012_try_it_and_the_routes_use_the_same_tls(tls_gw, owner):
    """FY-RTLS-012: a deployment's endpoints say https:// and `encrypted` (never the binding name), and Try it reaches
    the replica over TLS with the machine's certificate, the way the gateway does."""
    gw = tls_gw(TLS_FEATURES, engines=("a",))
    dep = owner.get("/api/deployments").json()["deployments"][0]
    detail = owner.get(f"/api/deployments/{dep['id']}").json()["deployment"]
    [ep] = detail["endpoints"]
    assert ep["url"].startswith("https://127.0.0.1:") and ep["encrypted"] is True and "tls_name" not in ep
    r = owner.post(f"/api/deployments/{dep['id']}/try", json={})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert r.json()["summary"] == "hello from a" and gw.guards[0].guard.forwarded == 1


def test_fy_rtls_013_passthrough_and_video_jobs_dial_with_the_binding(owner):
    """FY-RTLS-013: /x/ and /v1/videos build their targets the same way: https:// and the machine's binding for a TLS
    agent, plain HTTP for an older one."""
    from forgeyard.gateway import videojobs

    seed_catalog()
    nid = make_node("tls-box", features=TLS_FEATURES)
    deploy(owner, "chat", "qwen3-1.7b-gguf", "llama-cpp", params={"gpus": 0, "threads": 2}, wait=True)
    [uid] = make_ready("chat", [(nid, 30001, "s")])
    db = new_session()
    try:
        where = videojobs.where(db, uid, f"{db.scalar(select(Replica).where(Replica.uid == uid)).generation}:0")
    finally:
        db.close()
    name = replica_tls.binding_name(node_of(nid))
    assert where.url == "https://127.0.0.1:30001" and where.tls_name == name and where.ext == {"sni_hostname": name}
    db = new_session()
    try:
        [t] = proxy.load_route(db, "chat").targets
    finally:
        db.close()
    assert t.url == "https://127.0.0.1:30001" and t.ext == {"sni_hostname": name}


# ------------------------------------------------------------------ the server's side of the agent stream
def _answer(ws, kind: str = "certificate") -> dict:
    """The next message of that type (a `desired` list comes first to an agent with workloads)."""
    for _ in range(10):
        msg = ws.receive_json()
        if msg["type"] == kind:
            return msg
    raise AssertionError(f"no {kind}")


def _online(ws, **over):
    ws.send_json(hello(**over))
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome", welcome
    return welcome


def test_fy_rtls_014_the_stream_issues_certificates(owner):
    """FY-RTLS-014: a TLS agent's welcome names its binding and the CA; a cert_request is answered with a chain for
    that name, which the machine's runtime_info then records; a second one within ISSUE_EVERY is `too_often`; a bad
    CSR is `refused` and recorded as the machine's tls_error. An agent without guard_tls gets no replica_tls, and a
    cert_request from it is refused (4003) like any message sent without its feature."""
    from tests.machines_helpers import expect_close

    j = joined(owner)
    with connect(owner, j["credential"]) as ws:
        welcome = _online(ws, features=["workloads", "replica_keys", "replica_guard", "guard_tls"])
        n = node_of(j["node_id"])
        assert welcome["replica_tls"] == {"name": replica_tls.binding_name(n), "ca": replica_tls.ca_fingerprint()}
        t = ReplicaTls(None)
        rid, csr = t.request()
        ws.send_json({"v": 1, "type": "cert_request", "request_id": rid, "csr": csr})
        answer = _answer(ws)
        assert answer["type"] == "certificate" and answer["ok"] is True and answer["request_id"] == rid
        info = t.install(rid, answer["chain"], name=welcome["replica_tls"]["name"], ca=welcome["replica_tls"]["ca"])
        assert answer["name"] == info["name"] and answer["not_after"] == info["not_after"]
        assert node_of(j["node_id"]).runtime_info["tls"] == {k: info[k] for k in ("serial", "not_after", "name", "ca")}
        rid2, csr2 = t.request()
        ws.send_json({"v": 1, "type": "cert_request", "request_id": rid2, "csr": csr2})
        assert _answer(ws) == {"v": 1, "type": "certificate", "request_id": rid2, "ok": False, "code": "too_often",
                                     "reason": f"one certificate per {replica_tls.ISSUE_EVERY:g} s on a connection"}
    with connect(owner, j["credential"]) as ws:
        _online(ws, features=["workloads", "guard_tls"])
        ws.send_json({"v": 1, "type": "cert_request", "request_id": "x1", "csr": "-----BEGIN CERTIFICATE REQUEST-----"})
        answer = _answer(ws)
        assert (answer["ok"], answer["code"]) == (False, "refused")
        assert "cannot be read" in node_of(j["node_id"]).runtime_info["tls_error"]["reason"]
    with connect(owner, j["credential"]) as ws:
        welcome = _online(ws, features=["workloads", "replica_keys", "replica_guard"])
        assert "replica_tls" not in welcome
        ws.send_json({"v": 1, "type": "cert_request", "request_id": "x2", "csr": csr})
        assert expect_close(ws) == 4003


def test_fy_rtls_015_hello_reports_what_the_guards_serve(owner):
    """FY-RTLS-015: hello's runtime_info.tls is kept as reported when well formed (dropped otherwise), and a new hello
    clears the previous connection's tls_error."""
    j = joined(owner)
    good = {"serial": "ab12", "not_after": "2026-12-21T12:00:00+00:00", "name": "m1-x.replica.forgeyard.invalid",
            "ca": "sha256:" + "a" * 64}
    for sent, kept in ((good, good), ({**good, "ca": "md5:1"}, None), ({**good, "serial": "zz"}, None),
                       ({**good, "not_after": "soon"}, None), ("x", None)):
        with connect(owner, j["credential"]) as ws:
            _online(ws, features=["workloads", "guard_tls"], runtime_info={"tls": sent})
            assert node_of(j["node_id"]).runtime_info.get("tls") == kept
            assert "tls_error" not in node_of(j["node_id"]).runtime_info


# ------------------------------------------------------------------ status, the advisor, the machine's page
def test_fy_rtls_016_status_of_each_machine(tmp_path):
    """FY-RTLS-016: plain for an agent without guard_tls; ok with a current certificate; pending for a fresh connection
    without one, missing after MISSING_GRACE (or at once after a refusal, with its reason, or for a certificate of
    another machine or CA); expiring under ERROR_BEFORE_DAYS; expired after; none without workloads or revoked."""
    now = dt.datetime(2026, 11, 21, 12, 0, tzinfo=dt.timezone.utc)
    n = fake_node()
    replica_tls.ensure_ca()
    st = lambda node, at=now: replica_tls.status(node, now=at)  # noqa: E731
    assert st(fake_node(features=("workloads", "replica_guard")))["state"] == "plain"
    assert st(fake_node(features=("replica_guard",))) is None
    assert st(fake_node(status="revoked")) is None
    n.connected_at = now - dt.timedelta(seconds=30)
    assert st(n)["state"] == "pending"
    n.connected_at = now - replica_tls.MISSING_GRACE - dt.timedelta(seconds=1)
    assert st(n) == {"state": "missing", "not_after": None, "reason": None}
    n.connected_at = now
    replica_tls.record_error(n, "the CSR cannot be read", now)
    assert st(n) == {"state": "missing", "not_after": None, "reason": "the CSR cannot be read"}
    _, info = replica_tls.issue(n, csr_with(), now=now)
    replica_tls.record(n, info)
    assert "tls_error" not in n.runtime_info
    assert st(n) == {"state": "ok", "not_after": "2026-12-21T12:00:00+00:00", "reason": None}
    end = now + dt.timedelta(days=replica_tls.VALIDITY_DAYS)
    assert st(n, end - dt.timedelta(days=replica_tls.ERROR_BEFORE_DAYS) + dt.timedelta(minutes=1))["state"] == "expiring"
    assert st(n, end - dt.timedelta(days=replica_tls.ERROR_BEFORE_DAYS) - dt.timedelta(minutes=1))["state"] == "ok"
    assert st(n, end)["state"] == "expired"
    other = fake_node(credential="e" * 64)
    other.runtime_info = dict(n.runtime_info)
    other.connected_at = now - dt.timedelta(hours=1)
    assert st(other)["state"] == "missing" and "another machine" in st(other)["reason"]
    n.runtime_info = {"tls": {**n.runtime_info["tls"], "ca": "sha256:" + "f" * 64}}
    assert st(n)["state"] in ("pending", "missing")


def _set(node_id: int, **fields):
    db = new_session()
    try:
        n = db.get(Node, node_id)
        for k, v in fields.items():
            setattr(n, k, v)
        db.commit()
    finally:
        db.close()


def test_fy_rtls_017_setup_health_and_the_machine_page(owner):
    """FY-RTLS-017: an older agent: a warning "Traffic to <machine> is not encrypted: update the agent" that opens its
    update command, and `replica_tls.state` plain on the machine; a TLS machine without a certificate (after the
    grace): an error naming why its replicas cannot be reached; with a current one: nothing, and `ok`."""
    from forgeyard import __version__
    from tests.test_agent_update import FULL

    old = make_node("old-box", features=OLD_FEATURES)
    new = make_node("new-box", features=TLS_FEATURES)
    # The server's version, built from code before #163: only guard_tls missing; said once, as not encrypted.
    same = make_node("same-box", features=[f for f in FULL if f != FEATURE_GUARD_TLS])
    _set(same, agent_version=__version__)
    _set(new, connected_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10))
    findings = {f["key"]: f for f in owner.get("/api/advisor").json()["findings"]}
    assert f"replica-traffic-plain@machine:{same}" in findings and f"agent-outdated@machine:{same}" not in findings
    assert f"agent-outdated@machine:{old}" in findings   # older than the server: both are said
    plain = findings[f"replica-traffic-plain@machine:{old}"]
    assert plain["severity"] == "warning" and plain["title"] == "Traffic to old-box is not encrypted: update the agent"
    assert plain["fix_url"] == f"/machines/{old}#update" and plain["fix_label"] == "Update command"
    missing = findings[f"replica-certificate@machine:{new}"]
    assert missing["severity"] == "error" and not missing["snoozable"]
    assert "never falls back to plain HTTP" in missing["detail"]
    assert f"replica-traffic-plain@machine:{new}" not in findings
    machines = {m["name"]: m for m in owner.get("/api/machines").json()["machines"]}
    assert machines["old-box"]["replica_tls"]["state"] == "plain"
    assert machines["new-box"]["replica_tls"]["state"] == "missing"
    n = node_of(new)
    _, info = replica_tls.issue(n, csr_with())
    replica_tls.record(n, info)
    _set(new, runtime_info=n.runtime_info)
    findings = {f["key"] for f in owner.get("/api/advisor").json()["findings"]}
    assert f"replica-certificate@machine:{new}" not in findings
    m = next(m for m in owner.get("/api/machines").json()["machines"] if m["name"] == "new-box")
    assert m["replica_tls"] == {"state": "ok", "not_after": info["not_after"], "reason": None}


# ------------------------------------------------------------------ the agent's side
def test_fy_rtls_018_guards_follow_the_server_and_switch_when_it_changes(tmp_path):
    """FY-RTLS-018: the supervisor's guards serve TLS only for a server that lists guard_tls; at a new connection to a
    server of the other kind each guard listens again that way; to the same kind nothing is reopened."""
    from forgeyard.agent.workloads.supervisor import Supervisor

    sup = Supervisor(tmp_path / "state", {})
    assert sup.tls_required is False and sup.tls.usable
    sup.new_connection({"workloads"})
    assert sup.tls_required is False
    sup.new_connection({"workloads", FEATURE_GUARD_TLS})
    assert sup.tls_required is True

    async def scenario():
        from forgeyard.agent.workloads.records import Record

        port, internal = free_port(), free_port()
        rec = Record(id="r1", workload="chat", ordinal=0, generation=1, kind="service", runtime="process", spec={},
                     ports=[{"name": "http", "host_port": port, "internal": internal, "address": "127.0.0.1"}])
        sup.keys["r1"] = (None, KEY)
        assert await sup._open_guard(rec) is None
        first = sup.guards["r1"][0]
        assert first.tls and first.ssl_context is sup.tls.context
        sup.new_connection({"workloads", FEATURE_GUARD_TLS})
        assert await sup._open_guard(rec) is None and sup.guards["r1"][0] is first   # same kind: kept
        sup.new_connection({"workloads"})
        assert await sup._open_guard(rec) is None
        assert sup.guards["r1"][0] is not first and not sup.guards["r1"][0].tls and not first.listening
        await sup._close_guard("r1")

    run(scenario())


def test_fy_rtls_019_without_cryptography_the_agent_does_not_offer_tls(tmp_path, monkeypatch):
    """FY-RTLS-019: an agent that cannot make a key (no cryptography package) does not list guard_tls, so the server
    reaches it over plain HTTP and says so; with it, hello lists guard_tls (16 features at most, what servers up to
    1.4.0 accept) and reports the certificate its guards serve."""
    from forgeyard.agent.client import Agent
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory
    from forgeyard.agent.models import ModelCache
    from forgeyard.agent.workloads.container import ContainerRuntime
    from forgeyard.agent.workloads.records import RecordStore
    from forgeyard.agent.workloads.supervisor import Supervisor
    from tests.workload_helpers import FakeDocker

    monkeypatch.setenv(SIMULATION_ENV, "1")
    rt = ContainerRuntime(RecordStore(tmp_path / "s"), agent_id="a", engine=str(FakeDocker(tmp_path / "bin").path))
    inv = SimulatedInventory("nvidia-rtx4090x2")
    sup = Supervisor(tmp_path / "state", {"container": rt}, models=ModelCache(tmp_path / "c"), port_range=(32190, 32199))
    msg = Agent("http://127.0.0.1:9", "cred", inv, supervisor=sup).hello(inv.full())
    assert FEATURE_GUARD_TLS in msg["features"] and len(msg["features"]) <= 16 and "tls" not in msg["runtime_info"]
    t = certified(fake_node(), tmp_path)
    sup.tls = t
    msg = Agent("http://127.0.0.1:9", "cred", inv, supervisor=sup).hello(inv.full())
    assert msg["runtime_info"]["tls"] == t.report()
    monkeypatch.setattr(agent_tls, "available", lambda: False)
    bare = Supervisor(tmp_path / "state2", {"container": rt})
    assert bare.tls.usable is False
    msg = Agent("http://127.0.0.1:9", "cred", inv, supervisor=bare).hello(inv.full())
    assert FEATURE_GUARD_TLS not in msg["features"]
    bare.new_connection({"workloads", FEATURE_GUARD_TLS})
    assert bare.tls_required is False


class FakeWs:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, text):
        self.sent.append(json.loads(text))


def test_fy_rtls_020_the_agent_asks_installs_and_renews(tmp_path, monkeypatch):
    """FY-RTLS-020: the agent's certificate loop: asks at once when it has none, installs the answer and stops asking;
    an answer to an earlier request is ignored; a refusal waits RETRY_AFTER before asking again; an unanswered request
    is sent again after ANSWER_WAIT; a certificate getting old is renewed."""
    from forgeyard.agent.client import Agent
    from forgeyard.agent.inventory import SIMULATION_ENV, SimulatedInventory
    from forgeyard.agent.workloads.supervisor import Supervisor

    monkeypatch.setenv(SIMULATION_ENV, "1")
    monkeypatch.setattr(agent_tls, "ANSWER_WAIT", 0.4)
    monkeypatch.setattr(agent_tls, "RETRY_AFTER", 0.8)
    monkeypatch.setattr(agent_tls, "CHECK_EVERY", 0.2)
    n = fake_node()
    sup = Supervisor(tmp_path / "state", {})
    agent = Agent("http://127.0.0.1:9", "cred", SimulatedInventory("nvidia-rtx4090x2"), supervisor=sup)
    agent._tls_want = {"name": replica_tls.binding_name(n), "ca": replica_tls.ca_fingerprint()}

    async def until(pred, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not pred():
            assert time.monotonic() < deadline, "timed out"
            await asyncio.sleep(0.02)

    async def scenario():
        ws = FakeWs()
        loop = asyncio.create_task(agent._tls_loop(ws))
        try:
            await until(lambda: len(ws.sent) == 1)
            first = ws.sent[0]
            assert first["type"] == "cert_request" and first["csr"].startswith("-----BEGIN CERTIFICATE REQUEST")
            # Unanswered: sent again (a new key) after ANSWER_WAIT; the late answer to the first is ignored.
            await until(lambda: len(ws.sent) == 2, timeout=3)
            second = ws.sent[1]
            chain, _ = replica_tls.issue(n, first["csr"])
            agent._certificate({"request_id": first["request_id"], "ok": True, "chain": chain})
            assert sup.tls.info is None and sup.tls.refused_at == 0.0 and sup.tls.answers(second["request_id"])
            # Refused: nothing more until RETRY_AFTER.
            agent._certificate({"request_id": second["request_id"], "ok": False, "code": "failed", "reason": "x"})
            await asyncio.sleep(0.5)
            assert len(ws.sent) == 2
            await until(lambda: len(ws.sent) == 3, timeout=3)
            third = ws.sent[2]
            chain, info = replica_tls.issue(n, third["csr"])
            agent._certificate({"request_id": third["request_id"], "ok": True, "chain": chain})
            assert sup.tls.info["serial"] == info["serial"]
            await asyncio.sleep(0.6)
            assert len(ws.sent) == 3   # nothing to do while it is young
            # It gets old: renewed.
            sup.tls.info = {**sup.tls.info, "not_after": (dt.datetime.now(dt.timezone.utc)
                                                          + dt.timedelta(days=2)).isoformat()}
            await until(lambda: len(ws.sent) == 4, timeout=3)
        finally:
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)

    run(scenario())


def test_fy_rtls_021_one_pool_per_machine(tmp_path, engine):
    """FY-RTLS-021: the gateway's client keeps one connection pool per machine name: a connection made (and checked)
    for machine A is never reused for a request meant for machine B at the same address, which is refused."""
    a, b = fake_node(1, "a" * 64), fake_node(2, "b" * 64)
    t = certified(a, tmp_path)
    g = GuardThread(engine.port, ssl_context=t.context)
    url = f"https://127.0.0.1:{g.port}/x"

    async def scenario():
        transport = replica_tls.ReplicaTransport(limits=httpx.Limits())
        async with httpx.AsyncClient(transport=transport, timeout=5) as c:
            headers = {"Authorization": f"Bearer {KEY}"}
            r = await c.get(url, headers=headers, extensions=replica_tls.ext(replica_tls.binding_name(a)))
            assert r.status_code == 200
            with pytest.raises(httpx.ConnectError, match="Hostname mismatch"):
                await c.get(url, headers=headers, extensions=replica_tls.ext(replica_tls.binding_name(b)))
            assert set(transport._tls) == {replica_tls.binding_name(a), replica_tls.binding_name(b)}
            r = await c.get(url, headers=headers, extensions=replica_tls.ext(replica_tls.binding_name(a)))
            assert r.status_code == 200

    try:
        run(scenario())
        assert g.guard.forwarded == 2
    finally:
        g.close()


def test_fy_rtls_022_x_passthrough_over_tls(owner, tmp_path):
    """FY-RTLS-022: an app exposed at /x/ on a TLS machine is reached over TLS with that machine's certificate (its
    guard strips the key as before)."""
    from tests.test_passthrough import GUARDED, App, expose_workload, ready_behind

    node = make_node("cpu-tls", "nvidia-rtx4090x2", features=GUARDED + (FEATURE_GUARD_TLS,))
    app = App()
    guard = GuardThread(app.port, strip_auth=True, ssl_context=certified(node_of(node), tmp_path).context)
    try:
        expose_workload(owner, "web", node)
        ready_behind("web", [guard.port])
        api = api_client(owner, new_key(owner, "laptop", workloads="all")["key"])
        r = api.get("/x/web/hello")
        assert r.status_code == 200, r.text
        assert r.json()["path"] == "/hello" and r.json()["auth"] is None and guard.guard.forwarded == 1
    finally:
        guard.close()
        app.close()


def test_fy_rtls_023_voices_and_speech_over_tls(owner, tmp_path):
    """FY-RTLS-023: a speech deployment on a TLS machine: its voice list (GET /v1/audio/voices, and the one speech
    asks for itself) and the speech come over TLS with the machine's certificate."""
    from forgeyard.gateway import speech
    from tests.model_helpers import add_catalog
    from tests.test_speech import FakeSpeech, qwen_tts_meta

    seed_catalog()
    add_catalog("qwen3-tts-0.6b", qwen_tts_meta(), task="tts", fmt="safetensors",
                source="hf://Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice")
    gpu = make_node("a100", allowlist=[4], features=MODELS + ("replica_keys", "replica_guard", FEATURE_GUARD_TLS))
    engine = FakeSpeech("s-qwen")
    guard = GuardThread(engine.port, key=engine.secret, ssl_context=certified(node_of(gpu), tmp_path).context)
    speech.forget_voices()
    try:
        deploy(owner, "qwen", "qwen3-tts-0.6b", "vllm-omni-tts", wait=True)
        make_ready("qwen", [(gpu, guard.port, engine.secret)])
        api = api_client(owner, new_key(owner, "laptop")["key"])
        r = api.get("/v1/audio/voices", params={"model": "qwen"})
        assert r.status_code == 200, r.text
        assert [v["id"] for v in r.json()["voices"]] == ["vivian", "ryan", "uncle_fu"]
        speech.forget_voices()
        r = api.post("/v1/audio/speech", json={"model": "qwen", "input": "Hello there."})  # the gateway picks a voice
        assert r.status_code == 200, r.text
        assert guard.guard.forwarded >= 3 and guard.guard.refused == 0
    finally:
        guard.close()
        engine.close()
        speech.forget_voices()


def test_fy_rtls_024_video_jobs_over_tls(owner, tmp_path):
    """FY-RTLS-024: a video deployment on a TLS machine: the job is sent, followed, its content streamed and the job
    deleted over TLS with the machine's certificate, each a request through the guard."""
    from forgeyard.gateway import videojobs
    from tests.model_helpers import add_catalog
    from tests.test_video_generation import FEATURES, make, sweep, wait_status, wan_meta
    from tests.video_helpers import FakeVideos

    add_catalog("wan2.1-t2v-1.3b", wan_meta(), task="video", fmt="diffusers", source="hf://Wan-AI/Wan2.1-T2V-1.3B-Diffusers")
    gpu = make_node("a100", allowlist=[4], features=FEATURES + (FEATURE_GUARD_TLS,))
    engine = FakeVideos("s-0", work=0.15)
    guard = GuardThread(engine.port, key=engine.secret, ssl_context=certified(node_of(gpu), tmp_path).context)
    videojobs.forget()
    try:
        deploy(owner, "wan", "wan2.1-t2v-1.3b", "vllm-omni-video", wait=True)
        make_ready("wan", [(gpu, guard.port, engine.secret)])
        api = api_client(owner, new_key(owner, "laptop")["key"])
        r = make(api)
        assert r.status_code == 200, r.text
        vid = r.json()["id"]
        assert wait_status(owner, api, vid, "completed")["status"] == "completed"
        c = api.get(f"/v1/videos/{vid}/content")
        assert c.status_code == 200 and c.content[4:8] == b"ftyp"
        assert api.delete(f"/v1/videos/{vid}").status_code == 200
        assert len(engine.deletes) == 1   # the replica was asked to delete it, over TLS
        sweep(owner)
        assert guard.guard.forwarded >= 4 and guard.guard.refused == 0 and len(engine.posts) == 1
    finally:
        guard.close()
        engine.close()
        videojobs.forget()


def test_fy_rtls_025_without_the_ca_key_no_new_certificates_but_the_others_still_verify(tmp_path, engine):
    """FY-RTLS-025: with data/tls/ca.key gone (ca.crt still there) the server signs nothing ("failed", and no new CA
    is made over the old one), but the gateway still verifies and reaches machines that have their certificate."""
    a = fake_node(1, "a" * 64)
    t = certified(a, tmp_path)
    key = tmp_path / "data" / "tls" / "ca.key"
    key.rename(tmp_path / "ca.key.moved")
    with pytest.raises(replica_tls.IssueError) as e:
        replica_tls.issue(fake_node(2, "b" * 64), csr_with())
    assert e.value.code == "failed" and "only one of ca.key and ca.crt" in e.value.reason and not key.exists()
    g = GuardThread(engine.port, ssl_context=t.context)
    try:
        assert run(get(f"https://127.0.0.1:{g.port}", replica_tls.binding_name(a))).status_code == 200
    finally:
        g.close()
