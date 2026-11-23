"""Try this version (1.5.2, #95 PR 2; FY-ETRY-001…020): a custom copy of a built-in engine pinned to a version the
last check found. Against the fake registry and GitHub of tests/engineupdate_helpers.py (the real tag lists of
2026-11-23), with real llama.cpp attestations verified by sigstore against the trust root it ships: the copy's YAML
(the built-in's arguments, the new images by digest, the left-out variants and why), the refusals (a version the
check did not find, a moved tag, a taken name, a stored answer that is not the built-in variant's own form), process
archives, signatures (verified, failed, missing, unsigned upstream, no network), `based_on` and the catalog's engine
arguments, and the `image_pins` table: its migration, and every reader of recorded ids (the relay through the server,
the ids sent for a `docker load`ed copy, a machine's engine images and their removal, an engine page's presence)."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from forgeyard.config import get_settings
from forgeyard.db import AuditEvent, CustomRecipe, ImagePin, new_session
from forgeyard.machines import engineimages, relay, upload
from forgeyard.models import engineversions as ev, enginetry, fetch, recipes, render, signatures
from forgeyard.models.recipes import RecipeError, parse_recipe
from forgeyard.workloads import images, pinning, pins, registry as oci
from tests.conftest import add_user, sign_in
from tests.engineupdate_helpers import SD_REPO as SD, FakeUpstream, Image, real_attestations, real_llama_blobs, sha

LLAMA = "ggml-org/llama.cpp"
VULKAN_1 = "sha256:784bb87c7b22cf8653322f3680f33e1fd013d122d817441d5d2fc6410dee68da"
VULKAN_TAG = "server-vulkan-v1.0.0-b12359-b1b770d"
ARCHIVE_1 = "0bc1f091e8916102b6d8f2cf944233a620e90a0f856c72d0ce41150c65c2e72e"
OLD = "sha256:" + "ab" * 32


def offline_root(_policy, _data_dir):
    """The trust root shipped in the sigstore package (no TUF refresh: the tests have no network)."""
    import tempfile
    from pathlib import Path

    from sigstore._utils import read_embedded
    from sigstore.models import TrustedRoot

    path = Path(tempfile.mkdtemp()) / "trusted_root.json"
    path.write_bytes(read_embedded("trusted_root.json", signatures.TUF_URL))
    return TrustedRoot.from_file(str(path))


def llama_text() -> str:
    """llama-cpp as if its built-in were still at 0.5.0 (b11000): a Vulkan image and the Ubuntu archive. The check
    then finds 1.0.0, whose real image and archive (and their real attestations) the fake upstream serves."""
    text = recipes.builtin_recipes()["llama-cpp"][1].split("\nvariants:")[0]
    text = text.replace('version: "1.0.0"', 'version: "0.5.0"')
    return text + f"""
variants:
  - name: vulkan
    when: {{vendor: amd}}
    runtime: container
    amd_kfd: false
    image: ghcr.io/ggml-org/llama.cpp:server-vulkan-v0.5.0-b11000-abcdef0@{OLD}
    unsupported: {{kv_cache: [f8_e4m3]}}
  - name: cpu-process
    when: {{vendor: cpu, arch: [x86_64]}}
    runtime: process
    engine:
      url: https://github.com/ggml-org/llama.cpp/releases/download/b11000/llama-b11000-bin-ubuntu-x64.tar.gz
      sha256: {"cd" * 32}
      binary: llama-b11000/llama-server
