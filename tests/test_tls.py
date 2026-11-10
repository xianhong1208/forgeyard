"""HTTPS out of the box: Forgeyard's CA and server certificate, renewal, own certificates, plain
HTTP mode, cookies, HSTS and the trust endpoints (FY-TLS-*)."""

import datetime as dt
import ipaddress
import socket
import ssl
import stat

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from forgeyard.config import get_settings
from forgeyard.security import tls
from tests.conftest import OWNER, SETUP_TOKEN, add_user, make_client, sign_in
from tests.tls_helpers import LiveTLS, ca_of, free_port, https, peer_cert


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _cert(path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _sans(cert: x509.Certificate) -> tuple[list[str], list[str]]:
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return san.get_values_for_type(x509.DNSName), [str(i) for i in san.get_values_for_type(x509.IPAddress)]


def test_fy_tls_001_first_start_makes_a_ca_and_a_server_certificate():
    """FY-TLS-001: a CA (10 years, CA:TRUE pathlen 0, keyCertSign) and a server certificate signed by
    it (serverAuth, not a CA, at most 397 days); private keys 0600 in a 0700 directory."""
    m = tls.ensure()
    d = get_settings().tls_dir
    assert m.mode == "forgeyard-ca" and m.cert_file == d / "server.crt" and m.ca_pem == ca_of(get_settings().data_dir)
    assert _mode(d) == 0o700 and _mode(d / "ca.key") == 0o600 and _mode(d / "server.key") == 0o600
    assert _mode(d / "ca.crt") == 0o644
    ca, leaf = _cert(d / "ca.crt"), _cert(d / "server.crt")
    bc = ca.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert bc.ca and bc.path_length == 0
    assert ca.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign
    assert (ca.not_valid_after_utc - ca.not_valid_before_utc).days >= 3649
    assert "Forgeyard CA" in ca.subject.rfc4514_string()
    assert leaf.issuer == ca.subject
    ca.public_key().verify(leaf.signature, leaf.tbs_certificate_bytes, ec.ECDSA(leaf.signature_hash_algorithm))
    assert not leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value == x509.ExtendedKeyUsage(
        [ExtendedKeyUsageOID.SERVER_AUTH])
    assert (leaf.not_valid_after_utc - leaf.not_valid_before_utc).days <= 397
    # The chain file holds the server certificate, then the CA.
    assert (d / "server.crt").read_text().count("BEGIN CERTIFICATE") == 2
    serialization.load_pem_private_key((d / "ca.key").read_bytes(), password=None)
    # Written 0600 from the first byte (never briefly readable), and a key loosened by hand is tightened.
    tls._write_private(d / "probe.key", b"x")
    assert _mode(d / "probe.key") == 0o600
    (d / "probe.key").unlink()
    (d / "ca.key").chmod(0o644)
    tls.ensure()
    assert _mode(d / "ca.key") == 0o600


def test_fy_tls_002_the_server_certificate_names_every_address(monkeypatch):
    """FY-TLS-002: localhost and loopback, the host name (short and FQDN), the machine's own
    addresses, PUBLIC_URL's host, ALLOWED_HOSTS and TLS_EXTRA_NAMES (names and IPs)."""
    monkeypatch.setenv("PUBLIC_URL", "https://forge.lan:7860")
    monkeypatch.setenv("ALLOWED_HOSTS", "gpu-box.tail1234.ts.net")
    monkeypatch.setenv("TLS_EXTRA_NAMES", "alias.example, 203.0.113.7")
    get_settings.cache_clear()
    tls.ensure()
    names, ips = _sans(_cert(get_settings().tls_dir / "server.crt"))
    for n in ("localhost", "forge.lan", "gpu-box.tail1234.ts.net", "alias.example", socket.gethostname().lower()):
        assert n in names, (n, names)
    for ip in ("127.0.0.1", "::1", "203.0.113.7", *tls.local_addresses()):
        assert ip in ips, (ip, ips)
    assert _cert(get_settings().tls_dir / "server.crt").subject.rfc4514_string() == "CN=forge.lan"


def test_fy_tls_003_renewal_before_expiry_and_for_new_names_keeps_the_ca(monkeypatch):
    """FY-TLS-003: a second start keeps the certificate; less than 60 days left, or a name it does
    not cover, makes a new one signed by the same CA (agents and browsers notice nothing)."""
    d = get_settings().tls_dir
    tls.ensure()
    ca_before, first = (d / "ca.crt").read_bytes(), _cert(d / "server.crt").serial_number
    tls.ensure()
    assert _cert(d / "server.crt").serial_number == first

    names, ips = tls.wanted_names()
    tls.make_server_cert(d, names, ips, days=59)
    assert "expires in" in tls.renewal_reason(d, names, ips)
    tls.ensure()
    renewed = _cert(d / "server.crt")
    assert renewed.serial_number != first and (renewed.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)).days > 390
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=340)
    assert "expires in" in tls.renewal_reason(d, names, ips, now=later)  # it renews itself 60 days ahead

    monkeypatch.setenv("TLS_EXTRA_NAMES", "new-name.lan")
    get_settings.cache_clear()
    assert tls.renewal_reason(d, *tls.wanted_names()) == "it does not name new-name.lan"
    tls.ensure()
    assert "new-name.lan" in _sans(_cert(d / "server.crt"))[0]
    assert (d / "ca.crt").read_bytes() == ca_before


