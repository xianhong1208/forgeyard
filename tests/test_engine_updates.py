"""Newer versions of the built-in engines (1.5.2, #95 PR 1; FY-EUP-001…020): the tag rule on the real tag lists of
2026-11-23 (rc, nightly and model tags never match), the golden answer of a whole check against a fake registry and
GitHub that serve those lists (vLLM 0.33.0 and 0.34.0 without its CUDA 12.9 variant, a newer stable-diffusion.cpp
build without its process variant, the rest up to date, Forgeyard's own image not checked), GHCR's pages, the token
flow, the process archives of a newer llama.cpp, the release-notes scan, every way a registry or GitHub can fail or
be refused (each engine says so, never a 500), the deadline, and the API (admins check, everyone reads, audited, one
at a time)."""

import time

import pytest

from forgeyard.config import get_settings
from forgeyard.models import engineversions as ev, fetch
from forgeyard.models.recipes import builtin_recipes
from forgeyard.workloads import registry as oci
from tests.conftest import OWNER, add_user, sign_in
from tests.engineupdate_helpers import SD_REPO, FakeUpstream, Image, real_tags, sha, tag_digest

LLAMA = "ggml-org/llama.cpp"


@pytest.fixture()
def up(monkeypatch):
    server = FakeUpstream()
    monkeypatch.setitem(oci.ENDPOINTS, "registry-1.docker.io", server.url)
    monkeypatch.setitem(oci.ENDPOINTS, "ghcr.io", server.url)
    monkeypatch.setattr(ev, "GITHUB_API", server.url)
    yield server
    server.close()


def run(up, **kw):
    return ev.check_all(by="tester", allow_private=True, recipes=kw.pop("recipes", None) or up.recipes(), **kw)


def variants(cand):
    return {v["name"]: v for v in cand["variants"]}


# ------------------------------------------------------------------ the tag rule
def test_fy_eup_001_tag_rule_on_the_real_tag_lists():
    """FY-EUP-001: each built-in variant's tag with its version as the moving part, on the tags the registries
    listed on 2026-11-23: vLLM 0.33.0 and 0.34.0 for cuda and rocm, none past 0.32.0 for -cu129; speaches stops at
    0.8.3 in all three forms (its 0.9.0 rcs never match); llama.cpp's server tags of 1.0.0 (with -b<build>-<sha>)
    and none of its nightlies; vLLM-Omni stops at 0.32.0."""
    tags = real_tags()
    rec = {n: r for n, (r, _) in builtin_recipes().items()}

    def versions(recipe, variant):
        v = rec[recipe].variant(variant)
        rule = ev.tag_rule(v, rec[recipe].version)
        return rule.versions(tags[oci.split_image(v.image)[1]])

    def newer(vs, cur):
        return sorted((x for x in vs if ev.version_key(x) > ev.version_key(cur)), key=ev.version_key)

    assert newer(versions("vllm", "cuda"), "0.32.0") == ["0.33.0", "0.34.0"]
    assert newer(versions("vllm", "rocm"), "0.32.0") == ["0.33.0", "0.34.0"]
    assert newer(versions("vllm", "cuda-12.9"), "0.32.0") == []
    assert max(versions("vllm", "cuda-12.9"), key=ev.version_key) == "0.32.0"
    for variant in ("cuda", "cuda-12.4", "cpu"):
        assert max(versions("faster-whisper", variant), key=ev.version_key) == "0.8.3"
    cuda = versions("llama-cpp", "cuda")
    assert max(cuda, key=ev.version_key) == "1.0.0" and (12359, "server-cuda-v1.0.0-b12359-b1b770d") in cuda["1.0.0"]
    assert max(versions("vllm-omni-image", "cuda"), key=ev.version_key) == "0.32.0"
    rule = ev.tag_rule(rec["vllm"].variant("cuda"), "0.32.0")
    for t in ("v0.33.0rc1", "v0.32.0rc2", "nightly-2b1a5f3", "v0.33.0-aarch64", "v0.34.0-x86_64", "latest",
              "v0.32.0-cu129", "kimi-k31-mc-v0.33.0"):
        assert rule.match(t) is None, t
    assert ev.tag_rule(rec["faster-whisper"].variant("cpu"), "0.8.3").match("0.9.0-rc.3-cpu") is None
    lrule = ev.tag_rule(rec["llama-cpp"].variant("vulkan"), "1.0.0")
    assert lrule.match("server-vulkan-b12648") is None and lrule.match("full-vulkan-v1.0.0-b12359-b1b770d") is None
    assert lrule.match("server-vulkan-v1.1.0") == ("1.1.0", None)
    assert lrule.tag_for("1.1.0") == "server-vulkan-v1.1.0"
    assert ev.tag_rule(rec["sd-cpp"].variant("cuda"), rec["sd-cpp"].version) is None   # rolling: no version
    for tag in ("v10.32.0", "v0.32.0.1", "v0.32.01"):          # the version only as a whole number
        with pytest.raises(ValueError):
            ev.TagRule(tag, "0.32.0")
    assert ev.tag_rule(rec["sd-cpp"].variant("cpu-process"), rec["sd-cpp"].version) is None


