"""The built-in llama-cpp recipe on llama.cpp 1.0.0 (1.3.1): what every variant runs, that every flag it
passes is one llama-server 1.0.0 has, and the FP8 KV cache parameter with its estimate (FY-LCPP-*)."""

import json
import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from forgeyard.api.deployments import normalise_params
from forgeyard.models import gguf, recipes, render
from forgeyard.models.catalog import Context
from forgeyard.models.estimate import GGUF_KV_BYTES, MIB, EstimateError, estimate
from tests.model_helpers import FIX

R = recipes.builtin_recipes()["llama-cpp"][0]
CTX = 400 * MIB
HELP = (FIX.parent / "llama-server-v1.0.0-help.txt").read_text(encoding="utf-8")
MMPROJ = {"path": "mmproj-Qwen3VL-2B-Instruct-F16.gguf", "size": 819 * MIB}


def _meta(**extra) -> dict:
    g = gguf.parse((FIX / "qwen3-1.7b-q4_k_m.gguf-header").read_bytes()).summary()
    g["vocab"] = 151936  # the fixture's vocabulary is trimmed; the real one has 151,936 entries
    return {"gguf": g, **extra}


def _ctx(variant: recipes.Variant, params: dict, meta: dict | None = None) -> tuple[Context, list]:
    meta = meta if meta is not None else _meta()
    gpu = variant.gpu
    full = normalise_params(R, {**params, "gpus": 1 if gpu else 0})
    est = estimate(meta, R, full, device="gpu" if gpu else "cpu", gpu_context=CTX if gpu else 0)
    model = SimpleNamespace(meta=meta, format="gguf", engine_args={})
    ctx = Context(deployment=SimpleNamespace(served_name="chat"), model=model, recipe=R, params=full, estimate=est,
                  device="gpu" if gpu else "cpu", share="exclusive", gpus=1 if gpu else 0, machine_id=None)
    cards = [SimpleNamespace(vram_total=80 * 1024**3)] if gpu else []
    return ctx, cards


def _args(variant_name: str, params: dict | None = None, meta: dict | None = None) -> dict:
    v = R.variant(variant_name)
    ctx, cards = _ctx(v, params or {}, meta)
    return render.render(ctx, v, cards, "chat-0", gpu_context=CTX)["runtime"]


def _flags(args: list[str]) -> list[str]:
    return [a for a in args if re.fullmatch(r"--?[a-z][a-z0-9-]*", a)]


def test_fy_lcpp_001_every_variant_renders_the_same_flags():
    """FY-LCPP-001: each variant (cuda, rocm, vulkan, cpu, cpu-process) runs llama-server with the same
    flags: --fit off with an explicit context (-c = context x concurrency) and -ngl (999 on a GPU, 0 on
    the CPU), so its memory is where the estimate puts it; a container listens on 0.0.0.0:8080 inside
    it, a process on the agent's ${BIND_ADDRESS}:${PORT}. No KV cache type is passed by default."""
    gpu_args = ["-m", "${MODEL_FILE}", "--alias", "chat", "--host", "0.0.0.0", "--port", "8080", "-c", "8192",
                "-np", "2", "--kv-unified", "-ngl", "999", "--fit", "off", "-t", "8", "--no-webui"]
    for name in ("cuda", "rocm", "vulkan"):
        rt = _args(name)
        assert rt["type"] == "container" and rt["args"] == gpu_args, name
        assert rt["image"] == R.variant(name).image
    cpu = _args("cpu")
    assert cpu["type"] == "container" and cpu["args"] == [*gpu_args[:14], "0", *gpu_args[15:]]
    proc = _args("cpu-process")
    assert proc["type"] == "process" and proc["command"] == "llama-server"
    assert proc["args"] == ["-m", "${MODEL_FILE}", "--alias", "chat", "--host", "${BIND_ADDRESS}", "--port", "${PORT}",
                            "-c", "8192", "-np", "2", "--kv-unified", "-ngl", "0", "--fit", "off", "-t", "8",
                            "--no-webui"]
    four = _args("cuda", {"context": 2048, "concurrency": 4, "gpu_layers": 10, "threads": 3})["args"]
    assert four[four.index("-c") + 1] == "8192" and four[four.index("-np") + 1] == "4"
    assert four[four.index("-ngl") + 1] == "10" and four[four.index("-t") + 1] == "3"
    for rt in (cpu, proc, _args("cuda")):
        assert "-ctk" not in rt["args"] and "-ctv" not in rt["args"]


