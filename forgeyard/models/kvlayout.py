"""What a model keeps in memory per token and per sequence, layer by layer (1.4.5, #167).

The plain formula (2 x layers x KV heads x head size per token) holds for a model whose every layer is full
attention with the same heads. Others keep less, and the estimate counted them as if they did not:

- sliding-window layers (Gemma 2, 3 and 4, gpt-oss, Mistral 7B v0.1, Ministral) keep at most the window, not the
  whole context: vLLM 0.32 the last `window - 1` tokens plus what it has in flight
  (vllm/v1/kv_cache_interface.py: SlidingWindowSpec.max_admission_blocks_per_request), llama.cpp `window x
  sequences + 512` cells for all sequences together, padded to 256 (src/llama-kv-cache-iswa.cpp, without
  --swa-full, which the recipe never passes);
- MLA (DeepSeek V2 and V3, Kimi K2): one compressed latent per token and layer, `kv_lora_rank + qk_rope_head_dim`
  values, with no separate V (vLLM: MLAAttentionSpec; llama.cpp: a K-only cache when the GGUF has
  `attention.key_length_mla`, src/llama-kv-cache.cpp). vLLM keeps the whole latent on every GPU of a tensor-parallel
  replica;
- hybrid and linear-attention models (Qwen3-Next, Granite 4 hybrids, Falcon-H1, Jamba): only the attention layers
  grow with the context; a Mamba or gated-delta-net layer keeps a fixed state per sequence (its convolution window
  and its recurrent state: vllm/model_executor/layers/mamba/mamba_utils.py, llama.cpp's llama_hparams::n_embd_r and
  n_embd_s);
- KV-shared layers (Gemma 4 E2B, E4B: `num_kv_shared_layers`) keep nothing of their own.

A layout is stored with the catalog entry only for a model that is not plain: `kinds` has one letter per layer
(F full attention, S sliding window, R recurrent state only, B attention and a recurrent state, - nothing of its
own), and each kind its heads and values per head and token (K and V together; MLA: the latent). A model whose
layout is not one Forgeyard knows gets none, and the plain formula, which can only count too much.
"""

from __future__ import annotations

from typing import Optional

FULL, SLIDING, STATE, BOTH, NONE = "F", "S", "R", "B", "-"
ATTENTION = (FULL, SLIDING, BOTH)
RECURRENT = (STATE, BOTH)

# transformers' layer_types names.
_TYPE_KIND = {"full_attention": FULL, "attention": FULL, "global": FULL, "chunked_attention": FULL,
              "sliding_attention": SLIDING, "local": SLIDING,
              "linear_attention": STATE, "mamba": STATE, "mamba2": STATE}

# Models whose config.json gives a sliding window and a pattern instead of layer_types: transformers derives the
# layers from these (Gemma2Config: every other layer, from the first; Gemma3TextConfig and Cohere2Config: all but
# every n-th).
_PATTERN = {"gemma2": 2, "gemma3": 6, "gemma3_text": 6, "cohere2": 4}

# Qwen2-family configs: a sliding window only with use_sliding_window, from layer max_window_layers on.
_QWEN2 = ("qwen2", "qwen2_moe", "qwen3", "qwen3_moe")


def _int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _types(text: dict, n: int) -> Optional[list[str]]:
    lt = text.get("layer_types")
    if isinstance(lt, list) and len(lt) == n and all(isinstance(t, str) for t in lt):
        kinds = [_TYPE_KIND.get(t) for t in lt]
        return None if None in kinds else kinds  # a layer type Forgeyard does not know: no layout
    return []


def _attention_kinds(text: dict, n: int) -> list[str]:
    """Full or sliding, per layer, for a model without layer_types (what vLLM 0.32 does: a model-wide sliding
    window applies to every layer, engine/arg_utils.py)."""
    mt = str(text.get("model_type") or "")
    window = _int(text.get("sliding_window"))
    if not window or text.get("use_sliding_window") is False:
        return [FULL] * n
    if mt in _QWEN2:
        start = text.get("max_window_layers")
        start = start if isinstance(start, int) and not isinstance(start, bool) and start >= 0 else n
        return [SLIDING if i >= start else FULL for i in range(n)]
    if mt in _PATTERN:
        p = _int(text.get("sliding_window_pattern")) or _PATTERN[mt]
        if mt == "gemma2":
            return [SLIDING if i % p == 0 else FULL for i in range(n)]
        return [SLIDING if (i + 1) % p else FULL for i in range(n)]
    return [SLIDING] * n


