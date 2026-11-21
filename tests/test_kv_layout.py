"""KV cache by layer kind (1.4.5, #167): sliding-window, MLA, hybrid and KV-shared layers, from real config.json
files and real GGUF headers (FY-KVL-*).

Fixtures in tests/fixtures/models/kv/: config.json of google/gemma-3-{4b,12b,27b}-it (the unsloth copies, same
files, not gated), google/gemma-4-{E2B,E4B,12B,26B-A4B,31B}-it, mistralai/Mistral-7B-v0.1,
mistralai/Ministral-8B-Instruct-2410, deepseek-ai/DeepSeek-V2-Lite-Chat, deepseek-ai/DeepSeek-V3,
moonshotai/Kimi-K2-Instruct, Qwen/Qwen3-Next-80B-A3B-Instruct, ibm-granite/granite-4.0-h-{small,tiny},
openai/gpt-oss-20b, tiiuae/Falcon-H1-7B-Instruct; GGUF headers (each file's own `source`), read by range requests,
with only the architecture's metadata kept, and the tensor list for three of them.
"""

import json

import pytest

from forgeyard.models import gguf, inspect, kvlayout, recipes
from forgeyard.models.estimate import (LLAMA_UBATCH, VLLM_BLOCK, VLLM_IN_FLIGHT, VLLM_STATE_COPIES, estimate,
                                       gguf_swa_cells)
from tests.model_helpers import FIX

GIB = 1024**3
MIB = 1024**2
KV = FIX / "kv"
R = {n: r for n, (r, _) in recipes.builtin_recipes().items()}
W = {"params": {"BF16": 1000}, "bytes": 2000, "linear_params": 900, "other_params": 100}


def cfg(name: str) -> dict:
    return json.loads((KV / f"{name}.config.json").read_text())


def summary(name: str) -> dict:
    return inspect.config_summary(cfg(name))


def header(name: str) -> gguf.GGUFHeader:
    d = json.loads((KV / f"{name}.gguf-header.json").read_text())
    return gguf.GGUFHeader(d["version"], d["metadata"], [gguf.Tensor(*t) for t in d["tensors"]], d["header_size"],
                           d["data_offset"], d["array_lengths"])


def layout_of(name: str) -> dict:
    h = header(name)
    return kvlayout.from_gguf(h.metadata, h.architecture, int(h.arch_value("block_count")), h.array_lengths)


def kv(meta: dict, recipe: str, **params) -> dict:
    base = {"dtype": "auto", "quantization": "none", "gpus": 1, "images": 0, "videos": 0, "gpu_layers": 999,
            "kv_cache": "f16"}
    e = estimate(meta, R[recipe], {**base, **params}, device="cpu" if params.get("gpus") == 0 else "gpu",
                 gpu_context=0)
    return {p.label: p.bytes for p in e.parts}


def swa(window: int, context: int) -> int:
    """What vLLM keeps per sequence of a sliding-window layer."""
    return min(window - 1 + VLLM_IN_FLIGHT + VLLM_BLOCK, context)


# ------------------------------------------------------------------ config.json
def test_fy_kvl_001_plain_models_keep_their_summary_and_estimate():
    """FY-KVL-001: a model whose every layer is plain full attention (Qwen3, its embedding and reranker models, the
    Qwen3-TTS talker) gets no layout: its summary and its estimate stay what they were."""
    for name in ("qwen3-4b", "qwen3-embedding-0.6b", "qwen3-reranker-0.6b", "qwen3-tts-0.6b-customvoice"):
        s = inspect.config_summary(json.loads((FIX / f"{name}.config.json").read_text()))
        assert "kv_layout" not in s, name
    s = inspect.config_summary(json.loads((FIX / "qwen3-4b.config.json").read_text()))
    assert kv({"config": s, "weights": W}, "vllm", context=8192, concurrency=4)["KV cache"] == 2 * 36 * 8 * 128 * 8192 * 4 * 2
    # Ministral 8B slides on 27 of its 36 layers, but its window (32,768) is its whole context: nothing changes.
    m = summary("ministral-8b-instruct-2410")
    assert m["kv_layout"]["kinds"].count("S") == 27 and m["kv_layout"]["sliding"]["window"] == 32768
    assert kv({"config": m, "weights": W}, "vllm", context=32768, concurrency=2)["KV cache"] == \
        2 * 36 * 8 * 128 * 32768 * 2 * 2


