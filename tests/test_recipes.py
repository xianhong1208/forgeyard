"""Recipes: loading, validation and choosing a variant per machine (FY-REC-*)."""

import re

import pytest

from forgeyard.models import recipes
from forgeyard.models.recipes import RecipeError, parse_recipe, render_text, variant_matches, version_ok

A100 = {"vendor": "nvidia", "arch": "8.0", "driver": "580.178.04"}
RTX4090_OLD = {"vendor": "nvidia", "arch": "8.9", "driver": "550.54.14"}
GTX = {"vendor": "nvidia", "arch": "6.1", "driver": "580.1"}
GFX1151 = {"vendor": "amd", "arch": "gfx1151", "driver": "6.14.0"}
GFX1030 = {"vendor": "amd", "arch": "gfx1030", "driver": "6.14.0"}
CPU_X86 = {"vendor": "cpu", "arch": "x86_64", "driver": None}
CPU_ARM = {"vendor": "cpu", "arch": "aarch64", "driver": None}

MINIMAL = """
name: tiny
title: Tiny
engine: tiny
version: "1"
tasks: [chat]
formats: [gguf]
api: {chat: openai-chat}
estimate: {method: fixed, vram: 1GiB}
args: [--port, "{port}"]
variants:
  - name: cpu
    when: {vendor: cpu}
    runtime: container
    image: example/tiny:1
"""


def first_variant(name, accel):
    r = recipes.builtin_recipes()[name][0]
    return next((v.name for v in r.variants if variant_matches(v, accel) is None), None)


def test_fy_rec_001_builtins_load_and_are_pinned():
    """FY-REC-001: four recipes, every image pinned to a version (never latest), engines by SHA-256."""
    rs = recipes.builtin_recipes()
    assert sorted(rs) == ["faster-whisper", "llama-cpp", "vllm", "vllm-pooling"]
    for r, _ in rs.values():
        assert r.builtin
        for v in r.variants:
            if v.image:
                tag = v.image.split("@")[0]
                assert ":" in tag.split("/")[-1] and not tag.endswith(":latest"), v.image
            if v.engine:
                assert len(v.engine.sha256) == 64
    assert rs["vllm"][0].variant("cuda").image.startswith("vllm/vllm-openai:v0.32.0@sha256:")
    assert rs["vllm-pooling"][0].share == "vram" and rs["faster-whisper"][0].upstream_model == "repo"


def test_fy_rec_002_variant_per_vendor_arch_and_driver():
    """FY-REC-002"""
    assert first_variant("vllm", A100) == "cuda"
    assert first_variant("vllm", {**A100, "driver": "575.51"}) == "cuda-12.9"
    assert first_variant("vllm", RTX4090_OLD) is None          # driver 550 is too old for vLLM 0.32
    assert first_variant("vllm", GTX) is None                  # compute capability 6.1 < 7.0
    assert first_variant("vllm", GFX1151) == "rocm"
    assert first_variant("vllm", GFX1030) is None              # not built for gfx1030
    assert first_variant("llama-cpp", GFX1030) == "rocm"       # llama.cpp ROCm image: any gfx
    assert first_variant("llama-cpp", RTX4090_OLD) is None
    assert first_variant("faster-whisper", RTX4090_OLD) == "cuda-12.4"
    assert first_variant("faster-whisper", A100) == "cuda"
    assert first_variant("llama-cpp", CPU_X86) == "cpu"
    r = recipes.builtin_recipes()["llama-cpp"][0]
    assert [v.name for v in r.variants if variant_matches(v, CPU_X86) is None] == ["cpu", "cpu-process"]
    assert [v.name for v in r.variants if variant_matches(v, CPU_ARM) is None] == ["cpu"]
    assert "driver 550.54.14 is not >=580" in variant_matches(recipes.builtin_recipes()["vllm"][0].variants[0], RTX4090_OLD)


def test_fy_rec_003_version_comparisons():
    """FY-REC-003"""
    assert version_ok("580.95.05", ">=580") and version_ok("8.0", ">=7.0") and version_ok("8.9", ">8.6")
    assert not version_ok("579.1", ">=580") and not version_ok(None, ">=580") and not version_ok("x", ">=1")
    assert version_ok("anything", None) and version_ok("12.4", "<13") and version_ok("8.0", "==8")