def test_fy_tls_004_a_running_server_switches_to_the_renewed_certificate(monkeypatch):
    """FY-TLS-004: the twice-a-day check renews the certificate and the listener uses it for new
    connections, without a restart; a client trusting the CA notices nothing."""
    port = free_port()
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", str(port))
    with LiveTLS(port) as srv:
        ca = ca_of(get_settings().data_dir)
        before = peer_cert(port, ca)["der"]
        assert tls.live.check() is False  # nothing due
        d = get_settings().tls_dir
        tls.make_server_cert(d, *tls.wanted_names(), days=30)  # as if 30 days were left
        assert tls.live.check() is True
        after = peer_cert(port, ca)
        assert after["der"] != before
        assert x509.load_der_x509_certificate(after["der"]).serial_number == _cert(d / "server.crt").serial_number
        assert https(port, "GET", "/api/health", ca)[0]["ok"] is True
        assert srv.config.ssl is tls.live.context


def _own_cert(tmp_path, names=("localhost",), ips=("127.0.0.1",)):
    """A company CA (another Forgeyard CA in another directory) and a certificate it signed."""
    company = tmp_path / "company"
    tls.make_ca(company)
    tls.make_server_cert(company, list(names), list(ips))
    return company


def test_fy_tls_005_own_certificate(monkeypatch, tmp_path):
    """FY-TLS-005: TLS_CERT_FILE / TLS_KEY_FILE are used as they are and Forgeyard makes no CA;
    TLS_CA_FILE becomes what agents pin; half a pair, a missing file or a key that does not belong
    to the certificate stop the start with a clear error; changed files are picked up."""
    company = _own_cert(tmp_path)
    monkeypatch.setenv("TLS_CERT_FILE", str(company / "server.crt"))
    get_settings.cache_clear()
    with pytest.raises(tls.TlsError, match="both TLS_CERT_FILE and TLS_KEY_FILE"):
        tls.ensure()
    monkeypatch.setenv("TLS_KEY_FILE", str(tmp_path / "nope.key"))
    get_settings.cache_clear()
    with pytest.raises(tls.TlsError, match="does not exist"):
        tls.ensure()
    other = _own_cert(tmp_path / "other")
    monkeypatch.setenv("TLS_KEY_FILE", str(other / "server.key"))
    get_settings.cache_clear()
    with pytest.raises(tls.TlsError, match="cannot be used together"):
        tls.ensure()
    monkeypatch.setenv("TLS_KEY_FILE", str(company / "server.key"))
    get_settings.cache_clear()
    m = tls.ensure()
    assert m.mode == "own" and m.ca_pem is None and not get_settings().tls_dir.exists()
    assert tls.trust_anchor_pem() is None  # a public certificate: agents use the system store
    monkeypatch.setenv("TLS_CA_FILE", str(company / "ca.crt"))
    get_settings.cache_clear()
    assert tls.ensure().ca_pem == (company / "ca.crt").read_text() == tls.trust_anchor_pem()

    port = free_port()
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", str(port))
    with LiveTLS(port):
        ca = (company / "ca.crt").read_text()
        before = peer_cert(port, ca)["der"]
        _, headers = https(port, "GET", "/api/health", ca)
        assert headers["strict-transport-security"] == "max-age=31536000"  # own certificate: HSTS
        tls.make_server_cert(company, ["localhost"], ["127.0.0.1"])  # certbot renewed the files
        assert tls.live.check() is True
        assert peer_cert(port, ca)["der"] != before