def test_fy_eup_002_pick_tag_prefers_the_built_ins_form():
    """FY-EUP-002: of the tags of one version, the one in the built-in's form wins (llama.cpp's with its build,
    the highest build); a version found only without a build still counts."""
    assert ev.pick_tag([(None, "server-v1.1.0"), (12700, "server-v1.1.0-b12700-abcdef1")], True) == \
        (12700, "server-v1.1.0-b12700-abcdef1")
    assert ev.pick_tag([(12699, "a-b12699-1234567"), (12700, "a-b12700-abcdef1")], True)[0] == 12700
    assert ev.pick_tag([(None, "server-v1.1.0")], True) == (None, "server-v1.1.0")
    assert ev.pick_tag([(None, "v0.34.0")], False) == (None, "v0.34.0")


# ------------------------------------------------------------------ the golden answer
def test_fy_eup_003_a_check_finds_what_the_registries_held_on_2026_11_23(up):
    """FY-EUP-003: the golden table: vLLM (and vllm-pooling) 0.34.0 and 0.33.0, newest first, each on cuda and rocm
    with the digest the registry gives now, cuda-12.9 left out because its images stop at 0.32.0; the 0.33.0 notes
    link is GitHub's v0.33.0rc3 page (named v0.33.0); stable-diffusion.cpp a newer build master-1117-0d71f96 on
    cuda and vulkan, its process variant left out (the release's Linux archive is built for Ubuntu 26.04);
    llama.cpp, speaches and vLLM-Omni up to date (Omni's GitHub v0.34.0 has no image: said, not offered); the
    vision image not checked."""
    r = run(up)
    e = r["engines"]
    assert {n: x["status"] for n, x in e.items()} == {
        "faster-whisper": "up_to_date", "llama-cpp": "up_to_date", "sd-cpp": "newer_build",
        "speaches-tts": "up_to_date", "vision": "not_checked", "vllm": "newer", "vllm-omni-image": "up_to_date",
        "vllm-omni-tts": "up_to_date", "vllm-omni-video": "up_to_date", "vllm-pooling": "newer"}
    for name in ("vllm", "vllm-pooling"):
        cands = e[name]["candidates"]
        assert [c["version"] for c in cands] == ["0.34.0", "0.33.0"]
        for c in cands:
            vs = variants(c)
            tag = f"v{c['version']}"
            assert vs["cuda"]["available"] and vs["cuda"]["tag"] == tag
            assert vs["cuda"]["image"] == f"vllm/vllm-openai:{tag}@{tag_digest('vllm/vllm-openai', tag)}"
            assert vs["rocm"]["image"] == f"vllm/vllm-openai-rocm:{tag}@{tag_digest('vllm/vllm-openai-rocm', tag)}"
            miss = vs["cuda-12.9"]
            assert not miss["available"] and miss["why"]["code"] == "no_image"
            assert miss["why"]["tag"] == f"{tag}-cu129" and miss["why"]["last"] == "0.32.0"
            assert list(vs) == ["cuda", "cuda-12.9", "rocm"]
        assert cands[1]["release"]["url"] == "https://github.com/vllm-project/vllm/releases/tag/v0.33.0rc3"
        assert cands[0]["release"]["url"] == "https://github.com/vllm-project/vllm/releases/tag/v0.34.0"
    flags = {m["flag"] for m in e["vllm"]["candidates"][0]["mentions"]}
    assert "--gpu-memory-utilization" in flags and "--dtype" not in flags
    sd = e["sd-cpp"]["candidates"]
    assert [c["version"] for c in sd] == ["master-1117-0d71f96"] and sd[0]["kind"] == "build"
    vs = variants(sd[0])
    assert vs["cuda"]["available"] and vs["vulkan"]["available"]
    assert vs["cuda"]["image"].startswith("ghcr.io/leejet/stable-diffusion.cpp:master-cuda@sha256:")
    assert vs["cpu-process"]["why"]["code"] == "no_archive"
    assert vs["cpu-process"]["why"]["similar"] == ["sd-master-0d71f96-bin-Linux-Ubuntu-26.04-x86_64.zip"]
    assert "Ubuntu-24.04" in vs["cpu-process"]["why"]["archive"]
    for name in ("vllm-omni-image", "vllm-omni-tts", "vllm-omni-video"):
        assert e[name]["upstream"]["code"] == "release_without_image" and e[name]["upstream"]["version"] == "0.34.0"
        assert e[name]["newest"] == "0.32.0" and not e[name]["candidates"]
    assert e["faster-whisper"]["newest"] == "0.8.3" and "upstream" not in e["faster-whisper"]
    assert e["llama-cpp"]["newest"] == "1.0.0"
    assert e["vision"]["why"]["code"] == "own_image"
    assert not any(x["errors"] for x in e.values())
    assert r["checked_by"] == "tester" and r["github_requests"] <= ev.GITHUB_MAX_CALLS
    # One tag listing per repository, however many recipes share it; only HEADs for tags (no pulls counted).
    assert up.count("/v2/vllm/vllm-openai/tags/list") == 1
    assert up.count("/v2/vllm/vllm-openai/manifests/") == 0
    assert up.count("/v2/vllm/vllm-openai/manifests/", "HEAD") == 2