"""


@pytest.fixture()
def up(monkeypatch):
    server = FakeUpstream()
    monkeypatch.setitem(oci.ENDPOINTS, "registry-1.docker.io", server.url)
    monkeypatch.setitem(oci.ENDPOINTS, "ghcr.io", server.url)
    monkeypatch.setattr(ev, "GITHUB_API", server.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "1")
    get_settings.cache_clear()
    # The built-in recipes as the check and Try see them: sd-cpp pinned to the fake's copies, llama-cpp at "0.5.0".
    texts = {}
    for name, (_, text) in recipes.builtin_recipes().items():
        if name == "sd-cpp":
            for real, fake in server.sd_pins.items():
                text = text.replace(real, fake)
        texts[name] = text
    texts["llama-cpp"] = llama_text()
    built = {n: (parse_recipe(t, builtin=True), t) for n, t in texts.items()}
    monkeypatch.setattr(recipes, "_builtin_cache", built)
    # vLLM 0.34.0's images (cuda, rocm), as small images of their own.
    server.vllm = {}
    for repo, tag in (("vllm/vllm-openai", "v0.34.0"), ("vllm/vllm-openai-rocm", "v0.34.0"),
                      ("vllm/vllm-openai", "v0.33.0"), ("vllm/vllm-openai-rocm", "v0.33.0")):
        img = Image(f"{repo}-{tag}", "2026-11-16T00:00:00Z")
        server.add_image(repo, tag, img)
        server.vllm[(repo, tag)] = img
    # llama.cpp 1.0.0's real Vulkan image and the attestations of it and of the b12359 archive.
    for digest, data in real_llama_blobs().items():
        server.blobs[digest] = data
    server.digests[(LLAMA, VULKAN_TAG)] = VULKAN_1
    for digest, body in real_attestations().items():
        server.attestations[(LLAMA, digest)] = body
    monkeypatch.setattr(signatures, "trusted_root", offline_root)
    yield server
    server.close()
    get_settings.cache_clear()


def check(owner):
    r = owner.post("/api/engine-updates/check")
    assert r.status_code == 200, r.text
    return r.json()["result"]


def try_(owner, recipe, version, **body):
    return owner.post(f"/api/recipes/{recipe}/try", json={"version": version, **body})


def pin_rows() -> dict:
    db = new_session()
    try:
        return {p.ref: p for p in db.scalars(select(ImagePin)).all()}
    finally:
        db.close()


def created_audits() -> list[dict]:
    db = new_session()
    try:
        return [e.detail | {"_target": e.target} for e in db.scalars(
            select(AuditEvent).where(AuditEvent.action == "recipe.created").order_by(AuditEvent.id))]
    finally:
        db.close()


def custom_names() -> list[str]:
    db = new_session()
    try:
        return list(db.scalars(select(CustomRecipe.name)).all())
    finally:
        db.close()


# ------------------------------------------------------------------ a copy
def test_fy_etry_001_vllm_0_34_0_copied_with_the_built_ins_arguments_and_its_images_pinned(owner, up):
    """FY-ETRY-001: Try vLLM 0.34.0 makes `vllm-0-34-0` (201): title "vLLM 0.34.0", version 0.34.0, based_on vllm, the
    built-in's arguments and settings unchanged, cuda and rocm pinned by the digests the registry serves now, cuda-12.9
    left out with the check's reason (no -cu129 image past 0.32.0), a header comment that says what it is; one
    `image_pins` row per image with the recorded ids read from the registry, marked "unsigned upstream" (vLLM signs
    nothing); audited as recipe.created with where it came from; the engine's page and the recipe list show it."""
    check(owner)
    r = try_(owner, "vllm", "0.34.0")
    assert r.status_code == 201, r.text
    body = r.json()
    rec = body["recipe"]
    assert rec["name"] == "vllm-0-34-0" and rec["title"] == "vLLM 0.34.0" and rec["version"] == "0.34.0"
    assert rec["based_on"] == "vllm" and rec["builtin"] is False
    cuda, rocm = up.vllm[("vllm/vllm-openai", "v0.34.0")], up.vllm[("vllm/vllm-openai-rocm", "v0.34.0")]
    assert [(v["name"], v["image"]) for v in rec["variants"]] == [
        ("cuda", f"vllm/vllm-openai:v0.34.0@{cuda.digest}"), ("rocm", f"vllm/vllm-openai-rocm:v0.34.0@{rocm.digest}")]
    assert all(v["image_check"] for v in rec["variants"])          # recorded ids: works offline, comes through
    assert rec["copy"]["from_version"] == "0.32.0" and rec["copy"]["kind"] == "release"
    assert rec["copy"]["made_by"] == "owner" and rec["copy"]["made_at"]
    lo = rec["copy"]["left_out"]
    assert [x["variant"] for x in lo] == ["cuda-12.9"] and lo[0]["code"] == "no_image"
    assert lo[0]["params"]["tag"] == "v0.34.0-cu129" and lo[0]["params"]["last"] == "0.32.0"
    assert body["left_out"] == lo
    text = rec["yaml"]
    assert text.startswith("# A copy of the built-in recipe vllm (vLLM 0.32.0) for 0.34.0")
    assert "# Left out: cuda-12.9 (No v0.34.0-cu129 image" in text and "upstream publishes no signature" in text
    builtin = recipes.builtin_recipes()["vllm"][0]
    copy = parse_recipe(text)
    assert copy.args == builtin.args and copy.optional_args == builtin.optional_args and copy.params == builtin.params
    assert copy.env == builtin.env and copy.estimate == builtin.estimate and copy.engine == "vllm"
    assert copy.variant("cuda").entrypoint == builtin.variant("cuda").entrypoint
    assert copy.variant("cuda").when == builtin.variant("cuda").when
    rows = pin_rows()
    assert set(rows) == {v["image"] for v in rec["variants"]}
    row = rows[rec["variants"][0]["image"]]
    assert row.entry == {"index": cuda.digest, "platforms": {"linux/amd64": {
        "manifest": sha(cuda.manifest), "config": sha(cuda.config), "content": None}}}
    assert row.signature["status"] == "unsigned" and "vLLM publishes no signatures" in row.signature["note"]
    assert row.recorded_by == "owner" and body["signatures"]["cuda"]["status"] == "unsigned"
    audit = created_audits()[-1]
    assert audit["_target"] == "vllm-0-34-0" and audit["based_on"] == "vllm" and audit["from_version"] == "0.32.0"
    assert audit["version"] == "0.34.0" and audit["left_out"] == ["cuda-12.9"]
    assert audit["images"] == [v["image"] for v in rec["variants"]]
    assert audit["signatures"] == {"cuda": "unsigned", "rocm": "unsigned"}
    page = owner.get("/api/recipes/vllm-0-34-0").json()
    assert page["recipe"]["based_on"] == "vllm" and page["pins"]["cuda"]["signature"]["status"] == "unsigned"
    assert page["pins"]["cuda"]["recorded_by"] == "owner"
    listed = {x["name"]: x for x in owner.get("/api/recipes").json()["recipes"]}
    assert listed["vllm-0-34-0"]["based_on"] == "vllm" and listed["vllm"]["based_on"] is None
    # The built-in stays as it is.
    assert recipes.builtin_recipes()["vllm"][0].version == "0.32.0"
    assert owner.get("/api/recipes/vllm").json()["recipe"]["variants"][0]["image"].startswith("vllm/vllm-openai:v0.32.0@")