def _state(text: dict, mt: str) -> Optional[dict]:
    """A recurrent layer's state per sequence, in values: its convolution window and its recurrent state."""
    if mt == "qwen3_next":  # gated delta net (vLLM: gated_delta_net_state_shape)
        kh, vh = _int(text.get("linear_num_key_heads")), _int(text.get("linear_num_value_heads"))
        kd, vd = _int(text.get("linear_key_head_dim")), _int(text.get("linear_value_head_dim"))
        k = _int(text.get("linear_conv_kernel_dim"))
        if not (kh and vh and kd and vd and k):
            return None
        return {"conv": (kd * kh * 2 + vd * vh) * (k - 1), "ssm": vh * vd * kd}
    if mt == "jamba":  # Mamba 1 (mamba1_state_shape)
        inner = _int(text.get("mamba_expand")) * _int(text.get("hidden_size"))
        state, k = _int(text.get("mamba_d_state")), _int(text.get("mamba_d_conv"))
        if not (inner and state and k):
            return None
        return {"conv": inner * (k - 1), "ssm": inner * state}
    # Mamba 2 (mamba2_state_shape): Granite 4 hybrids, Falcon-H1, Nemotron-H.
    heads, head = _int(text.get("mamba_n_heads")), _int(text.get("mamba_d_head"))
    state, k = _int(text.get("mamba_d_state") or text.get("ssm_state_size")), _int(text.get("mamba_d_conv") or
                                                                                    text.get("conv_kernel"))
    groups = _int(text.get("mamba_n_groups") or text.get("n_groups")) or 1
    inner = _int(text.get("mamba_d_ssm")) or (_int(text.get("mamba_expand")) * _int(text.get("hidden_size"))) \
        or heads * head
    if not (heads and head and state and k and inner):
        return None
    return {"conv": (inner + 2 * groups * state) * (k - 1), "ssm": heads * head * state,
            **({"ssm_bytes": 4} if mt.startswith("nemotron_h") else {})}


def _hybrid_kinds(text: dict, mt: str, n: int) -> Optional[list[str]]:
    if mt == "qwen3_next":
        interval = _int(text.get("full_attention_interval")) or 4
        return [FULL if (i + 1) % interval == 0 else STATE for i in range(n)]
    if mt == "jamba":
        period, offset = _int(text.get("attn_layer_period")), text.get("attn_layer_offset")
        if not period or not isinstance(offset, int):
            return None
        return [FULL if i % period == offset else STATE for i in range(n)]
    if mt == "falcon_h1":  # attention and Mamba 2 side by side in every layer
        return [BOTH] * n
    if mt.startswith("nemotron_h"):
        pattern = text.get("hybrid_override_pattern")
        if not isinstance(pattern, str) or len(pattern) != n:
            return None
        letters = {"M": STATE, "*": FULL, "-": NONE, "E": NONE}
        kinds = [letters.get(c) for c in pattern]
        return None if None in kinds else kinds
    return None