def test_fy_tls_006_plain_http_only_when_asked_and_admins_are_warned(monkeypatch):
    """FY-TLS-006: TLS=off makes no certificates and listens without TLS; the UI state says so,
    and that the page came over plain HTTP (unless a trusted proxy says https)."""
    monkeypatch.setenv("TLS", "off")
    get_settings.cache_clear()
    assert tls.ensure().mode == "off" and not get_settings().tls_dir.exists()
    port = free_port()
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", str(port))
    with LiveTLS(port) as srv:
        assert srv.config.ssl is None
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/auth/state", timeout=5) as r:
            import json
            assert json.loads(r.read())["tls"] == {"mode": "off", "https": False}
    with make_client() as c:
        assert c.get("/api/auth/state").json()["tls"] == {"mode": "off", "https": False}
        assert c.get("/api/tls/ca.pem").status_code == 404
    monkeypatch.setenv("TRUSTED_PROXIES", "testclient")
    get_settings.cache_clear()
    with make_client() as c:
        state = c.get("/api/auth/state", headers={"X-Forwarded-Proto": "https"}).json()
        assert state["tls"] == {"mode": "off", "https": True}  # behind a TLS proxy: no warning
    monkeypatch.setenv("TLS", "maybe")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="TLS must be 'on' or 'off'"):
        get_settings()