def test_fy_etry_002_the_name_is_chosen_checked_and_never_taken_twice(owner, up):
    """FY-ETRY-002: a name may be given; a taken name (a custom recipe, or the copy made a moment ago), a built-in's
    name and a name recipes cannot have are refused before any registry is asked; 0.33.0 can be tried beside 0.34.0."""
    check(owner)
    asked = len(up.requests)
    r = try_(owner, "vllm", "0.34.0", name="vllm")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "builtin_name"
    r = try_(owner, "vllm", "0.34.0", name="vLLM 0.34")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_name"
    assert len(up.requests) == asked
    assert try_(owner, "vllm", "0.34.0", name="vllm-next").status_code == 201
    asked = len(up.requests)
    r = try_(owner, "vllm", "0.34.0", name="vllm-next")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "name_taken"
    assert len(up.requests) == asked                                   # refused before any registry is asked
    r = try_(owner, "vllm", "0.33.0")
    assert r.status_code == 201 and r.json()["recipe"]["name"] == "vllm-0-33-0"
    assert enginetry.default_name("vllm-pooling", "0.34.0") == "vllm-pooling-0-34-0"
    assert enginetry.default_name("sd-cpp", "master-1117-0d71f96") == "sd-cpp-master-1117-0d71f96"
    assert len(enginetry.default_name("x" * 38, "1.2.3")) <= 40
    assert sorted(custom_names()) == ["vllm-0-33-0", "vllm-next"]


def test_fy_etry_003_only_a_version_the_last_check_found_and_only_admins(owner, up):
    """FY-ETRY-003: before any check, for a version the check did not find (or an older one), for a built-in that
    moved since the check, for a custom engine and an unknown one, Try is refused; operators and users get 403."""
    r = try_(owner, "vllm", "0.34.0")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "check_again"
    check(owner)
    for version in ("0.35.0", "0.32.0", "0.31.0", "v0.34.0", "latest"):
        r = try_(owner, "vllm", version)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "check_again", version
    r = try_(owner, "faster-whisper", "0.9.0")        # an rc: never a candidate
    assert r.status_code == 409
    add_user(owner, "olga", role="operator")
    op = sign_in(owner, "olga", "quiet-meadow-4821")
    assert try_(op, "vllm", "0.34.0").status_code == 403
    assert try_(owner, "nope", "1.0.0").status_code == 404
    assert try_(owner, "vllm", "0.34.0", name="mine").status_code == 201
    r = try_(owner, "mine", "0.34.0")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_builtin"
    # Forgeyard was updated after the check: its built-in vllm is now another version.
    db = new_session()
    try:
        from forgeyard.db import ServerSetting

        row = db.get(ServerSetting, ev.SETTING_KEY)
        value = json.loads(json.dumps(row.value))
        value["engines"]["vllm"]["version"] = "0.31.0"
        row.value = value
        db.commit()
    finally:
        db.close()
    r = try_(owner, "vllm", "0.34.0", name="again")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "check_again" and "0.31.0" in r.json()["detail"]["message"]


def test_fy_etry_004_a_tag_that_moved_since_the_check_is_refused_and_nothing_is_written(owner, up):
    """FY-ETRY-004: the digest is resolved again at Try; v0.34.0 pointing at another image than when checked is a 409
    digest_moved naming the variant, and no recipe, no pin and no audit event is written."""
    check(owner)
    up.add_image("vllm/vllm-openai-rocm", "v0.34.0", Image("rebuilt", "2026-11-20T00:00:00Z"))
    r = try_(owner, "vllm", "0.34.0")
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["code"] == "digest_moved" and d["variant"] == "rocm" and "Check for newer versions again" in d["message"]
    assert custom_names() == [] and pin_rows() == {} and created_audits() == []


