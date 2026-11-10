"""Inspecting models and estimating their memory, against real config.json files, real
safetensors headers and a real GGUF header (FY-INS-*, FY-GGUF-*, FY-EST-*)."""

import struct

import pytest

from forgeyard.models import fetch, gguf, inspect, recipes, safetensors
from forgeyard.models.estimate import EstimateError, estimate, gpu_memory_utilization
from tests.model_helpers import FIX, QWEN4B_REV, FakeHF, fixture_json, gguf_repo, qwen4b_repo, tiny_repo

GIB = 1024**3
MIB = 1024**2
CTX = 400 * MIB
A100_TOTAL = 85899345920
R = {n: r for n, (r, _) in recipes.builtin_recipes().items()}
LOCAL = fetch.Policy(allow_private=True)


@pytest.fixture()
def hf():
    server = FakeHF(qwen4b_repo(), gguf_repo(), tiny_repo(), tiny_repo("acme/gated", gated=True))
    yield server
    server.close()


def test_fy_ins_001_qwen3_4b_from_its_real_headers(hf):
    """FY-INS-001: pinned to the commit, architecture from config.json, 4,022,468,096 BF16
    parameters from the three safetensors headers (two range requests per shard)."""
    meta = inspect.inspect_hf("Qwen/Qwen3-4B", "main", None, endpoint=hf.url, policy=LOCAL)
    assert meta["revision"] == QWEN4B_REV and meta["format"] == "safetensors" and meta["task_hint"] == "chat"
    assert meta["config"] == {"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3", "layers": 36, "heads": 32,
                              "kv_heads": 8, "head_dim": 128, "hidden_size": 2560, "vocab": 151936,
                              "max_context": 40960, "dtype": "bfloat16", "quantization": None}
    w = meta["weights"]
    assert w["params"] == {"BF16": 4022468096} and w["bytes"] == 8044936192
    assert w["linear_params"] + w["other_params"] == 4022468096 and w["other_params"] == 389152256
    assert {f["path"] for f in meta["files"]} == {f["rfilename"] for f in fixture_json("qwen3-4b.hf-revision.json")["siblings"]} - {".gitattributes"}
    assert meta["size_bytes"] == sum(f["size"] for f in meta["files"])
    shard_reads = [r for r in hf.requests if ".safetensors" in r["path"] and "/cdn/" in r["path"]]
    assert len(shard_reads) == 6 and all(r["range"] for r in shard_reads)


def test_fy_ins_002_gguf_metadata_by_range_reads(hf):
    """FY-INS-002: the default Q4_K_M file, its metadata and exact tensor sizes, from range reads."""
    meta = inspect.inspect_hf("ggml-org/Qwen3-1.7B-GGUF", None, None, endpoint=hf.url, policy=LOCAL)
    assert meta["file"] == "Qwen3-1.7B-Q4_K_M.gguf" and meta["task_hint"] == "chat"
    assert [c["path"] for c in meta["gguf_files"]] == ["Qwen3-1.7B-Q4_K_M.gguf", "Qwen3-1.7B-Q8_0.gguf", "Qwen3-1.7B-f16.gguf"]
    g = meta["gguf"]
    assert (g["architecture"], g["layers"], g["kv_heads"], g["key_length"], g["context_length"]) == ("qwen3", 28, 8, 128, 40960)
    assert g["tensor_bytes"] == 1276487680 and g["tensor_count"] == 311
    # The real file is 1,282,439,264 bytes: header (5,951,573, padded to 5,951,584) + tensors.
    assert 5951584 + g["tensor_bytes"] == 1282439264
    assert [f["path"] for f in meta["files"]] == ["Qwen3-1.7B-Q4_K_M.gguf"] and meta["size_bytes"] == 1282439264
    assert all(r["range"] for r in hf.requests if "/cdn/" in r["path"])


def test_fy_ins_003_gated_and_missing_repositories(hf):
    """FY-INS-003: a gated repository needs a token (used for these requests only)."""
    with pytest.raises(inspect.InspectError, match="gated or private"):
        inspect.inspect_hf("acme/gated", None, None, endpoint=hf.url, policy=LOCAL)
    meta = inspect.inspect_hf("acme/gated", None, None, endpoint=hf.url, policy=LOCAL, token=FakeHF.TOKEN)
    assert meta["format"] == "safetensors" and FakeHF.TOKEN not in repr(meta)
    assert [f["path"] for f in meta["files"]] == ["config.json", "README.md", "model.safetensors", "tokenizer.json",
                                                  "sub/dir/extra.txt"]
    with pytest.raises(inspect.InspectError, match="does not exist"):
        inspect.inspect_hf("acme/nothing", None, None, endpoint=hf.url, policy=LOCAL)
    with pytest.raises(inspect.InspectError, match="revision"):
        inspect.inspect_hf("acme/gated", "../x", None, endpoint=hf.url, policy=LOCAL)