def from_config(text: dict) -> Optional[dict]:
    """The layout of a transformers config (its text part), or None for a plain model or one it does not know."""
    n = _int(text.get("num_hidden_layers") or text.get("n_layer"))
    heads = _int(text.get("num_attention_heads"))
    kv_heads = _int(text.get("num_key_value_heads")) or heads
    hidden = _int(text.get("hidden_size") or text.get("d_model"))
    head_dim = _int(text.get("head_dim")) or (hidden // heads if heads else 0)
    if not (n and kv_heads and head_dim):
        return None
    mt = str(text.get("model_type") or "")
    kinds = _types(text, n)
    if kinds is None:
        return None
    if not kinds:
        kinds = _hybrid_kinds(text, mt, n) if (mt in ("qwen3_next", "jamba", "falcon_h1")
                                               or mt.startswith("nemotron_h")) else _attention_kinds(text, n)
        if kinds is None:
            return None
    out: dict = {}
    if any(k in RECURRENT for k in kinds):
        state = _state(text, mt)
        if state is None:
            return None
        out["state"] = state
    shared = _int(text.get("num_kv_shared_layers"))
    if shared and shared < n:
        kinds = kinds[:n - shared] + [NONE] * shared
    lora, rope = _int(text.get("kv_lora_rank")), _int(text.get("qk_rope_head_dim"))
    if lora:
        if not rope:
            return None
        out["mla"] = True
        # One latent per token: a single "head", which tensor parallel therefore never splits.
        attn = {"heads": 1, "values": lora + rope}
        full, sliding = dict(attn), dict(attn)
    else:
        full = {"heads": kv_heads, "values": 2 * head_dim}
        sliding = dict(full)
        if mt.startswith("gemma4"):
            # Gemma 4's full-attention layers have their own head size and KV heads (global_head_dim,
            # num_global_key_value_heads); with attention_k_eq_v, vLLM still keeps K in both the K and the V slot.
            full = {"heads": _int(text.get("num_global_key_value_heads")) or kv_heads,
                    "values": 2 * (_int(text.get("global_head_dim")) or head_dim)}
    if SLIDING in kinds:
        window = _int(text.get("sliding_window"))
        if not window:
            return None
        out["sliding"] = {**sliding, "window": window}
    if FULL in kinds or BOTH in kinds:
        out["full"] = full
    plain = set(kinds) == {FULL} and not out.get("mla") and full == {"heads": kv_heads, "values": 2 * head_dim}
    if plain:
        return None
    return {"kinds": "".join(kinds), **out}


# ------------------------------------------------------------------ GGUF (llama.cpp)
# Architectures whose sliding-window layers follow a fixed pattern when the GGUF does not list them
# (`attention.sliding_window_pattern`): llama.cpp's load_swa_pattern(ml, n) per model (src/models/*.cpp), and the
# window when the file does not say (gemma2: 4096). Gemma 3 has sliding layers only when its file gives a window.
GGUF_SWA = {"gemma2": (2, 4096), "gemma3": (6, 0), "gemma3n": (5, 0), "cohere2": (4, 0), "gpt-oss": (2, 0),
            "exaone4": (4, 0), "gemma4": (0, 0)}
# Recurrent layers: which ones, per architecture (src/models/*.cpp: is_recr_impl).
GGUF_HYBRID = ("qwen3next", "granitehybrid", "jamba", "falcon-h1", "nemotron_h", "mamba", "mamba2")


def _pattern(n_pattern: int, n: int) -> list[bool]:
    """llama_hparams::set_swa_pattern without dense_first: layer il slides unless it is the last of each
    group of n_pattern; 0 slides everywhere."""
    return [n_pattern == 0 or (il % n_pattern < n_pattern - 1) for il in range(n)]


def from_gguf(md: dict, arch: str, n: int, lengths: Optional[dict] = None) -> Optional[dict]:
    """The layout of a GGUF file from its metadata (`md`: the header's metadata, long arrays dropped as the
    reader drops them), or None for a plain model or one it does not know."""
    def get(key, default=None):
        return md.get(f"{arch}.{key}", default)

    if not n:
        return None
    heads = _int(get("attention.head_count"))
    kv = get("attention.head_count_kv", heads)
    if isinstance(kv, list):
        if len(kv) != n or not all(isinstance(x, int) for x in kv):
            return None
        per_layer_kv = list(kv)
    elif isinstance(kv, int):
        per_layer_kv = [kv] * n
    elif f"{arch}.attention.head_count_kv" in (lengths or {}):
        return None  # a per-layer list too long to keep: not known here
    else:
        per_layer_kv = [heads] * n
    embd = _int(get("embedding_length"))
    key_len = _int(get("attention.key_length")) or (embd // heads if heads else 0)
    value_len = _int(get("attention.value_length")) or key_len
    mla = bool(_int(get("attention.key_length_mla")) and _int(get("attention.value_length_mla")))
    # Which layers slide.
    swa = [False] * n
    window = _int(get("attention.sliding_window"))
    known = GGUF_SWA.get(arch)
    pattern = get("attention.sliding_window_pattern")
    if known is not None:
        if not window and known[1]:
            window = known[1]
        if window:
            if isinstance(pattern, list):
                if len(pattern) < n:
                    return None
                swa = [bool(x) for x in pattern[:n]]
            elif isinstance(pattern, int) and not isinstance(pattern, bool):
                swa = _pattern(pattern, n)
            elif f"{arch}.attention.sliding_window_pattern" in (lengths or {}) or not known[0]:
                return None  # the per-layer list is too long to keep, or this architecture always lists it
            else:
                swa = _pattern(known[0], n)
    # Which layers are recurrent.
    recr = [False] * n
    state: Optional[dict] = None
    if arch in GGUF_HYBRID:
        d_conv, d_inner = _int(get("ssm.conv_kernel")), _int(get("ssm.inner_size"))
        d_state, groups = _int(get("ssm.state_size")), _int(get("ssm.group_count"))
        if not (d_conv and d_inner and d_state):
            return None
        state = {"conv": (d_conv - 1) * (d_inner + 2 * groups * d_state), "ssm": d_state * d_inner}
        if arch == "qwen3next":
            listed = get("attention.recurrent_layers")
            if isinstance(listed, list) and len(listed) >= n:
                recr = [bool(x) for x in listed[:n]]
            else:
                interval = _int(get("full_attention_interval")) or 4
                recr = [(il + 1) % interval != 0 for il in range(n)]
        elif arch == "falcon-h1":
            recr = [True] * n
        elif arch in ("mamba", "mamba2"):
            recr = [True] * n
            per_layer_kv = [0] * n
        else:  # granitehybrid, jamba, nemotron_h: a layer is recurrent iff it has no KV heads
            recr = [k == 0 for k in per_layer_kv]
    shared = _int(get("attention.shared_kv_layers"))
    first_shared = n - shared if 0 < shared < n else n  # Gemma 4: the last layers reuse earlier layers' KV
    kinds = []
    for il in range(n):
        attn = per_layer_kv[il] > 0 and (arch == "falcon-h1" or not recr[il])
        if il >= first_shared:
            kinds.append(NONE)
        elif attn and recr[il]:
            kinds.append(BOTH)
        elif recr[il]:
            kinds.append(STATE)
        elif not attn:
            kinds.append(NONE)
        else:
            kinds.append(SLIDING if swa[il] else FULL)
    out: dict = {}
    attn_heads = {per_layer_kv[il] for il in range(n) if kinds[il] in ATTENTION}
    if mla:
        out["mla"] = True
        # K only (the latent and the rope part): llama.cpp keeps no V cache for MLA.
        full = {"heads": max(attn_heads or {1}), "values": key_len}
        sliding = dict(full)
    else:
        full_heads = {per_layer_kv[il] for il in range(n) if kinds[il] in (FULL, BOTH)}
        swa_heads = {per_layer_kv[il] for il in range(n) if kinds[il] == SLIDING}
        if len(full_heads) > 1 or len(swa_heads) > 1:
            return None
        full = {"heads": next(iter(full_heads), 0), "values": key_len + value_len}
        k_swa, v_swa = _int(get("attention.key_length_swa")) or key_len, _int(get("attention.value_length_swa"))
        sliding = {"heads": next(iter(swa_heads), 0), "values": k_swa + (v_swa or k_swa)}
    if SLIDING in kinds:
        out["sliding"] = {**sliding, "window": window}
    if FULL in kinds or BOTH in kinds:
        out["full"] = full
    if state is not None and any(k in RECURRENT for k in kinds):
        out["state"] = state
    plain_kv = max(per_layer_kv) if per_layer_kv else 0
    if set(kinds) == {FULL} and not mla and full == {"heads": plain_kv, "values": key_len + value_len}:
        return None
    return {"kinds": "".join(kinds), **out}