def test_fy_etry_005_the_stored_answer_is_never_trusted_for_where_or_what(owner, up):
    """FY-ETRY-005: the registry and repository always come from the built-in variant's own image; a stored tag that is
    not that variant's form for that version (a tampered or stale result) is refused; the image string the check
    stored is never used. Only the built-in's own repositories are asked."""
    from forgeyard.db import ServerSetting

    check(owner)

    def tamper(fn):
        db = new_session()
        try:
            row = db.get(ServerSetting, ev.SETTING_KEY)
            value = json.loads(json.dumps(row.value))
            fn(next(c for c in value["engines"]["vllm"]["candidates"] if c["version"] == "0.34.0"))
            row.value = value
            db.commit()
        finally:
            db.close()

    tamper(lambda c: c["variants"][0].update(image="evil/engine:1@" + OLD))   # ignored
    start = len(up.requests)
    r = try_(owner, "vllm", "0.34.0")
    assert r.status_code == 201, r.text
    assert r.json()["recipe"]["variants"][0]["image"].startswith("vllm/vllm-openai:v0.34.0@")
    repos = {x["path"].split("/manifests/")[0].removeprefix("/v2/") for x in up.requests[start:]
             if "/manifests/" in x["path"]}
    assert repos == {"vllm/vllm-openai", "vllm/vllm-openai-rocm"}
    for bad in ("latest", "v0.33.0", "v0.34.0-cu129", "../../x", "v0.34.0rc1"):
        tamper(lambda c, bad=bad: c["variants"][0].update(tag=bad))
        r = try_(owner, "vllm", "0.34.0", name=f"x-{len(bad)}-{bad.count('.')}")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "check_again", bad
    tamper(lambda c: c["variants"][0].update(tag="v0.34.0", digest="not-a-digest"))
    assert try_(owner, "vllm", "0.34.0", name="y").json()["detail"]["code"] == "check_again"


def test_fy_etry_006_a_rolling_build_unsigned_upstream_and_its_archive_left_out(owner, up):
    """FY-ETRY-006: stable-diffusion.cpp's newer build: cuda and vulkan pinned at the digests their rolling tags point
    at, the cpu-process archive left out (the new release's Linux archive is built for another system), version
    "master-1117-0d71f96", kind build; stable-diffusion.cpp publishes only an unsigned build record: said so."""
    check(owner)
    r = try_(owner, "sd-cpp", "master-1117-0d71f96")
    assert r.status_code == 201, r.text
    rec = r.json()["recipe"]
    assert rec["name"] == "sd-cpp-master-1117-0d71f96" and rec["version"] == "master-1117-0d71f96"
    assert rec["copy"]["kind"] == "build" and [v["name"] for v in rec["variants"]] == ["cuda", "vulkan"]
    assert rec["variants"][0]["image"] == f"ghcr.io/leejet/stable-diffusion.cpp:master-cuda@{up.digests[(SD, 'master-cuda')]}"
    lo = rec["copy"]["left_out"]
    assert [x["variant"] for x in lo] == ["cpu-process"] and lo[0]["code"] in ("no_archive", "no_release")
    sig = r.json()["signatures"]["cuda"]
    assert sig["status"] == "unsigned" and "unsigned build record" in sig["note"]


# ------------------------------------------------------------------ process archives and signatures
def test_fy_etry_007_llama_cpp_signatures_verified_for_its_image_and_its_archive(owner, up):
    """FY-ETRY-007: llama.cpp signs its images and archives (GitHub artifact attestations): the 1.0.0 Vulkan image and
    the b12359 Ubuntu archive are verified (real bundles, sigstore, the trust root shipped with it) and the signer
    recorded: its own repository's workflows (docker.yml at refs/tags/v1.0.0; release-publish.yml), the commit and
    the run. The archive's URL and SHA-256 move to b12359, read again from GitHub at Try; the image's recorded ids are
    the real image's (amd64 and arm64)."""
    check(owner)
    r = try_(owner, "llama-cpp", "1.0.0")
    assert r.status_code == 201, r.text
    rec = r.json()["recipe"]
    assert rec["name"] == "llama-cpp-1-0-0" and [v["name"] for v in rec["variants"]] == ["vulkan", "cpu-process"]
    vulkan = rec["variants"][0]["image"]
    assert vulkan == f"ghcr.io/ggml-org/llama.cpp:{VULKAN_TAG}@{VULKAN_1}"
    copy = parse_recipe(rec["yaml"])
    assert copy.variant("cpu-process").engine.url.endswith("/b12359/llama-b12359-bin-ubuntu-x64.tar.gz")
    assert copy.variant("cpu-process").engine.sha256 == ARCHIVE_1
    assert copy.variant("cpu-process").engine.binary == "llama-b11000/llama-server"   # the built-in's, as it is
    sig = pin_rows()[vulkan].signature
    assert sig["status"] == "verified" and sig["repository"] == LLAMA and sig["workflow"] == ".github/workflows/docker.yml"
    assert sig["ref"] == "refs/tags/v1.0.0" and sig["commit"] == "b1b770d90d94cad9e9ea5b993bdbc383a522fb28"
    assert sig["run"].startswith("https://github.com/ggml-org/llama.cpp/actions/runs/") and sig["log_index"]
    assert sig["predicate"] == "https://slsa.dev/provenance/v1" and sig["issuer"] == signatures.GITHUB_ISSUER
    arch = rec["copy"]["archives"][0]
    assert arch["variant"] == "cpu-process" and arch["release"] == "b12359"
    assert arch["signature"]["status"] == "verified" and arch["signature"]["workflow"] == ".github/workflows/release-publish.yml"
    assert set(pin_rows()[vulkan].entry["platforms"]) == {"linux/amd64", "linux/arm64"}
    assert "signature verified: ggml-org/llama.cpp .github/workflows/docker.yml" in rec["yaml"]
    assert created_audits()[-1]["signatures"] == {"vulkan": "verified", "cpu-process": "verified"}


