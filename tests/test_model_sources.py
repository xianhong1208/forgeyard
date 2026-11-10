"""Model sources, the cache layout and the SSRF guard (FY-SRC-*)."""

import http.server
import threading

import pytest

from forgeyard.models import fetch, sources
from forgeyard.models.sources import SourceError


def test_fy_src_001_only_hf_urls_and_local_are_sources():
    """FY-SRC-001"""
    assert sources.parse_source("hf://Qwen/Qwen3-4B") == sources.Source("hf", repo="Qwen/Qwen3-4B")
    assert sources.parse_source("local:///srv/models/x/").path == "/srv/models/x"
    assert sources.parse_source("https://models.example.org/a/b.gguf").kind == "url"
    for bad, why in [("hf://Qwen", "<org>/<name>"), ("hf://../etc/passwd", "<org>/<name>"),
                     ("hf://a/b/c", "<org>/<name>"), ("local://relative", "absolute"), ("local:///", "absolute"),
                     ("https://example.org/model.safetensors", ".gguf"), ("https://u:p@example.org/x.gguf", "credentials"),
                     ("file:///etc/passwd", "a model source is"), ("ftp://x/y.gguf", "a model source is"),
                     ("modelscope://Qwen/Qwen3-4B", "not supported")]:
        with pytest.raises(SourceError, match=why):
            sources.parse_source(bad)


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.5", "169.254.169.254",
                                  "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "fc00::1", "fe80::1",
                                  "::ffff:127.0.0.1", "::ffff:169.254.169.254", "198.18.0.1", "240.0.0.1"])
def test_fy_src_002_private_and_metadata_addresses_are_refused(addr):
    """FY-SRC-002: loopback, private, link-local (cloud metadata), CGNAT, multicast, reserved,
    IPv6 and IPv4-mapped forms."""
    assert fetch.address_problem(addr) is not None


def test_fy_src_009_the_reason_names_the_kind_of_address():
    """FY-SRC-009"""
    for addr, word in [("10.1.2.3", "private"), ("192.168.1.5", "private"), ("127.0.0.1", "loopback"),
                       ("169.254.169.254", "metadata"), ("100.64.0.1", "shared"), ("fc00::1", "private"),
                       ("::ffff:10.0.0.1", "private")]:
        assert word in fetch.address_problem(addr), addr


def test_fy_src_003_public_addresses_pass():
    """FY-SRC-003"""
    for addr in ("8.8.8.8", "18.239.50.1", "2606:4700:4700::1111"):
        assert fetch.address_problem(addr) is None


def _resolver(table):
    return lambda host, port: table[host]


def test_fy_src_004_policy_checks_scheme_host_and_every_address():
    """FY-SRC-004: https only (http only for a private mirror), each resolved address must be
    public, the allowlist applies to the first URL, wildcards cover subdomains only."""
    pol = fetch.Policy(resolver=_resolver({"hf.test": ["8.8.8.8"], "mixed.test": ["8.8.8.8", "10.0.0.5"],
                                           "inner.test": ["192.168.1.20"]}))
    assert pol.check("https://hf.test/x")[3] == "8.8.8.8"
    with pytest.raises(fetch.BlockedAddress, match="plain http"):
        pol.check("http://hf.test/x")
    with pytest.raises(fetch.BlockedAddress, match="private"):
        pol.check("https://mixed.test/x")
    with pytest.raises(fetch.BlockedAddress, match="private"):
        pol.check("https://inner.test/x")
    with pytest.raises(fetch.BlockedAddress, match="credentials"):
        pol.check("https://user:pw@hf.test/x")
    allow = fetch.Policy(allowed_hosts=["*.example.org", "cdn.test"],
                         resolver=_resolver({"a.example.org": ["8.8.8.8"], "example.org": ["8.8.8.8"],
                                             "cdn.test": ["8.8.8.8"], "evil.test": ["8.8.8.8"]}))
    assert allow.check("https://a.example.org/m.gguf")
    assert allow.check("https://cdn.test/m.gguf")
    for host in ("example.org", "evil.test"):
        with pytest.raises(fetch.BlockedAddress, match="not on the allowlist"):
            allow.check(f"https://{host}/m.gguf")
    assert allow.check("https://evil.test/m.gguf", first=False)  # a redirect: only the address check
    private = fetch.Policy(allow_private=True, resolver=_resolver({"mirror.lan": ["10.0.0.5"]}))
    assert private.check("http://mirror.lan/x")[3] == "10.0.0.5"