def test_fy_eup_004_ghcr_pages_are_all_read_and_a_newer_release_on_the_last_one_is_found(up):
    """FY-EUP-004: llama.cpp's 13,683 tags at 1,000 a page are 14 pages, all read; a 1.1.0 release published only
    for cuda, cpu and vulkan (as -b12700-<sha>), on the last page, is offered: its rocm variant is named as missing,
    and the CPU process variant takes b12700's archive with GitHub's SHA-256 (Q5 A)."""
    new = ["server-cuda-v1.1.0-b12700-abcdef1", "server-v1.1.0-b12700-abcdef1", "server-vulkan-v1.1.0-b12700-abcdef1",
           "server-cuda-b12701", "server-v1.1.0rc1-b12650-1234567"]
    up.tags[LLAMA] = up.tags[LLAMA] + new
    digest = "sha256:" + "ab" * 32
    up.releases[LLAMA].append({"tag_name": "b12700", "name": "b12700", "draft": False, "prerelease": False,
                               "html_url": "https://github.com/ggml-org/llama.cpp/releases/tag/b12700", "body": "",
                               "assets": [{"name": "llama-b12700-bin-ubuntu-x64.tar.gz", "digest": digest,
                                           "browser_download_url": "https://github.com/ggml-org/llama.cpp/releases/"
                                                                   "download/b12700/llama-b12700-bin-ubuntu-x64.tar.gz"}]})
    up.releases[LLAMA].append({"tag_name": "v1.1.0", "name": "v1.1.0", "draft": False, "prerelease": False,
                               "html_url": "https://github.com/ggml-org/llama.cpp/releases/tag/v1.1.0",
                               "body": "Removed `--no-mmap`; `--kv-unified` is now the default. -ngl unchanged.",
                               "assets": []})
    e = run(up)["engines"]["llama-cpp"]
    assert up.count(f"/v2/{LLAMA}/tags/list") == 14
    assert e["status"] == "newer" and [c["version"] for c in e["candidates"]] == ["1.1.0"]
    vs = variants(e["candidates"][0])
    assert vs["cuda"]["tag"] == "server-cuda-v1.1.0-b12700-abcdef1" and vs["vulkan"]["available"]
    assert vs["rocm"]["why"] == {"code": "no_image", "tag": "server-rocm-v1.1.0", "last": "1.0.0",
                                 "repo": "ghcr.io/ggml-org/llama.cpp",
                                 "text": "No server-rocm-v1.1.0 image in ghcr.io/ggml-org/llama.cpp; this variant's "
                                         "images stop at 1.0.0."}
    p = vs["cpu-process"]
    assert p["available"] and p["sha256"] == "ab" * 32 and p["release"] == "b12700"
    assert p["url"].endswith("/download/b12700/llama-b12700-bin-ubuntu-x64.tar.gz")
    assert e["candidates"][0]["release"]["tag"] == "v1.1.0"
    assert [m["flag"] for m in e["candidates"][0]["mentions"]] == ["--kv-unified"]   # not --no-mmap: not passed