def test_fy_gguf_001_header_reader_grows_its_window():
    """FY-GGUF-001: starting small, the reader doubles until the header is complete; a file that
    ends early or is not GGUF is refused."""
    data = (FIX / "qwen3-1.7b-q4_k_m.gguf-header").read_bytes()
    calls = []

    def fetch_range(a, b):
        calls.append((a, b))
        return data[a:b + 1]

    h = gguf.read_remote(fetch_range, first=4096)
    assert h.summary()["tensor_bytes"] == 1276487680 and len(calls) > 3 and calls[0] == (0, 4095)
    assert calls[1][0] == 4096  # only the missing bytes are fetched again
    with pytest.raises(gguf.GGUFError, match="ended"):
        gguf.read_remote(lambda a, b: data[:30000][a:b + 1], first=4096)
    with pytest.raises(gguf.GGUFError, match="magic"):
        gguf.parse(b"NOPE" + data[4:])


def test_fy_gguf_002_long_arrays_are_skipped_not_kept():
    """FY-GGUF-002: a 151,936-entry vocabulary is skipped (only its length kept)."""
    def s(text):
        b = text.encode()
        return struct.pack("<Q", len(b)) + b
    tokens = b"".join(s(f"t{i}") for i in range(151936))
    buf = (b"GGUF" + struct.pack("<IQQ", 3, 0, 2) + s("general.architecture") + struct.pack("<I", 8) + s("qwen3")
           + s("tokenizer.ggml.tokens") + struct.pack("<IIQ", 9, 8, 151936) + tokens)
    h = gguf.parse(buf)
    assert h.array_lengths == {"tokenizer.ggml.tokens": 151936} and "tokenizer.ggml.tokens" not in h.metadata
    assert h.summary()["vocab"] == 151936


def test_fy_gguf_003_tensor_sizes_follow_ggml_block_sizes():
    """FY-GGUF-003"""
    assert gguf.Tensor("a", [2048, 151936], 14, 0).nbytes == 2048 * 151936 // 256 * 210   # Q6_K
    assert gguf.Tensor("b", [2048, 6144], 12, 0).nbytes == 2048 * 6144 // 256 * 144       # Q4_K
    assert gguf.Tensor("c", [2048], 0, 0).nbytes == 8192                                  # F32
    with pytest.raises(gguf.GGUFError, match="unknown ggml type"):
        _ = gguf.Tensor("d", [32], 99, 0).nbytes


def _qwen4b_meta():
    heads = fixture_json("qwen3-4b.safetensors-headers.json")
    import json
    cfg = inspect.config_summary(json.loads((FIX / "qwen3-4b.config.json").read_text()))
    return {"config": cfg, "weights": safetensors.merge([safetensors.count(h) for h in heads.values()])}


def _gguf_meta():
    g = gguf.parse((FIX / "qwen3-1.7b-q4_k_m.gguf-header").read_bytes()).summary()
    g["vocab"] = 151936  # the fixture's vocabulary is trimmed to 1,000 entries; the real one has 151,936
    return {"gguf": g}


def test_fy_est_001_vllm_qwen3_4b():
    """FY-EST-001: weights + KV cache (2 x 36 x 8 x 128 x 8192 x 4 x 2 B) + overhead + CUDA context."""
    params = {"context": 8192, "concurrency": 4, "dtype": "auto", "quantization": "none", "gpus": 1}
    e = estimate(_qwen4b_meta(), R["vllm"], params, device="gpu", gpu_context=CTX)
    parts = {p.label: p.bytes for p in e.parts}
    assert parts == {"Weights": 8044936192, "KV cache": 2 * 36 * 8 * 128 * 8192 * 4 * 2,
                     "Engine overhead": 2 * GIB, "CUDA context": CTX}
    assert e.per_gpu == sum(parts.values()) == 15443688448 and e.kv_tokens == 32768
    assert abs(e.per_gpu / GIB - 14.38) < 0.01
    two = estimate(_qwen4b_meta(), R["vllm"], {**params, "gpus": 2}, device="gpu", gpu_context=CTX)
    assert {p.label: p.bytes for p in two.parts}["Weights"] == 8044936192 // 2
    assert two.per_gpu == 8044936192 // 2 + 2 * 36 * 8 * 128 * 8192 * 4 * 2 // 2 + 2 * GIB + CTX


def test_fy_est_002_fp8_and_dtype():
    """FY-EST-002: FP8 shrinks only the linear layers; a float32 dtype doubles the weights."""
    base = {"context": 4096, "concurrency": 1, "dtype": "auto", "gpus": 1}
    fp8 = estimate(_qwen4b_meta(), R["vllm"], {**base, "quantization": "fp8"}, device="gpu", gpu_context=0)
    assert {p.label: p.bytes for p in fp8.parts}["Weights"] == 3633315840 + 389152256 * 2
    f16 = estimate(_qwen4b_meta(), R["vllm"], {**base, "dtype": "float16", "quantization": "none"}, device="gpu",
                   gpu_context=0)
    assert {p.label: p.bytes for p in f16.parts}["Weights"] == 4022468096 * 2