class _Redirector(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        _Redirector.seen.append((self.headers.get("Host"), self.path, self.headers.get("Authorization")))
        if self.path.startswith("/to-evil"):
            self.send_response(302)
            self.send_header("Location", f"http://evil.test:{self.server.server_address[1]}/stolen")
            self.end_headers()
            return
        if self.path.startswith("/to-cdn"):
            self.send_response(302)
            self.send_header("Location", f"http://cdn.test:{self.server.server_address[1]}/blob")
            self.end_headers()
            return
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def redirector(monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirector)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Redirector.seen = []
    real = fetch.address_problem
    # Pretend 127.0.0.1 is public and 127.0.0.2 private, so a local server can play both parts.
    monkeypatch.setattr(fetch, "address_problem", lambda a: "a private address" if a == "127.0.0.2" else (
        None if a == "127.0.0.1" else real(a)))
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_fy_src_005_every_redirect_is_checked_and_credentials_stay_home(redirector):
    """FY-SRC-005: a redirect to a private address is refused before any request reaches it, and
    an Authorization header is dropped when a redirect leaves the original host."""
    port = redirector
    pol = fetch.Policy(allow_http=True, resolver=_resolver({"good.test": ["127.0.0.1"], "evil.test": ["127.0.0.2"],
                                                            "cdn.test": ["127.0.0.1"]}))
    with pytest.raises(fetch.BlockedAddress, match="private"):
        fetch.get_bytes(f"http://good.test:{port}/to-evil", pol, headers={"Authorization": "Bearer s3cret"})
    assert [p for _, p, _ in _Redirector.seen] == ["/to-evil"]
    _Redirector.seen = []
    assert fetch.get_bytes(f"http://good.test:{port}/to-cdn", pol, headers={"Authorization": "Bearer s3cret"}) == b"ok"
    assert _Redirector.seen[0][2] == "Bearer s3cret" and _Redirector.seen[1] == (f"cdn.test:{port}", "/blob", None)


def test_fy_src_006_local_dirs_and_safe_paths():
    """FY-SRC-006"""
    dirs = ["/srv/models", "/data/llm/"]
    assert sources.local_allowed("/srv/models/qwen", dirs) and sources.local_allowed("/data/llm", dirs)
    assert not sources.local_allowed("/srv/models-evil/x", dirs)
    assert not sources.local_allowed("/srv/models/../../etc", dirs)
    assert not sources.local_allowed("/srv/models/x", []) and not sources.local_allowed("/x", ["/"])
    assert sources.safe_relpath("sub/dir/model.safetensors") == "sub/dir/model.safetensors"
    for bad in ("../x", "/etc/passwd", "a/../../b", "a\\b", "a/./b", "x\nb", ""):
        with pytest.raises(SourceError):
            sources.safe_relpath(bad)


def test_fy_src_007_which_files_are_downloaded():
    """FY-SRC-007: safetensors without other weight formats; one GGUF (or all parts of a split one)."""
    files = [{"path": p, "size": 1} for p in ("config.json", "model.safetensors", "pytorch_model.bin", "onnx/model.onnx",
                                             "original/consolidated.pth", "README.md", ".gitattributes", "a.gguf")]
    assert [f["path"] for f in sources.select_files(files, "safetensors")] == ["config.json", "model.safetensors",
                                                                               "README.md"]
    gg = [{"path": p, "size": 10} for p in ("m-Q4_K_M.gguf", "m-Q8_0.gguf", "big-00001-of-00002.gguf",
                                            "big-00002-of-00002.gguf", "README.md")]
    assert [f["path"] for f in sources.select_files(gg, "gguf", "m-Q4_K_M.gguf")] == ["m-Q4_K_M.gguf"]
    assert [f["path"] for f in sources.select_files(gg, "gguf", "big-00001-of-00002.gguf")] == [
        "big-00001-of-00002.gguf", "big-00002-of-00002.gguf"]
    choices = sources.gguf_choices(gg)
    assert [(c["path"], c["size"], c["parts"]) for c in choices] == [
        ("big-00001-of-00002.gguf", 20, 2), ("m-Q4_K_M.gguf", 10, 1), ("m-Q8_0.gguf", 10, 1)]
    assert sources.default_gguf(choices) == "m-Q4_K_M.gguf"
    with pytest.raises(SourceError, match="choose"):
        sources.select_files(gg, "gguf", None)


def test_fy_src_008_cache_layout_is_keyed_by_source_and_revision():
    """FY-SRC-008: Hugging Face layout on the host; the same paths under /models in a container."""
    rev = "a" * 40
    m = {"kind": "hf", "repo": "Qwen/Qwen3-4B", "revision": rev, "file": None}
    assert sources.layout(m) == {"mount": "hub/models--Qwen--Qwen3-4B", "path": f"hub/models--Qwen--Qwen3-4B/snapshots/{rev}",
                                 "file": None, "hub": "hub"}
    assert sources.container_paths({**m, "file": "x.gguf"})["file"] == f"/models/hub/models--Qwen--Qwen3-4B/snapshots/{rev}/x.gguf"
    assert sources.cache_key(m) == f"hub/models--Qwen--Qwen3-4B@{rev}"
    assert sources.cache_key({**m, "revision": "b" * 40}) != sources.cache_key(m)
    u = {"kind": "url", "url": "https://h.example/m.gguf"}
    assert sources.layout(u)["file"].startswith("url/") and sources.layout(u)["file"].endswith("/m.gguf")
    loc = {"kind": "local", "path": "/srv/models/q", "file": "q.gguf"}
    assert sources.container_paths(loc) == {"mount": "/models/local", "path": "/models/local",
                                            "file": "/models/local/q.gguf", "hub": "/models/hub"}
    with pytest.raises(SourceError, match="pinned"):
        sources.layout({**m, "revision": "main"})