@pytest.mark.parametrize("change,why", [
    (("args: [--port, \"{port}\"]", "args: [--x, \"{nope}\"]"), "unknown placeholder"),
    (("    image: example/tiny:1\n", "    engine: null\n"), "needs an image"),
    (("    runtime: container\n    image: example/tiny:1\n", "    runtime: process\n"), "either an engine"),
    (("api: {chat: openai-chat}", "api: {}"), "say which protocol"),
    (("version: \"1\"\n", "version: \"1\"\nenv: {CUDA_VISIBLE_DEVICES: \"0\"}\n"), "cannot be set"),
    (("version: \"1\"\n", "version: \"1\"\nenv: {FORGEYARD_X: \"0\"}\n"), "cannot be set"),
    (("name: tiny", "name: Tiny!"), "name"),
    (("estimate: {method: fixed, vram: 1GiB}", "estimate: {method: fixed}"), "needs vram"),
    (("version: \"1\"\n", "version: \"1\"\nparams: {gpus: {type: int, default: 1}}\n"), "reserved"),
    (("version: \"1\"\n", "version: \"1\"\nparams: {ctx: {type: int, default: 5, min: 10}}\n"), "default is not valid"),
])
def test_fy_rec_004_invalid_recipes_are_refused(change, why):
    """FY-REC-004"""
    text = MINIMAL.replace(*change)
    with pytest.raises(RecipeError, match=why):
        parse_recipe(text)


def test_fy_rec_005_engines_yaml_and_size():
    """FY-REC-005: process engines must be https and pinned; broken YAML and huge recipes are refused."""
    proc = MINIMAL.replace("    runtime: container\n    image: example/tiny:1\n",
                           "    runtime: process\n    engine: {url: %s, sha256: %s, binary: %s}\n")
    ok = proc % ("https://e.example/t.tgz", "a" * 64, "t/server")
    assert parse_recipe(ok).variants[0].engine.binary == "t/server"
    for url, sha, binary, why in [("http://e.example/t.tgz", "a" * 64, "t/s", "https"),
                                  ("https://e.example/t.tgz", "abc", "t/s", "64 hex"),
                                  ("https://e.example/t.tgz", "a" * 64, "../s", "relative path")]:
        with pytest.raises(RecipeError, match=why):
            parse_recipe(proc % (url, sha, binary))
    installed = MINIMAL.replace("    runtime: container\n    image: example/tiny:1\n",
                                "    runtime: process\n    command: /opt/tiny/bin/serve\n")
    assert parse_recipe(installed).variants[0].command == "/opt/tiny/bin/serve"
    with pytest.raises(RecipeError, match="absolute"):
        parse_recipe(installed.replace("/opt/tiny/bin/serve", "serve"))
    with pytest.raises(RecipeError, match="not valid YAML"):
        parse_recipe("name: [")
    with pytest.raises(RecipeError, match="64 KB"):
        parse_recipe(MINIMAL + "#" * 70000)


def test_fy_rec_006_placeholders_leave_json_and_jinja_alone():
    """FY-REC-006: only {known_names} are filled; JSON for --hf-overrides and Jinja survive."""
    vals = {"port": 8000, "served_name": "q"}
    assert render_text("{port}", vals) == "8000"
    j = '{"architectures": ["Qwen3ForSequenceClassification"], "is_original_qwen3_reranker": true}'
    assert render_text(j, vals) == j
    assert render_text("{{ messages }} {served_name} {unknown}", vals) == "{{ messages }} q {unknown}"


def test_fy_rec_007_params_are_checked():
    """FY-REC-007"""
    r = recipes.builtin_recipes()["vllm"][0]
    assert r.params["context"].coerce("context", "4096") == 4096
    for value, why in [(100, "at least"), (2_000_000, "at most"), ("abc", "whole number"), (True, "number")]:
        with pytest.raises(ValueError, match=why):
            r.params["context"].coerce("context", value)
    with pytest.raises(ValueError, match="one of"):
        r.params["quantization"].coerce("quantization", "int3")


DIGEST_RE = re.compile(r"^[a-z0-9][a-z0-9./_-]*:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")


def test_fy_rec_008_every_builtin_image_is_pinned_by_digest():
    """FY-REC-008: every container variant names its image as repo:tag@sha256:<digest> (the tag for
    people, the digest for what runs), so a tag moved on the registry cannot change what an agent
    runs. Bump with scripts/pin_images.py --write."""
    images = [(r.name, v.name, v.image) for r, _ in recipes.builtin_recipes().values() for v in r.variants
              if v.runtime == "container"]
    assert len(images) == 12
    unpinned = [f"{name}/{variant}: {image}" for name, variant, image in images if not DIGEST_RE.match(image or "")]
    assert unpinned == []
    # One tag, one digest: the same tag in two recipes (vllm and vllm-pooling) is the same image.
    by_tag: dict[str, set[str]] = {}
    for _, _, image in images:
        tag, digest = image.split("@")
        by_tag.setdefault(tag, set()).add(digest)
    assert all(len(d) == 1 for d in by_tag.values()), by_tag