def test_fy_kvl_002_sliding_window_layers_from_config():
    """FY-KVL-002: which layers slide, as transformers and vLLM 0.32 read them: Gemma 3 from its pattern (all but
    every sixth), Gemma 4, gpt-oss and Ministral from layer_types, Mistral 7B v0.1 everywhere (a model-wide window);
    Qwen3's window is off (use_sliding_window false)."""
    g3 = summary("gemma-3-27b-it")["kv_layout"]
    assert g3["kinds"] == ("SSSSSF" * 11)[:62] and g3["kinds"].count("F") == 10
    assert g3["sliding"] == {"heads": 16, "values": 256, "window": 1024} and g3["full"] == {"heads": 16, "values": 256}
    assert summary("gemma-3-4b-it")["kv_layout"]["kinds"] == ("SSSSSF" * 6)[:34]
    mistral = summary("mistral-7b-v0.1")["kv_layout"]
    assert mistral["kinds"] == "S" * 32 and mistral["sliding"]["window"] == 4096
    assert summary("gpt-oss-20b")["kv_layout"]["kinds"] == "SF" * 12
    # Gemma 4: full layers with their own head size and KV heads; E2B's last 20 layers reuse earlier KV.
    g4 = summary("gemma-4-31b-it")["kv_layout"]
    assert g4["full"] == {"heads": 4, "values": 1024} and g4["sliding"] == {"heads": 16, "values": 512, "window": 1024}
    e2b = summary("gemma-4-e2b-it")["kv_layout"]
    assert e2b["kinds"] == "SSSSF" * 3 + "-" * 20 and e2b["full"] == {"heads": 1, "values": 1024}
    q = json.loads((FIX / "qwen3-4b.config.json").read_text())
    # Qwen2.5's config.json names a window that is off: use_sliding_window false.
    assert kvlayout.from_config({**q, "sliding_window": 32768, "use_sliding_window": False, "max_window_layers": 21}) is None
    assert kvlayout.from_config({**q, "use_sliding_window": True, "sliding_window": 4096, "max_window_layers": 30}
                                )["kinds"] == "F" * 30 + "S" * 6


def test_fy_kvl_003_mla_and_hybrid_layers_from_config():
    """FY-KVL-003: MLA keeps one latent of kv_lora_rank + qk_rope_head_dim values, replicated on every GPU; Qwen3-Next
    keeps a gated-delta-net state on three layers of four; Granite 4 hybrids a Mamba 2 state where layer_types says
    mamba; Falcon-H1 attention and Mamba 2 side by side in every layer."""
    for name, n in (("deepseek-v2-lite-chat", 27), ("deepseek-v3", 61), ("kimi-k2-instruct", 61)):
        lay = summary(name)["kv_layout"]
        assert lay["kinds"] == "F" * n and lay["mla"] is True
        assert lay["full"] == {"heads": 1, "values": 512 + 64}, name
    nxt = summary("qwen3-next-80b-a3b-instruct")["kv_layout"]
    assert nxt["kinds"] == "RRRF" * 12 and nxt["full"] == {"heads": 2, "values": 512}
    assert nxt["state"] == {"conv": (128 * 16 * 2 + 128 * 32) * 3, "ssm": 32 * 128 * 128}
    tiny = summary("granite-4.0-h-tiny")["kv_layout"]
    assert tiny["kinds"].count("F") == 4 and tiny["kinds"].count("R") == 36
    assert tiny["state"] == {"conv": (3072 + 2 * 128) * 3, "ssm": 48 * 64 * 128}
    falcon = summary("falcon-h1-7b-instruct")["kv_layout"]
    assert falcon["kinds"] == "B" * 44 and falcon["state"] == {"conv": (3072 + 2 * 256) * 3, "ssm": 24 * 128 * 256}
    # A layer type nobody taught it: no layout, the plain formula (which can only count too much).
    odd = {**cfg("gpt-oss-20b")}
    odd["layer_types"] = ["sliding_attention"] * 23 + ["something_new"]
    assert kvlayout.from_config(odd) is None