def test_fy_eup_005_too_many_pages_and_a_next_page_elsewhere_are_errors_not_short_lists(up, monkeypatch):
    """FY-EUP-005: past TAGS_MAX_PAGES a listing is an error (a version could hide on the page not read), and a
    `Link` to another host is refused; either way only the engines of that repository say "could not check"."""
    monkeypatch.setattr(oci, "TAGS_MAX_PAGES", 5)
    e = run(up)["engines"]
    assert e["llama-cpp"]["status"] == "could_not_check"
    assert "more than 5 pages" in e["llama-cpp"]["errors"][0]["text"]
    assert e["llama-cpp"]["errors"][0]["where"] == "ghcr.io/ggml-org/llama.cpp"
    assert e["vllm"]["status"] == "newer"
    monkeypatch.setattr(oci, "TAGS_MAX_PAGES", 30)
    up.next_link = '<https://elsewhere.example/v2/x/tags/list?n=1000>; rel="next"'
    e = run(up)["engines"]
    assert "points at another host" in e["llama-cpp"]["errors"][0]["text"]


def test_fy_eup_006_the_token_flow_and_a_registry_without_a_digest_header(up):
    """FY-EUP-006: the anonymous Bearer flow (401, token, again), one token per repository for the whole check; a
    registry that sends no Docker-Content-Digest gets a GET and the bytes are hashed."""
    run(up)
    assert up.tokens == len({"vllm/vllm-openai", "vllm/vllm-openai-rocm", "vllm/vllm-omni", "speaches-ai/speaches",
                             LLAMA, SD_REPO})
    assert all(r["auth"] for r in up.requests if r["path"].startswith("/v2/") and r["path"] != "/v2/")
    up.no_digest_header = True
    reg = oci.Registry(lambda: fetch.Policy(allow_private=True))
    assert reg.resolve("ghcr.io", "vllm/vllm-openai", "v0.34.0") == sha(b'{"schemaVersion":2}')
    with pytest.raises(oci.RegistryError, match="not a tag"):
        reg.resolve("ghcr.io", "vllm/vllm-openai", "../x")


# ------------------------------------------------------------------ rolling tags
def test_fy_eup_007_rolling_tags_unmoved_older_and_split(up):
    """FY-EUP-007: a rolling tag still at the pin is up to date; one moved to an image made before the pin is not
    newer; when only one variant's tag moved, the other is named as not moved."""
    for tag in ("master-cuda", "master-vulkan"):
        up.digests[(SD_REPO, tag)] = next(f for r, f in up.sd_pins.items() if (tag == "master-cuda") ==
                                          r.startswith("sha256:34f7"))
    e = run(up)["engines"]["sd-cpp"]
    assert e["status"] == "up_to_date" and e["candidates"] == []
    old = Image("aaaaaaa000", "2026-11-01T00:00:00Z")
    up.add_image(SD_REPO, "master-cuda", old)
    assert run(up)["engines"]["sd-cpp"]["status"] == "up_to_date"
    up.add_image(SD_REPO, "master-cuda", Image("0d71f963af9c94941f4a67e8ea145935651dd756", "2026-11-22T18:45:07Z"))
    e = run(up)["engines"]["sd-cpp"]
    vs = variants(e["candidates"][0])
    assert vs["cuda"]["available"] and vs["vulkan"]["why"]["code"] == "not_moved"