def test_fy_est_003_pooling_holds_one_sequence():
    """FY-EST-003: embeddings and rerank need the KV cache of one full-length sequence only."""
    import json
    cfg = inspect.config_summary(json.loads((FIX / "qwen3-embedding-0.6b.config.json").read_text()))
    meta = {"config": cfg, "weights": {"params": {"BF16": 595776512}, "bytes": 1191553024, "linear_params": 440000000,
                                       "other_params": 155776512}}
    e = estimate(meta, R["vllm-pooling"], {"context": 8192, "concurrency": 32, "dtype": "auto", "gpus": 1},
                 device="gpu", gpu_context=CTX)
    parts = {p.label: p.bytes for p in e.parts}
    assert parts["KV cache"] == 2 * 28 * 8 * 128 * 8192 * 2 == 939524096
    assert e.per_gpu == 1191553024 + 939524096 + GIB + CTX


def test_fy_est_004_context_beyond_the_model_is_refused():
    """FY-EST-004"""
    with pytest.raises(EstimateError, match="at most 40960"):
        estimate(_qwen4b_meta(), R["vllm"], {"context": 65536, "concurrency": 1, "dtype": "auto", "gpus": 1},
                 device="gpu", gpu_context=CTX)
    with pytest.raises(EstimateError, match="GPUs only"):
        estimate(_qwen4b_meta(), R["vllm"], {"context": 1024, "concurrency": 1, "gpus": 0}, device="cpu", gpu_context=0)


def test_fy_est_005_gguf_cpu_and_gpu_offload():
    """FY-EST-005: on the CPU all tensors + KV + logits buffer; on a GPU only the offloaded layers
    (and the output layer when everything is offloaded)."""
    meta = _gguf_meta()
    g = meta["gguf"]
    cpu = estimate(meta, R["llama-cpp"], {"context": 4096, "concurrency": 2, "gpus": 0, "gpu_layers": 999},
                   device="cpu", gpu_context=CTX)
    kv_per_token = 28 * 8 * (128 + 128) * 2
    assert cpu.ram == 1276487680 + kv_per_token * 8192 + 151936 * 512 * 4 + 512 * MIB
    full = estimate(meta, R["llama-cpp"], {"context": 4096, "concurrency": 2, "gpus": 1, "gpu_layers": 999},
                    device="gpu", gpu_context=CTX)
    assert {p.label: p.bytes for p in full.parts}["Weights"] == sum(g["layer_bytes"]) + g["output_bytes"]
    part = estimate(meta, R["llama-cpp"], {"context": 4096, "concurrency": 2, "gpus": 1, "gpu_layers": 10},
                    device="gpu", gpu_context=CTX)
    p = {x.label: x.bytes for x in part.parts}
    assert p["Weights"] == sum(g["layer_bytes"][18:]) and p["KV cache"] == 28 * 0 + 8 * 256 * 2 * 10 * 8192
    assert part.notes == ["18 layers stay on the CPU (host memory)."]


def test_fy_est_006_files_fixed_and_declared():
    """FY-EST-006: faster-whisper = model files + overhead + context; a declared amount replaces
    everything; a local model must declare one."""
    e = estimate({"weights_bytes": 483546902}, R["faster-whisper"], {"gpus": 1, "compute_type": "default"},
                 device="gpu", gpu_context=CTX)
    assert e.per_gpu == 483546902 + GIB + CTX
    d = estimate({"declared_only": True}, R["vllm"], {"gpus": 1, "vram": 20 * GIB}, device="gpu", gpu_context=CTX)
    assert d.per_gpu == 20 * GIB and d.method == "declared"
    with pytest.raises(EstimateError, match="declare its memory"):
        estimate({"declared_only": True}, R["vllm"], {"gpus": 1, "context": 1024, "concurrency": 1}, device="gpu",
                 gpu_context=CTX)


def test_fy_est_007_gpu_memory_utilization_comes_from_the_reservation():
    """FY-EST-007: (reservation - CUDA context) / the card's TOTAL, rounded down to 1/1000: vLLM's
    footprint (utilization x total + its context) never exceeds what was reserved, and wastes at
    most 1/1000 of the card."""
    reserved = 15443688448
    util = gpu_memory_utilization(reserved, A100_TOTAL, CTX)
    assert util == 0.174
    footprint = util * A100_TOTAL + CTX
    assert footprint <= reserved and reserved - footprint < A100_TOTAL / 1000
    assert gpu_memory_utilization(A100_TOTAL * 2, A100_TOTAL, CTX) == 0.99
    with pytest.raises(EstimateError, match="too small"):
        gpu_memory_utilization(CTX + 100 * MIB, A100_TOTAL, CTX)
    # A shared card: what matters for vLLM's start check is free memory >= utilization x total.
    free_on_card = 48294330368  # card #4 of the shared host
    assert util * A100_TOTAL < free_on_card