@pytest.mark.parametrize("case", ["other_subject", "tampered", "missing", "github_down", "trust_root_down"])
def test_fy_etry_008_a_signature_that_fails_or_cannot_be_checked_refuses_the_copy(owner, up, monkeypatch, case):
    """FY-ETRY-008: for a project that signs, the copy is refused (nothing written) when the attestation is about other
    artifacts (the archive's statement served for the image), when its bytes were changed, when GitHub has none for the
    digest (422 signature_missing), when GitHub cannot be reached, or when Sigstore's trust root cannot be refreshed
    (502 cannot_verify: never treated as unsigned)."""
    check(owner)
    key = (LLAMA, VULKAN_1)
    if case == "other_subject":
        up.attestations[key] = up.attestations[(LLAMA, f"sha256:{ARCHIVE_1}")]
    elif case == "tampered":
        body = json.loads(json.dumps(up.attestations[key]))
        env = body["attestations"][0]["bundle"]["dsseEnvelope"]
        import base64

        payload = json.loads(base64.b64decode(env["payload"]))
        payload["predicate"]["buildDefinition"]["externalParameters"]["workflow"]["ref"] = "refs/heads/evil"
        env["payload"] = base64.b64encode(json.dumps(payload).encode()).decode()
        up.attestations[key] = body
    elif case == "missing":
        del up.attestations[key]
    elif case == "github_down":
        up.fail[f"/repos/{LLAMA}/attestations/"] = 503
    else:
        def down(_policy, _data_dir):
            raise signatures.SignatureError("cannot_verify", "Could not refresh Sigstore's trust root: no network.")
        monkeypatch.setattr(signatures, "trusted_root", down)
    r = try_(owner, "llama-cpp", "1.0.0")
    want = {"other_subject": (422, "signature_failed"), "tampered": (422, "signature_failed"),
            "missing": (422, "signature_missing"), "github_down": (502, "cannot_verify"),
            "trust_root_down": (502, "cannot_verify")}[case]
    assert (r.status_code, r.json()["detail"]["code"]) == want, r.text
    assert r.json()["detail"]["variant"] == "vulkan"
    if case == "other_subject":
        assert "other artifacts" in r.json()["detail"]["message"]
    assert custom_names() == [] and pin_rows() == {} and created_audits() == []


def test_fy_etry_009_an_archive_whose_sha256_changed_since_the_check_is_refused(owner, up):
    """FY-ETRY-009: the archive's SHA-256 is read again from GitHub at Try; a release asset replaced since the check
    (another digest) or gone is a 409 digest_moved for that variant."""
    check(owner)
    rel = next(r for r in up.releases[LLAMA] if r["tag_name"] == "b12359")
    asset = next(a for a in rel["assets"] if a["name"] == "llama-b12359-bin-ubuntu-x64.tar.gz")
    asset["digest"] = "sha256:" + "ee" * 32
    r = try_(owner, "llama-cpp", "1.0.0")
    assert r.status_code == 409 and r.json()["detail"] == {**r.json()["detail"], "code": "digest_moved",
                                                           "variant": "cpu-process"}
    rel["assets"] = [a for a in rel["assets"] if a is not asset]
    assert try_(owner, "llama-cpp", "1.0.0").json()["detail"]["code"] == "digest_moved"
    assert custom_names() == []


def test_fy_etry_010_verify_checks_the_signer_the_issuer_and_the_subject(up, tmp_path):
    """FY-ETRY-010: signatures.verify on the real bundles: the right digest verifies; another repository's policy
    (the signer must be a workflow of the repository named in SOURCES) fails; a digest with no attestation is
    signature_missing; `about` says who signs."""
    policy = fetch.Policy(allow_private=True)
    got = signatures.verify("llama.cpp", VULKAN_1, what="x", policy=policy, data_dir=tmp_path)
    assert got["status"] == "verified" and got["workflow"] == ".github/workflows/docker.yml"
    with pytest.raises(signatures.SignatureError) as e:
        signatures.verify("llama.cpp", "sha256:" + "0" * 64, what="x", policy=policy, data_dir=tmp_path)
    assert e.value.code == "signature_missing"
    # The same bundle, claimed for another project: its certificate names ggml-org/llama.cpp, so it fails.
    up.attestations[("acme/llama.cpp", VULKAN_1)] = up.attestations[(LLAMA, VULKAN_1)]
    up.releases["acme/llama.cpp"] = []
    src = dict(signatures.SOURCES)
    try:
        signatures.SOURCES["llama.cpp"] = {**src["llama.cpp"], "repository": "acme/llama.cpp"}
        with pytest.raises(signatures.SignatureError) as e:
            signatures.verify("llama.cpp", VULKAN_1, what="x", policy=policy, data_dir=tmp_path)
        assert e.value.code == "signature_failed"
    finally:
        signatures.SOURCES.clear()
        signatures.SOURCES.update(src)
    assert signatures.about("llama.cpp") == {"signed": True, "kind": "github-attestation", "repository": LLAMA}
    assert signatures.about("vllm")["signed"] is False and signatures.unsigned("vllm")["status"] == "unsigned"
    assert "no signatures that Forgeyard can verify" in signatures.about("someone")["text"]