def test_fy_kvl_004_vllm_kv_by_layer_kind():
    """FY-KVL-004: the vLLM estimate counts full layers for the context, sliding ones for at most their window plus
    what vLLM has in flight, MLA's latent without V, a recurrent state per sequence (two with prefix caching) that
    does not grow with the context."""
    g3 = {"config": summary("gemma-3-27b-it"), "weights": W}
    # At the default context the window plus what is in flight covers it all: nothing changes.
    assert kv(g3, "vllm", context=8192, concurrency=4)["KV cache"] == 2 * 62 * 16 * 128 * 8192 * 4 * 2
    long = kv(g3, "vllm", context=131072, concurrency=1)["KV cache"]
    assert long == (10 * 16 * 256 * 131072 + 52 * 16 * 256 * swa(1024, 131072)) * 2
    assert long < 2 * 62 * 16 * 128 * 131072 * 2 * 0.3
    ds = {"config": summary("deepseek-v3"), "weights": W}
    assert kv(ds, "vllm", context=65536, concurrency=2)["KV cache"] == 61 * 576 * 65536 * 2 * 2
    # Tensor parallel splits KV heads but not the latent: 8 GPUs each hold all of it.
    assert kv(ds, "vllm", context=65536, concurrency=2, gpus=8)["KV cache"] == 61 * 576 * 65536 * 2 * 2
    nxt = {"config": summary("qwen3-next-80b-a3b-instruct"), "weights": W}
    p = kv(nxt, "vllm", context=32768, concurrency=4)
    assert p["KV cache"] == 12 * 2 * 512 * 32768 * 4 * 2
    per_seq = ((128 * 16 * 2 + 128 * 32) * 3 + 32 * 128 * 128) * 2
    assert p["Recurrent state"] == 36 * per_seq * 2 * 4 and VLLM_STATE_COPIES == 2
    assert kv(nxt, "vllm", context=65536, concurrency=4)["Recurrent state"] == p["Recurrent state"]
    e2b = {"config": summary("gemma-4-e2b-it"), "weights": W}
    assert kv(e2b, "vllm", context=32768, concurrency=1)["KV cache"] == \
        (3 * 1 * 1024 * 32768 + 12 * 1 * 512 * swa(512, 32768)) * 2


def test_fy_kvl_005_kv_heads_are_never_split():
    """FY-KVL-005: with fewer KV heads than GPUs each GPU holds a whole head (vLLM replicates it): Qwen3-Next's 2 KV
    heads on 4 GPUs are 1 per GPU, half the cache, not a quarter; with as many heads as GPUs the split is even."""
    nxt = {"config": summary("qwen3-next-80b-a3b-instruct"), "weights": W}
    one = kv(nxt, "vllm", context=8192, concurrency=1)["KV cache"]
    assert kv(nxt, "vllm", context=8192, concurrency=1, gpus=4)["KV cache"] == one // 2
    assert kv(nxt, "vllm", context=8192, concurrency=1, gpus=2)["KV cache"] == one // 2
    plain = inspect.config_summary({**json.loads((FIX / "qwen3-4b.config.json").read_text()), "num_key_value_heads": 2})
    full = kv({"config": plain, "weights": W}, "vllm", context=8192, concurrency=1)["KV cache"]
    assert kv({"config": plain, "weights": W}, "vllm", context=8192, concurrency=1, gpus=4)["KV cache"] == full // 2
    assert kv({"config": plain, "weights": W}, "vllm", context=8192, concurrency=1, gpus=2)["KV cache"] == full // 2


# ------------------------------------------------------------------ GGUF
def test_fy_kvl_006_layouts_from_real_gguf_headers():
    """FY-KVL-006: llama.cpp's own rules: Gemma 3 slides on all but every sixth layer (no list in the file), gpt-oss on
    every other one, Gemma 4 as its file lists, with its shared-KV layers and its own head sizes for sliding layers;
    an MLA file (key_length_mla) keeps K only; recurrent layers are those without KV heads (Granite 4), three of
    four (Qwen3-Next) or every layer beside attention (Falcon-H1)."""
    g3 = layout_of("gemma-3-4b-it-q4_k_m")
    assert g3["kinds"] == ("SSSSSF" * 6)[:34] and g3["sliding"] == {"heads": 4, "values": 512, "window": 1024}
    assert layout_of("gpt-oss-20b-mxfp4")["kinds"] == "SF" * 12
    e2b = layout_of("gemma-4-e2b-it-q8_0")
    assert e2b["kinds"] == "SSSSF" * 3 + "-" * 20
    assert e2b["full"] == {"heads": 1, "values": 1024} and e2b["sliding"] == {"heads": 1, "values": 512, "window": 512}
    g4 = layout_of("gemma-4-31b-it-q4_0")
    assert g4["full"] == {"heads": 4, "values": 1024} and g4["sliding"]["heads"] == 16
    mla = layout_of("deepseek-v3.1-ud-q2_k_xl-00001")
    assert mla["mla"] is True and mla["full"] == {"heads": 1, "values": 576} and mla["kinds"] == "F" * 61
    # An older DeepSeek-V2-Lite conversion without key_length_mla: llama.cpp keeps K and V of every head, as the
    # plain formula says.
    assert layout_of("deepseek-v2-lite-chat-q4_k_m") is None
    tiny = layout_of("granite-4.0-h-tiny-q4_k_m")
    assert tiny["kinds"] == ("RRRRRF" + "R" * 9 + "F" + "R" * 9 + "F" + "R" * 9 + "F" + "R" * 4)
    assert tiny["state"] == {"conv": 3 * (3072 + 2 * 128), "ssm": 128 * 3072}
    nxt = layout_of("qwen3-next-80b-a3b-q8_0-00001")
    assert nxt["kinds"] == "RRRF" * 12 and nxt["state"] == {"conv": 3 * (4096 + 2 * 16 * 128), "ssm": 128 * 4096}
    assert layout_of("falcon-h1-7b-instruct-bf16")["kinds"] == "B" * 44
    # Gemma 4 always lists its sliding layers: a file whose list was too long to keep gets no layout.
    h = header("gemma-4-e2b-it-q8_0")
    md = {k: v for k, v in h.metadata.items() if k != "gemma4.attention.sliding_window_pattern"}
    assert kvlayout.from_gguf(md, "gemma4", 35, {"gemma4.attention.sliding_window_pattern": 35}) is None
    assert kvlayout.from_gguf(md, "gemma4", 35, {}) is None