# ------------------------------------------------------------------ process variants
def test_fy_eup_008_process_variants_need_the_same_archive_with_a_sha256(up):
    """FY-EUP-008: a process variant follows only when the newer release publishes the built-in's archive with a
    SHA-256 (Q5 A): no such release, an archive without a digest, an installed command and a non-GitHub archive
    are each left out with their reason."""
    rec = {n: r for n, (r, _) in builtin_recipes().items()}
    v = rec["llama-cpp"].variant("cpu-process")
    ck = ev.Checker(allow_private=True)
    assert ev.process_variant(ck, v, "b99999")["why"]["code"] == "no_release"
    up.releases[LLAMA].append({"tag_name": "b12800", "name": "b12800", "draft": False, "html_url": "",
                               "assets": [{"name": "llama-b12800-bin-ubuntu-x64.tar.gz", "digest": None,
                                           "browser_download_url": "https://github.com/ggml-org/llama.cpp/x"}]})
    assert ev.process_variant(ck, v, "b12800")["why"]["code"] == "no_sha256"
    # A digest, but a download address outside the engine's own repository: not taken.
    up.releases[LLAMA].append({"tag_name": "b12801", "name": "b12801", "draft": False, "html_url": "",
                               "assets": [{"name": "llama-b12801-bin-ubuntu-x64.tar.gz", "digest": "sha256:" + "cd" * 32,
                                           "browser_download_url": "https://github.com/someone-else/llama.cpp/x"}]})
    assert ev.process_variant(ck, v, "b12801")["why"]["code"] == "no_sha256"
    assert ev.process_variant(ck, v, None)["why"]["code"] == "release_unknown"
    assert ev.process_variant(ck, v, "b12359") == {
        "name": "cpu-process", "runtime": "process", "vendor": "cpu", "available": True, "release": "b12359",
        "url": v.engine.url, "sha256": v.engine.sha256, "archive": "llama-b12359-bin-ubuntu-x64.tar.gz"}
    cmd = v.model_copy(update={"engine": None, "command": "/usr/bin/llama-server"})
    assert ev.process_variant(ck, cmd, "b1")["why"]["code"] == "installed_command"
    other = v.model_copy(update={"engine": v.engine.model_copy(update={"url": "https://example.org/x.tar.gz"})})
    assert ev.process_variant(ck, other, "b1")["why"]["code"] == "not_github"
    assert ev._swap_ids("sd-master-3be2378-bin-Linux-Ubuntu-24.04-x86_64.zip", "master-1043-3be2378",
                        "master-1117-0d71f96") == "sd-master-0d71f96-bin-Linux-Ubuntu-24.04-x86_64.zip"


# ------------------------------------------------------------------ release notes (Q6 A)
def test_fy_eup_009_release_notes_scan():
    """FY-EUP-009: the recipe's long flags as written or (two words or more) in snake_case, and its environment
    variables, with up to three lines each; a one-word flag only with its dashes; not inside a longer name."""
    notes = ("Set `gpu_memory_utilization` only to share a GPU.\n"
             "quantization got faster; --dtype auto is the default\n"
             "Opt out with --gpu-memory-utilization 0.9\n"
             "--gpu-memory-utilization-extra is new; so is VLLM_API_KEY_FILE\n"
             "VLLM_API_KEY still works\n"
             + "x" * 400 + " --max-model-len " + "y" * 400 + "\n"
             + "--max-num-seqs\n--max-num-seqs\n--max-num-seqs\n--max-num-seqs\n")
    got = {m["flag"]: m for m in ev.flag_mentions(notes, ["--gpu-memory-utilization", "--dtype", "--quantization",
                                                          "--max-model-len", "--max-num-seqs", "--port"],
                                                  ["VLLM_API_KEY", "HF_HUB_OFFLINE"])}
    assert set(got) == {"--gpu-memory-utilization", "--dtype", "--max-model-len", "--max-num-seqs", "VLLM_API_KEY"}
    assert got["--gpu-memory-utilization"]["lines"] == 2
    assert got["VLLM_API_KEY"]["lines"] == 1
    assert got["--max-num-seqs"]["lines"] == 4 and len(got["--max-num-seqs"]["excerpts"]) == 1   # same line once
    ex = got["--max-model-len"]["excerpts"][0]
    assert "--max-model-len" in ex and len(ex) <= ev.EXCERPT_CHARS + 2 and ex.startswith("…") and ex.endswith("…")
    flags, envs = ev.recipe_flags(next(r for n, (r, _) in builtin_recipes().items() if n == "vllm"))
    assert "--gpu-memory-utilization" in flags and "--max-num-batched-tokens" in flags and "--quantization" in flags
    assert "VLLM_API_KEY" in envs and "VLLM_CACHE_ROOT" in envs and all(f.startswith("--") for f in flags)
    assert ev.flag_mentions("", flags, envs) == []