def test_fy_lcpp_002_projector_and_fp8_kv_cache_flags():
    """FY-LCPP-002: a model with a projector gets --mmproj, --image-max-tokens and --video-fps on every
    variant; kv_cache f8_e4m3 adds -ctk f8_e4m3 -ctv f8_e4m3 (K and V both) on every variant, with or
    without a projector; f16 adds nothing (llama-server's default), and neither does a deployment stored
    before the parameter existed."""
    meta = _meta(mmproj=MMPROJ)
    for name in ("cuda", "rocm", "vulkan", "cpu", "cpu-process"):
        a = _args(name, {}, meta)["args"]
        assert a[a.index("--mmproj") + 1] == "${MODEL_PATH}/mmproj-Qwen3VL-2B-Instruct-F16.gguf", name
        assert a[a.index("--image-max-tokens") + 1] == "1024" and float(a[a.index("--video-fps") + 1]) == 1.0
        f8 = _args(name, {"kv_cache": "f8_e4m3"}, meta)["args"]
        assert f8[-4:] == ["-ctk", "f8_e4m3", "-ctv", "f8_e4m3"], name
        assert f8[:-4] == a, name
        plain = _args(name, {"kv_cache": "f8_e4m3"})["args"]
        assert plain[-4:] == ["-ctk", "f8_e4m3", "-ctv", "f8_e4m3"] and "--mmproj" not in plain
        assert "-ctk" not in _args(name, {"kv_cache": "f16"})["args"]
    v = R.variant("cuda")
    ctx, cards = _ctx(v, {})
    ctx.params = {k: x for k, x in ctx.params.items() if k != "kv_cache"}  # a deployment from before 1.3.1
    old = render.render(ctx, v, cards, "chat-0", gpu_context=CTX)["runtime"]["args"]
    assert "-ctk" not in old and "-ctv" not in old


def test_fy_lcpp_003_every_flag_is_one_llama_server_1_0_0_has():
    """FY-LCPP-003: every flag the recipe passes (args and optional_args) is listed by llama-server 1.0.0's
    --help (tests/fixtures/llama-server-v1.0.0-help.txt, from the release binary), --fit still takes off,
    -ctk and -ctv take f8_e4m3, and --api-key still reads LLAMA_API_KEY, where the replica's key goes."""
    known = set()
    for line in HELP.splitlines():
        if line.startswith("-"):  # "-c,    --ctx-size N   …": the names, at the start and after each comma
            known |= set(re.findall(r"(?:^|,\s*)(--?[A-Za-z][\w-]*)", line))
    passed = _flags(R.args) + [f for oa in R.optional_args for f in _flags(oa.args)]
    assert passed and set(passed) <= known, sorted(set(passed) - known)
    assert {"--fit", "-c", "-ngl", "--mmproj", "--image-max-tokens", "--video-fps", "-ctk", "-ctv"} <= set(passed)
    assert re.search(r"--fit \[on\|off\]", HELP)
    for flag in ("-ctk", "-ctv"):
        block = HELP[HELP.index(f"{flag},"):][:400]
        assert "f8_e4m3" in block and "f16" in block
    api = HELP[HELP.index("--api-key KEY"):][:400]
    assert f"(env: {R.api_key_env})" in api
    kv = R.params["kv_cache"]
    assert kv.default == "f16" and kv.advanced and set(kv.choices) <= set(GGUF_KV_BYTES)