def test_fy_etry_011_the_trust_root_is_fetched_through_the_egress_rules(tmp_path):
    """FY-ETRY-011: Sigstore's TUF repository is read through forgeyard.models.fetch: a host that resolves to a private
    address is refused there (unless the server allows private hosts), and a refresh that fails is cannot_verify, never
    an old or unchecked root."""
    from tuf.api import exceptions as tuf_errors

    private = fetch.Policy(resolver=lambda host, port: ["10.0.0.5"])
    fetcher = signatures._tuf_fetcher(private)
    with pytest.raises(tuf_errors.DownloadError) as e:
        list(fetcher.fetch(f"{signatures.TUF_URL}/timestamp.json"))
    assert "private address" in str(e.value)
    with pytest.raises(signatures.SignatureError) as e:
        signatures.tuf_trusted_root(private, tmp_path)
    assert e.value.code == "cannot_verify" and "trust root" in e.value.text
    # Nothing was trusted from it: no trusted_root.json was written.
    assert not list(tmp_path.rglob("trusted_root.json"))


# ------------------------------------------------------------------ based_on and the catalog's engine arguments
def test_fy_etry_012_based_on_is_checked_and_the_catalogs_engine_arguments_follow_it(owner):
    """FY-ETRY-012: `based_on` names a built-in recipe of the same engine, only in a custom recipe; `copy` only beside
    it. A catalog model's engine arguments for the built-in (Qwen3-Reranker's --hf-overrides for vllm-pooling) apply
    to a copy (vllm-pooling-0-34-0) when rendering its replica; an entry of the copy's own wins, even an empty one."""
    pooling = recipes.builtin_recipes()["vllm-pooling"][1]
    copy = pooling.replace("name: vllm-pooling\n", "name: vllm-pooling-0-34-0\n") + "\nbased_on: vllm-pooling\n"
    r = parse_recipe(copy)
    assert r.based_on == "vllm-pooling"
    for bad, why in ((pooling.replace("name: vllm-pooling\n", "name: x\n") + "\nbased_on: nope\n", "not a built-in"),
                     (pooling.replace("name: vllm-pooling\n", "name: x\n") + "\nbased_on: llama-cpp\n", "runs llama.cpp"),
                     (pooling.replace("name: vllm-pooling\n", "name: x\n") + "\ncopy: {from_version: a, made_at: b, "
                                                                            "made_by: c}\n", "only for a recipe with based_on")):
        with pytest.raises(RecipeError) as e:
            parse_recipe(bad)
        assert why in str(e.value)
    with pytest.raises(RecipeError):
        parse_recipe(pooling + "\nbased_on: vllm\n", builtin=True)
    resp = owner.post("/api/recipes", json={"yaml": pooling.replace("name: vllm-pooling\n", "name: x\n")
                                            + "\nbased_on: llama-cpp\n"})
    assert resp.status_code == 422 and "based_on" in resp.json()["detail"]["message"]
    args = ["--hf-overrides", '{"architectures": ["Qwen3ForSequenceClassification"]}']
    assert recipes.engine_args_for({"vllm-pooling": args}, r) == args
    assert recipes.engine_args_for({"vllm-pooling": args, "vllm-pooling-0-34-0": []}, r) == []
    assert recipes.engine_args_for({"vllm-pooling": args}, recipes.builtin_recipes()["vllm"][0]) == []
    ctx = SimpleNamespace(recipe=r, params={}, model=SimpleNamespace(engine_args={"vllm-pooling": args}, meta={},
                                                                     format="safetensors"))
    out = render.args_for(ctx, r.variants[0], {k: "x" for k in recipes.BUILTIN_PLACEHOLDERS})
    assert out[-2:] == args