def test_fy_tls_007_cookies_are_secure_under_https():
    """FY-TLS-007: the session cookie is Secure when the browser talks HTTPS, and only then;
    HSTS is never sent with Forgeyard's own CA (the warning must stay clickable)."""
    with make_client(base_url="https://localhost") as c:
        r = c.post("/api/auth/setup", json={"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
        assert r.status_code == 200
        cookie = r.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        assert "strict-transport-security" not in r.headers
    with make_client() as c:  # plain http (a test client, as behind TLS=off)
        r = c.post("/api/auth/login", json={"username": OWNER[0], "password": OWNER[1]})
        assert r.status_code == 200 and "secure" not in r.headers["set-cookie"].lower()


def test_fy_tls_008_ca_downloads_info_and_the_pinned_join_command(owner, monkeypatch):
    """FY-TLS-008: the CA as PEM and DER, public (a device gets it before signing in); its
    fingerprint; admins also get the server certificate's names and the trust command; the join
    command pins the fingerprint over https."""
    tls.ensure()
    pem = ca_of(get_settings().data_dir)
    fp = tls.fingerprint(pem)
    with make_client(base_url="https://localhost") as anon:
        r = anon.get("/api/tls/ca.pem")
        assert r.status_code == 200 and r.text == pem and "forgeyard-ca.pem" in r.headers["content-disposition"]
        der = anon.get("/api/tls/ca.crt")
        assert der.headers["content-type"] == "application/x-x509-ca-cert" and der.content == ssl.PEM_cert_to_DER_cert(pem)
        info = anon.get("/api/tls").json()
        assert info["mode"] == "forgeyard-ca" and info["ca"]["fingerprint"] == fp and info["https"] is True
        assert info["ca"]["fingerprint_colons"].count(":") == 31 and "admin" not in info
    https_owner = sign_in(owner, *OWNER)
    https_owner.base_url = "https://localhost"
    info = https_owner.get("/api/tls").json()
    assert info["admin"]["trust_command"] == (f"uv run --no-sync python -m forgeyard agent trust --server "
                                               f"https://localhost --ca-fingerprint {fp}")
    assert "localhost" in info["admin"]["server_certificate"]["names"]
    joined = https_owner.post("/api/machines/join-tokens").json()
    assert joined["command"].endswith(f"--join-token {joined['token']} --ca-fingerprint {fp}")
    assert joined["ca_fingerprint"] == fp and joined["command"].startswith("uv run --no-sync python -m forgeyard agent "
                                                                          "--server https://localhost ")
    assert https_owner.get("/api/keys").json()["trust_url"] == "https://localhost/trust"
    add_user(owner, "alice")
    alice = sign_in(owner, "alice", "quiet-meadow-4821")
    alice.base_url = "https://localhost"
    assert "admin" not in alice.get("/api/tls").json()
    # Over plain http (TLS=off behind nothing) the command has no pin and no trust step.
    plain = owner.post("/api/machines/join-tokens").json()
    assert "--ca-fingerprint" not in plain["command"] and plain["trust_command"] is None


def test_fy_tls_009_a_lost_ca_key_is_never_replaced_silently():
    """FY-TLS-009: certificates without the CA key stop the start: a new CA would lock out every
    agent and browser that trusts the old one, so that must be a deliberate step."""
    tls.ensure()
    d = get_settings().tls_dir
    (d / "ca.key").unlink()
    with pytest.raises(tls.TlsError, match="ca.key is missing but certificates are there"):
        tls.ensure()


def test_fy_tls_010_fingerprints_and_names():
    """FY-TLS-010: the fingerprint is the SHA-256 of the DER certificate, shown two ways; a
    wildcard bind address is never a name."""
    tls.ensure()
    pem = ca_of(get_settings().data_dir)
    import hashlib
    assert tls.fingerprint(pem) == "sha256:" + hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    assert tls.colon_hex("sha256:ab01") == "AB:01"
    names, ips = tls.wanted_names()
    assert "0.0.0.0" not in ips and all(ipaddress.ip_address(i) for i in ips)


def test_fy_tls_011_an_http_public_url_from_1_0_is_upgraded_and_logged(monkeypatch, caplog):
    """FY-TLS-011: PUBLIC_URL=http://… kept from 1.0 would put dead http:// links in join commands
    and the gateway address of a server that only speaks HTTPS: it is read as https://, with a
    warning; with TLS=off (a proxy) it is left alone."""
    monkeypatch.setenv("PUBLIC_URL", "http://forge.lan:7860")
    get_settings.cache_clear()
    assert get_settings().public_url == "https://forge.lan:7860" and get_settings().public_url_upgraded
    caplog.set_level("WARNING")
    with make_client(base_url="https://forge.lan:7860") as c:
        r = c.post("/api/auth/setup", json={"setup_token": SETUP_TOKEN, "username": OWNER[0], "password": OWNER[1]})
        assert r.status_code == 200
        assert c.post("/api/machines/join-tokens").json()["server_url"] == "https://forge.lan:7860"
    assert "PUBLIC_URL says http:// but this server speaks HTTPS: using https://forge.lan:7860" in caplog.text
    monkeypatch.setenv("TLS", "off")
    get_settings.cache_clear()
    assert get_settings().public_url == "http://forge.lan:7860" and not get_settings().public_url_upgraded