# ------------------------------------------------------------------ failures
def test_fy_eup_010_one_registry_failing_leaves_the_others_reported(up):
    """FY-EUP-010: Docker Hub answering 500 for vLLM's tag list makes vLLM and vllm-pooling "could not check"
    (the rocm repository still answers, so "partly"), never an exception; GHCR's engines are unaffected."""
    up.fail["/v2/vllm/vllm-openai/tags"] = 500
    e = run(up)["engines"]
    assert e["vllm"]["errors"] == [{"where": "docker.io/vllm/vllm-openai",
                                    "text": "registry-1.docker.io answered HTTP 500 for the tags of vllm/vllm-openai"}]
    vs = variants(e["vllm"]["candidates"][0])
    assert vs["cuda"]["why"]["code"] == "registry_error" and vs["rocm"]["available"]
    up.fail["/v2/vllm/vllm-openai-rocm/tags"] = 500
    e = run(up)["engines"]
    assert e["vllm"]["status"] == "could_not_check" and e["vllm-pooling"]["status"] == "could_not_check"
    assert e["sd-cpp"]["status"] == "newer_build" and e["llama-cpp"]["status"] == "up_to_date"


def test_fy_eup_011_egress_rules_refuse_and_every_engine_says_so(up, monkeypatch):
    """FY-EUP-011: with private addresses not allowed (the default), the loopback fake is refused by the egress
    rules: every engine that was to be checked says "could not check" with the reason; none is skipped quietly."""
    r = ev.check_all(by="tester", allow_private=False, recipes=up.recipes())
    for name, e in r["engines"].items():
        if name == "vision":
            continue
        assert e["status"] == "could_not_check", name
        assert "plain http is refused" in e["errors"][0]["text"]
    assert up.requests == []
    # The real registry's name resolving to a private address (DNS rebinding, a hostile resolver): refused before
    # anything is sent.
    monkeypatch.delitem(oci.ENDPOINTS, "registry-1.docker.io")
    blocked = ev.check_all(by="t", allow_private=False, recipes={"vllm": up.recipes()["vllm"]},
                           resolver=lambda host, port: ["10.0.0.7"])
    assert "private address" in blocked["engines"]["vllm"]["errors"][0]["text"]


def test_fy_eup_012_github_failing_keeps_the_images_and_says_why(up):
    """FY-EUP-012: GitHub's anonymous limit (403) or a GitHub that cannot be reached: the image candidates stay,
    with the notes marked as not read (and why); the process variant says GitHub could not be asked."""
    up.fail["/repos/"] = 403
    e = run(up)["engines"]
    c = e["vllm"]["candidates"][0]
    assert c["version"] == "0.34.0" and c["notes"]["code"] == "github_error"
    assert "60 an hour" in c["notes"]["text"] and "mentions" not in c and "release" not in c
    assert e["vllm-omni-image"]["status"] == "up_to_date" and "upstream" not in e["vllm-omni-image"]
    del up.fail["/repos/"]
    up.releases["vllm-project/vllm"] = []
    c = run(up)["engines"]["vllm"]["candidates"][0]
    assert c["notes"]["code"] == "no_notes"


def test_fy_eup_013_the_deadline_bounds_the_whole_check(up):
    """FY-EUP-013: a registry that answers too slowly is cut by the check's deadline: every engine still gets its
    entry ("could not check: …stopped…" or a timeout), and the check ends close to its limit."""
    up.delay = 1.2
    t0 = time.monotonic()
    r = run(up, seconds=1.0)
    assert time.monotonic() - t0 < 8
    texts = " ".join(x["text"] for e in r["engines"].values() for x in e["errors"])
    assert "stopped after 1 s" in texts or "timed out" in texts
    assert all(e["status"] in ("could_not_check", "not_checked") for e in r["engines"].values())
    # A check whose time is up asks nothing more.
    up.delay = 0
    up.requests.clear()
    r = run(up, seconds=0)
    assert up.requests == []
    assert all("stopped after 0 s" in e["errors"][0]["text"] for n, e in r["engines"].items() if n != "vision")