# ------------------------------------------------------------------ the image_pins table and every reader of it
def test_fy_etry_013_the_migration_adds_image_pins_and_goes_back(owner):
    """FY-ETRY-013: 0016_image_pins is additive (a new table) and reversible: down to 0015 drops it, up again makes it
    empty, and the server's lookup reloads from it."""
    from alembic import command
    from sqlalchemy import inspect

    from forgeyard.db import get_engine
    from forgeyard.migrate import alembic_config, upgrade

    assert "image_pins" in inspect(get_engine()).get_table_names()
    db = new_session()
    try:
        pins.record(db, "ghcr.io/acme/x:1@" + OLD, {"index": OLD, "platforms": {}}, None, "owner")
        db.commit()
    finally:
        db.close()
    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0015_downloads")
    assert "image_pins" not in inspect(get_engine()).get_table_names()
    upgrade()
    assert "image_pins" in inspect(get_engine()).get_table_names() and pin_rows() == {}


@pytest.fixture()
def img_reg(monkeypatch):
    """A small two-platform image on a fake registry (tests/image_helpers.py), NOT in the committed lock file."""
    from tests.image_helpers import FakeRegistry, build_image

    img = build_image("ghcr.io/acme/engine:2.0")
    server = FakeRegistry(img)
    monkeypatch.setitem(oci.ENDPOINTS, "ghcr.io", server.url)
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "1")
    get_settings.cache_clear()
    assert images.load_lock().get(img.ref) is None
    yield img, server
    server.close()


def _pin(img, signature=None):
    db = new_session()
    try:
        pins.record(db, img.ref, img.lock_entry(), signature, "owner")
        db.commit()
        pins.refresh(db)
    finally:
        db.close()


def test_fy_etry_014_the_server_reads_an_images_ids_as_the_script_does(img_reg):
    """FY-ETRY-014: pinning.describe over the server's registry client (RegistryReader: egress rules, pull token,
    every manifest and config hashed) gives the entry scripts/pin_images.py records; a config whose bytes do not match
    its digest is refused; the platforms a variant needs (amd64, arm64 for aarch64) are checked."""
    img, server = img_reg
    reader = pinning.RegistryReader(oci.Registry(lambda: fetch.Policy(allow_private=True)))
    assert pinning.describe(reader, img.ref) == img.lock_entry()
    assert pinning.missing_platforms(img.lock_entry(), ["x86_64", "aarch64"]) == []
    assert pinning.missing_platforms({"platforms": {"linux/amd64": {}}}, ["x86_64", "aarch64"]) == ["linux/arm64"]
    assert pinning.missing_platforms({"platforms": {}}, None) == ["linux/amd64"]
    server.corrupt.add(img.platforms["amd64"].config_digest)
    with pytest.raises(fetch.FetchError) as e:
        pinning.describe(reader, img.ref)
    assert "hashes to" in str(e.value)
    with pytest.raises(ValueError):
        pinning.describe(reader, "ghcr.io/acme/engine:2.0")


def test_fy_etry_015_a_pinned_copys_image_comes_through_the_server_and_is_checked_after_docker_load(owner, img_reg):
    """FY-ETRY-015: once pinned, a copy's image (not in the lock file) is relayable like a built-in's: a download with
    that engine gets `image_ids` (what an agent checks a `docker load`ed copy by) and `image_relay`; the relay serves
    the recorded manifest of the machine's platform. Without the pin row (the copy deleted): neither."""
    from tests.test_relay_images import IMAGES, engine_recipe, get, hello_on, start
    from tests.machines_helpers import connect, joined
    from tests.test_workload_stream import recv_type

    img, _ = img_reg
    assert not relay.relayable_image(img.ref) and images.check_for(img.ref) is None
    engine_recipe(owner, img.ref, "acme-copy")
    _pin(img)
    assert relay.relayable_image(img.ref) and images.check_for(img.ref) == img.check()
    a = joined(owner, "dock")
    with connect(owner, a["credential"]) as ws:
        hello_on(ws, features=IMAGES)
        d = start(owner, a["node_id"], "acme-copy")
        item = recv_type(ws, "desired")["downloads"][0]
        assert item["image"] == img.ref and item["image_ids"] == img.check()
        assert item["image_relay"]["mode"] == "auto"
        r = get(owner, f"/agent/v1/relay/download/{d['uid']}/image/manifest", a["credential"])
        assert r.status_code == 200 and r.content == img.platforms["amd64"].manifest
    # The replica of a deployment gets the same ids (workloads/state.py reads them through images.check_for too).
    from forgeyard.workloads import state

    assert state.images.check_for is images.check_for
    assert owner.delete("/api/recipes/acme-copy").status_code == 200
    assert pin_rows() == {} and not relay.relayable_image(img.ref) and images.check_for(img.ref) is None