def test_fy_lcpp_004_the_estimate_follows_the_kv_cache_type():
    """FY-LCPP-004: the KV cache is 2 bytes a value with f16 (the default) and 1 with f8_e4m3, on the CPU
    and on a GPU (the offloaded layers only); other llama.cpp types count their block size (q8_0: 34
    bytes for 32 values); an unknown type is refused rather than guessed, and the deployment's parameter
    takes only the recipe's choices."""
    meta = _meta()
    g = meta["gguf"]
    values = 28 * 8 * (128 + 128)  # Qwen3-1.7B: layers x KV heads x (key + value length), per token
    base = {"context": 4096, "concurrency": 2, "gpus": 0, "gpu_layers": 999}

    def kv(params, device="cpu"):
        e = estimate(meta, R, {**base, **params}, device=device, gpu_context=CTX)
        return {p.label: p for p in e.parts}["KV cache"], e

    f16, e16 = kv({})
    f8, e8 = kv({"kv_cache": "f8_e4m3"})
    assert f16.bytes == values * 8192 * 2 and f8.bytes == values * 8192
    assert e16.ram - e8.ram == values * 8192
    assert f16.detail.endswith("x 2 B (f16)") and f8.detail.endswith("x 1 B (f8_e4m3)")
    gpu8, _ = kv({"kv_cache": "f8_e4m3", "gpus": 1, "gpu_layers": 10}, "gpu")
    assert gpu8.bytes == 8 * 256 * 10 * 8192 and g["layers"] == 28
    q8, _ = kv({"kv_cache": "q8_0"})
    assert q8.bytes == values * 8192 * 34 // 32 and q8.detail.endswith("x 34/32 B (q8_0)")
    with pytest.raises(EstimateError, match="kv_cache: q3_k"):
        kv({"kv_cache": "q3_k"})
    assert normalise_params(R, {"gpus": 0})["kv_cache"] == "f16"
    with pytest.raises(HTTPException):
        normalise_params(R, {"gpus": 0, "kv_cache": "q8_0"})


def test_fy_lcpp_005_what_fp8_saves_for_qwen3_4b_at_8k():
    """FY-LCPP-005: Qwen3-4B (36 layers, 8 KV heads, key and value length 128) at a context of 8,192: its
    KV cache is 1.125 GiB a sequence in f16 and half that in FP8, so f8_e4m3 saves 576 MiB a sequence,
    1,152 MiB at the default two concurrent requests."""
    g = dict(_meta()["gguf"], layers=36, kv_heads=8, key_length=128, value_length=128, context_length=40960,
             layer_bytes=[1] * 36)
    meta = {"gguf": g}
    cut = {}
    for n in (1, 2):
        p = {"context": 8192, "concurrency": n, "gpus": 1, "gpu_layers": 999}
        f16 = estimate(meta, R, p, device="gpu", gpu_context=CTX)
        f8 = estimate(meta, R, {**p, "kv_cache": "f8_e4m3"}, device="gpu", gpu_context=CTX)
        assert {x.label: x.bytes for x in f16.parts}["KV cache"] == n * 1152 * MIB
        cut[n] = f16.per_gpu - f8.per_gpu
    assert cut == {1: 576 * MIB, 2: 1152 * MIB}