def test_fy_eup_014_github_budget_is_bounded(up, monkeypatch):
    """FY-EUP-014: no more than GITHUB_MAX_CALLS GitHub requests in one check; past it, notes are "not read"."""
    monkeypatch.setattr(ev, "GITHUB_MAX_CALLS", 1)
    r = run(up)
    assert r["github_requests"] == 1 and up.count("/repos/") == 1
    notes = [c.get("notes", {}).get("code") for e in r["engines"].values() for c in e["candidates"]]
    assert "github_error" in notes


def test_fy_eup_015_an_unexpected_error_in_one_engine_is_its_own(up, monkeypatch):
    """FY-EUP-015: a bug in one engine's check is reported as that engine's error, not a failed check."""
    real = ev.check_recipe

    def boom(ck, recipe):
        if recipe.name == "speaches-tts":
            raise KeyError("surprise")
        return real(ck, recipe)

    monkeypatch.setattr(ev, "check_recipe", boom)
    e = run(up)["engines"]
    assert e["speaches-tts"]["status"] == "could_not_check" and "surprise" in e["speaches-tts"]["errors"][0]["text"]
    assert e["vllm"]["status"] == "newer"


# ------------------------------------------------------------------ the API
@pytest.fixture()
def api_up(up, monkeypatch):
    monkeypatch.setenv("MODEL_ALLOW_PRIVATE_HOSTS", "1")
    get_settings.cache_clear()
    recs = up.recipes()
    monkeypatch.setattr(ev, "builtin_recipes", lambda: {n: (r, "") for n, r in recs.items()})
    yield up
    get_settings.cache_clear()


def test_fy_eup_016_admins_check_everyone_reads_the_last_result(owner, client, api_up):
    """FY-EUP-016: before any check the result is null; an operator or user may read but not check (403); an
    admin's check is stored (one server_settings row) with when and by whom, and read back by everyone signed in;
    no data shape change (no migration)."""
    from forgeyard.db import ServerSetting, new_session

    assert owner.get("/api/engine-updates").json() == {"result": None, "can_check": True}
    add_user(owner, "olga", role="operator")
    r = owner.post("/api/engine-updates/check")
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert res["checked_by"] == OWNER[0] and res["checked_at"]
    assert [c["version"] for c in res["engines"]["vllm"]["candidates"]] == ["0.34.0", "0.33.0"]
    db = new_session()
    try:
        row = db.get(ServerSetting, ev.SETTING_KEY)
        assert row.value["engines"]["sd-cpp"]["status"] == "newer_build" and row.updated_by == res["checked_by"]
    finally:
        db.close()
    op = sign_in(owner, "olga", "quiet-meadow-4821")
    got = op.get("/api/engine-updates").json()
    assert got["can_check"] is False and got["result"]["checked_at"] == res["checked_at"]
    assert op.post("/api/engine-updates/check").status_code == 403
    client.cookies.clear()
    assert client.get("/api/engine-updates").status_code == 401


def test_fy_eup_017_the_check_is_audited_and_runs_one_at_a_time(owner, api_up, monkeypatch):
    """FY-EUP-017: each check is audited (recipe.updates_checked: the newer versions per engine and the seconds);
    a second check while one runs gets 409 check_running."""
    from forgeyard.api import engine_updates

    assert owner.post("/api/engine-updates/check").status_code == 200
    rows = owner.get("/api/audit?action=recipe.").json()["events"]
    hit = next(x for x in rows if x["action"] == "recipe.updates_checked")
    assert hit["actor"] == OWNER[0]
    assert hit["detail"]["newer"]["vllm"] == ["0.34.0", "0.33.0"]
    assert hit["detail"]["newer"]["sd-cpp"] == ["master-1117-0d71f96"] and "seconds" in hit["detail"]
    assert engine_updates._running.acquire(blocking=False)
    try:
        r = owner.post("/api/engine-updates/check")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "check_running"
    finally:
        engine_updates._running.release()