def test_fy_kvl_007_llama_cpp_kv_by_layer_kind():
    """FY-KVL-007: llama.cpp keeps context x sequences cells for full layers, window x sequences + 512 (padded to 256)
    for sliding ones, and a 32-bit state per sequence for recurrent ones; on the CPU the same."""
    g = header("gemma-3-4b-it-q4_k_m").summary()
    g["vocab"] = 262144
    tokens = 32768 * 2
    cells = gguf_swa_cells(1024, tokens, 2)
    assert cells == 2560 == (1024 * 2 + LLAMA_UBATCH + 255) // 256 * 256
    assert gguf_swa_cells(1000, 100000, 2) == 2560 and gguf_swa_cells(1000, 2000, 2) == 2048  # padded to 256
    want = (5 * 4 * 512 * tokens + 29 * 4 * 512 * cells) * 2
    assert kv({"gguf": g}, "llama-cpp", context=32768, concurrency=2)["KV cache"] == want
    assert kv({"gguf": g}, "llama-cpp", context=32768, concurrency=2, gpus=0)["KV cache"] == want
    # Short contexts: the window covers everything, so nothing changes.
    plain = 34 * 4 * 512 * 1024 * 2
    assert kv({"gguf": g}, "llama-cpp", context=512, concurrency=2)["KV cache"] == plain
    t = header("granite-4.0-h-tiny-q4_k_m").summary()
    p = kv({"gguf": t}, "llama-cpp", context=65536, concurrency=2)
    assert p["KV cache"] == 4 * 4 * 256 * 65536 * 2 * 2
    assert p["Recurrent state"] == 36 * (3 * (3072 + 256) + 128 * 3072) * 4 * 2


def test_fy_kvl_008_partial_offload_counts_the_layers_on_the_gpu():
    """FY-KVL-008: llama.cpp offloads the last layers: with 10 of Gemma 4 E2B's 35 on the GPU, all ten share KV with
    earlier layers, so the GPU holds no KV cache; with 25, the GPU has layers 10 to 34."""
    g = header("gemma-4-e2b-it-q8_0").summary()
    assert kv({"gguf": g}, "llama-cpp", context=8192, concurrency=1, gpu_layers=10)["KV cache"] == 0
    p = kv({"gguf": g}, "llama-cpp", context=8192, concurrency=1, gpu_layers=25)["KV cache"]
    # layers 10..14 = SSSSF: 4 sliding, 1 full
    assert p == (1 * 1 * 1024 * 8192 + 4 * 1 * 512 * gguf_swa_cells(512, 8192, 1)) * 2


@pytest.mark.parametrize("name", ["gemma-3-4b-it-q4_k_m", "gemma-4-e2b-it-q8_0", "granite-4.0-h-tiny-q4_k_m"])
def test_fy_kvl_009_summary_stores_the_layout(name):
    """FY-KVL-009: the GGUF summary stored with a catalog entry carries the layout (and a plain file none)."""
    h = header(name)
    assert h.summary()["kv_layout"] == kvlayout.from_gguf(h.metadata, h.architecture, h.summary()["layers"],
                                                         h.array_lengths)
    plain = gguf.parse((FIX / "qwen3-1.7b-q4_k_m.gguf-header").read_bytes()).summary()
    assert "kv_layout" not in plain