def test_fy_etry_016_a_copys_image_is_listed_present_and_removable_on_machine_pages(owner, img_reg):
    """FY-ETRY-016: with its pin row, a loaded copy of the image (by its content id, whatever its local Id) is matched
    to the copy's recipe ("checked"), counts as present on the engine's page and is in use by a replica that names
    it; the removal precheck takes it as Forgeyard's. Without the row only the tag is left to match."""
    from forgeyard.db import Node

    img, _ = img_reg
    p = img.platforms["amd64"]
    item = {"id": "sha256:" + "9" * 64, "tags": [img.name], "content": p.content, "size": 10}
    from tests.test_relay_images import engine_recipe

    engine_recipe(owner, img.ref, "acme-copy")
    db = new_session()
    try:
        assert upload.recipe_matches(db, item) == []   # pinned by digest, no recorded ids: never matched by tag
        _pin(img)
        assert upload.recipe_matches(db, item) == [{"recipe": "acme-copy", "variant": "cpu", "image": img.ref,
                                                    "how": "checked"}]
        assert engineimages.refers(item, img.ref)
        node = Node(id=1, name="box", runtime_info={"images": {"at": "2026-11-23T00:00:00+00:00", "items": [item]}})
        assert engineimages.image_on(node, img.ref)["present"] is True
        matches, used = engineimages.precheck(db, node, item, {"agent_image": None})
        assert matches[0]["how"] == "checked" and used == []
    finally:
        db.close()


def test_fy_etry_017_pins_are_shared_and_pruned_only_when_no_custom_recipe_names_them(owner, img_reg):
    """FY-ETRY-017: one row per image, shared by copies: deleting one copy keeps the row while another names the image;
    editing the last one to another image (or deleting it) removes the row; the lookup reloads each time. The committed
    lock file always wins over a row of the same reference."""
    img, _ = img_reg
    from tests.test_relay_images import engine_recipe

    engine_recipe(owner, img.ref, "copy-a")
    engine_recipe(owner, img.ref, "copy-b")
    _pin(img, {"status": "unsigned"})
    assert owner.delete("/api/recipes/copy-a").status_code == 200
    assert set(pin_rows()) == {img.ref} and relay.relayable_image(img.ref)
    text = owner.get("/api/recipes/copy-b").json()["recipe"]["yaml"].replace(img.ref, "ghcr.io/acme/engine:2.0")
    assert owner.put("/api/recipes/copy-b", json={"yaml": text}).status_code == 200
    assert pin_rows() == {} and not relay.relayable_image(img.ref)
    builtin = next(iter(images.load_lock()))
    images.set_pinned({builtin: {"index": OLD, "platforms": {}}})
    assert images.entry_for(builtin) == images.load_lock()[builtin]
    assert images.entry_for("ghcr.io/acme/engine:2.0") is None and images.entry_for(None) is None


def test_fy_etry_018_the_pins_load_when_the_server_starts(owner, img_reg):
    """FY-ETRY-018: the rows are read into the lookup when the server starts (a restart keeps a copy's image
    relayable)."""
    from tests.conftest import make_client

    img, _ = img_reg
    _pin(img)
    pins.reset()
    assert not relay.relayable_image(img.ref)
    with make_client():
        assert relay.relayable_image(img.ref)


# ------------------------------------------------------------------ the YAML and one Try at a time
def test_fy_etry_019_every_built_in_can_be_copied_keeping_any_set_of_variants(tmp_path):
    """FY-ETRY-019: the copy's YAML is the built-in's text edited (comments kept), for every built-in recipe and each
    variant kept alone: it parses, keeps exactly that variant with the new image or archive, and every other field
    equals the built-in's (enginetry.check_copy)."""
    for name, (builtin, text) in recipes.builtin_recipes().items():
        for keep in builtin.variants:
            plan = enginetry.Plan(builtin, text, "9.9.9", "release", f"{name}-copy"[:40])
            for v in builtin.variants:
                if v is not keep:
                    plan.left_out.append({"variant": v.name, "code": "no_image", "text": "None here.", "params": {}})
                elif v.runtime == "container":
                    ref = f"{v.image.partition('@')[0].rsplit(':', 1)[0]}:9.9.9@sha256:{'e' * 64}"
                    plan.images[v.name] = ref
                    plan.image_signatures[ref] = {"status": "unsigned"}
                elif v.engine is not None:
                    url = v.engine.url.rsplit("/", 2)[0] + "/b9/x.tar.gz"
                    plan.archives[v.name] = {"url": url, "sha256": "f" * 64, "release": "b9", "archive": "x.tar.gz",
                                             "signature": {"status": "unsigned"}}
                else:
                    plan.left_out.append({"variant": v.name, "code": "installed_command", "text": "x", "params": {}})
            if not plan.images and not plan.archives:
                continue
            out, rec = enginetry.build(plan, made_by="owner", made_at="2026-11-23T00:00:00+00:00")
            assert [v.name for v in rec.variants] == [keep.name], (name, keep.name)
            assert "# " in out.split("\nvariants:")[0] and rec.copy_of.from_version == builtin.version


def test_fy_etry_020_one_try_at_a_time(owner, up):
    """FY-ETRY-020: a second Try while one runs gets 409 try_running (two copies of one name never race)."""
    from forgeyard.api import engine_updates

    check(owner)
    assert engine_updates._trying.acquire(blocking=False)
    try:
        r = try_(owner, "vllm", "0.34.0")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "try_running"
    finally:
        engine_updates._trying.release()
    assert try_(owner, "vllm", "0.34.0").status_code == 201