def test_fy_eup_018_a_blocked_registry_is_a_200_with_reasons_never_a_500(owner, up, monkeypatch):
    """FY-EUP-018: through the API with the server's default egress rules (no private hosts), the fake loopback
    upstream is refused: the answer is 200 and each engine says it could not check, and the result is stored."""
    monkeypatch.delenv("MODEL_ALLOW_PRIVATE_HOSTS", raising=False)
    get_settings.cache_clear()
    recs = up.recipes()
    monkeypatch.setattr(ev, "builtin_recipes", lambda: {n: (r, "") for n, r in recs.items()})
    r = owner.post("/api/engine-updates/check")
    assert r.status_code == 200
    assert r.json()["result"]["engines"]["vllm"]["status"] == "could_not_check"
    assert owner.get("/api/engine-updates").json()["result"]["engines"]["vllm"]["status"] == "could_not_check"


def test_fy_eup_019_the_check_asks_only_each_variants_own_repository(up):
    """FY-EUP-019: the requests a check makes go only to the built-in variants' own repositories (and GitHub's
    releases of their engines): no other image is ever looked at, and the API takes no image name at all."""
    run(up)
    recs = {n: r for n, (r, _) in builtin_recipes().items()}
    own = {oci.split_image(v.image)[1] for r in recs.values() if r.engine != "forgeyard-vision"
           for v in r.variants if v.image}
    asked = {r["path"].removeprefix("/v2/").split("/tags/")[0].split("/manifests/")[0].split("/blobs/")[0]
             for r in up.requests if r["path"].startswith("/v2/") and r["path"] != "/v2/"}
    assert asked <= own and "tonyhuang1101/forgeyard-vision" not in asked
    gh = {"/".join(r["path"].split("/")[2:4]) for r in up.requests if r["path"].startswith("/repos/")}
    assert gh <= set(ev.GITHUB_REPOS.values())


def test_fy_eup_020_two_recipes_on_one_repository_share_answers_when_checked_together(up):
    """FY-EUP-020: vllm and vllm-pooling (and the three vLLM-Omni recipes) checked at once ask each tag and
    release once: the second engine waits for the first one's answer instead of asking again."""
    run(up)
    heads = [r["path"] for r in up.requests if r["method"] == "HEAD"]
    assert len(heads) == len(set(heads))
    lists = [r["path"] for r in up.requests if r["path"].endswith("/releases")]
    assert len(lists) == len(set(lists))


def test_fy_eup_021_the_audit_summary():
    """FY-EUP-021: what the audit log keeps of a check: the newer versions per engine, the engines that could not
    be checked, the seconds (absent parts left out)."""
    assert ev.summary({"engines": {"a": {"candidates": [{"version": "2"}], "status": "newer"},
                                   "b": {"candidates": [], "status": "could_not_check"}}, "seconds": 3.0}) == \
        {"newer": {"a": ["2"]}, "could_not_check": ["b"], "seconds": 3.0}
    assert ev.summary({"engines": {}}) == {"newer": None, "could_not_check": None, "seconds": None}


def test_fy_eup_022_github_pre_releases_and_drafts_are_not_versions(up):
    """FY-EUP-022: a GitHub pre-release or draft with a plain version tag is neither the release of a version nor a
    "release without image"; a version whose every digest failed is still listed, with the reasons (never hidden as
    up to date)."""
    omni = "vllm-project/vllm-omni"
    up.releases[omni] = [{"tag_name": "v0.36.0", "name": "v0.36.0", "draft": True, "prerelease": False, "html_url": "",
                          "body": "", "assets": []},
                         {"tag_name": "v0.35.0", "name": "v0.35.0", "draft": False, "prerelease": True, "html_url": "",
                          "body": "", "assets": []}] + up.releases[omni]
    e = run(up)["engines"]
    assert e["vllm-omni-image"]["upstream"]["version"] == "0.34.0"
    up.fail["/v2/vllm/vllm-openai/manifests/"] = 500
    up.fail["/v2/vllm/vllm-openai-rocm/manifests/"] = 500
    e = run(up)["engines"]["vllm"]
    assert e["status"] == "newer" and [c["version"] for c in e["candidates"]] == ["0.34.0", "0.33.0"]
    codes = {v["name"]: v["why"]["code"] for v in e["candidates"][0]["variants"]}
    assert codes == {"cuda": "digest_failed", "cuda-12.9": "no_image", "rocm": "digest_failed"}