def test_fy_lcpp_006_fp8_is_never_placed_on_vulkan(owner):
    """FY-LCPP-006: llama.cpp 1.0.0 has no FP8 for Vulkan (llama-server aborts at startup), so the vulkan
    variant lists kv_cache f8_e4m3 as unsupported: on an AMD APU an FP8 deployment is refused with the
    reason, while f16 goes to Vulkan there; a discrete card the ROCm image is built for (gfx1100) runs FP8
    on rocm, and so does the CPU."""
    from sqlalchemy import select

    from forgeyard.db import Accelerator, new_session
    from tests.test_scheduler import MODELS, deploy, replica_row, seed_catalog
    from tests.workload_helpers import PROCESS_OK, make_node

    seed_catalog()
    make_node("strix", "amd-gfx1151-apu", features=MODELS)

    def plan(**params):
        return owner.post("/api/deployments/estimate", json={"name": "t", "model": "qwen3-1.7b-gguf", "recipe": "llama-cpp",
                                                             "params": params}).json()["placement"]

    assert plan()["placed"][0]["variant"] == "vulkan"
    fp8 = plan(kv_cache="f8_e4m3")
    assert fp8["fits"] is False
    assert "vulkan: kv_cache f8_e4m3 is not available on this variant" in fp8["machines"][0]["reason"], fp8
    r = owner.post("/api/deployments", json={"name": "apu8", "model": "qwen3-1.7b-gguf", "recipe": "llama-cpp",
                                             "params": {"kv_cache": "f8_e4m3"}})
    assert r.status_code == 409 and "kv_cache f8_e4m3" in r.text
    radeon = make_node("radeon", "amd-gfx1151-apu", features=MODELS)
    db = new_session()
    try:
        card = db.scalar(select(Accelerator).where(Accelerator.node_id == radeon, Accelerator.vendor == "amd"))
        card.arch, card.unified_memory, card.vram_total = "gfx1100", False, 48 * 1024**3
        db.commit()
    finally:
        db.close()
    deploy(owner, "rocm8", "qwen3-1.7b-gguf", "llama-cpp", machine_id=radeon, params={"kv_cache": "f8_e4m3"})
    row = replica_row("rocm8")
    assert row.spec["runtime"]["image"] == R.variant("rocm").image
    assert row.spec["runtime"]["args"][-4:] == ["-ctk", "f8_e4m3", "-ctv", "f8_e4m3"]
    cpu = make_node("cpu-box", "nvidia-rtx4090x2", features=MODELS,
                    runtime_info={**PROCESS_OK, "container": {"ok": False, "reason": "docker not found"}})
    deploy(owner, "cpu8", "qwen3-1.7b-gguf", "llama-cpp", machine_id=cpu, params={"gpus": 0, "kv_cache": "f8_e4m3"})
    row = replica_row("cpu8")
    assert row.spec["runtime"]["type"] == "process" and row.spec["runtime"]["args"][-4:] == ["-ctk", "f8_e4m3", "-ctv",
                                                                                          "f8_e4m3"]
    # The CPU's variants are checked the same way: a custom recipe whose process variant refuses FP8.
    text = recipes.builtin_recipes()["llama-cpp"][1].replace("name: llama-cpp", "name: own-llama").replace(
        "    runtime: process\n", "    runtime: process\n    unsupported: {kv_cache: [f8_e4m3]}\n")
    assert owner.post("/api/recipes", json={"yaml": text}).status_code == 201
    r = owner.post("/api/deployments/estimate", json={"name": "t", "model": "qwen3-1.7b-gguf", "recipe": "own-llama",
                                                      "machine_id": cpu, "params": {"gpus": 0, "kv_cache": "f8_e4m3"}})
    why = r.json()["placement"]
    assert why["fits"] is False and "cpu-process: kv_cache f8_e4m3 is not available" in json.dumps(why), why


def test_fy_lcpp_007_a_variants_unsupported_values_are_checked():
    """FY-LCPP-007: a variant's `unsupported` names recipe parameters with at least one value; anything
    else is refused when the recipe is added. A value matches as text, and only for that parameter."""
    text = recipes.builtin_recipes()["llama-cpp"][1]
    for bad, why in (("unsupported: {kv_cache: [f8_e4m3]}", None),
                     ("unsupported: {kv_type: [f8_e4m3]}", "not a parameter"),
                     ("unsupported: {kv_cache: []}", "lists no values")):
        changed = text.replace("unsupported: {kv_cache: [f8_e4m3]}", bad).replace("name: llama-cpp", "name: own")
        if why is None:
            recipes.parse_recipe(changed)
        else:
            with pytest.raises(recipes.RecipeError, match=why):
                recipes.parse_recipe(changed)
    vulkan = R.variant("vulkan")
    assert recipes.variant_params_problem(vulkan, {"kv_cache": "f8_e4m3"}) == \
        "kv_cache f8_e4m3 is not available on this variant"
    assert recipes.variant_params_problem(vulkan, {"kv_cache": "f16"}) is None
    assert recipes.variant_params_problem(vulkan, {}) is None
    assert recipes.variant_params_problem(R.variant("rocm"), {"kv_cache": "f8_e4m3"}) is None
