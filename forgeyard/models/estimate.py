"""How much memory a replica needs, before anything is downloaded (design section 5).

- safetensors (vLLM): weights = parameters x bytes per parameter (from the safetensors headers;
  FP8 quantisation keeps embeddings and norms in 16 bits); KV cache = 2 x layers x KV heads x
  head size x tokens x bytes, where tokens = context x concurrent sequences; plus the recipe's
  engine overhead and the CUDA context of the process.
- GGUF (llama.cpp): the exact tensor sizes from the header, the KV cache of the offloaded layers
  (f16), the logits buffer (vocabulary x 512-token micro-batch x 4 bytes) and the recipe overhead.
- files (faster-whisper): the model files plus the recipe overhead.
- fixed: the recipe's number.
- A declared `vram` parameter replaces all of it (and is required for local:// models, whose
  files the server cannot read).

Every number is per replica and per GPU: with N GPUs (tensor parallel) weights and KV cache split
across them, the overhead and the context do not.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from forgeyard.models import modalities
from forgeyard.models.recipes import Recipe

GIB = 1024**3
MIB = 1024**2
LLAMA_UBATCH = 512
_DTYPE_BYTES = {"bfloat16": 2, "float16": 2, "half": 2, "float32": 4, "float": 4, "fp8": 1}


class EstimateError(ValueError):
    pass


def gib(n: float) -> str:
    return f"{n / GIB:.2f} GiB"


@dataclass
class Part:
    label: str
    bytes: int
    detail: str = ""


@dataclass
class Estimate:
    method: str
    device: str                    # gpu | cpu
    gpus: int
    per_gpu: int = 0               # bytes reserved on each GPU
    ram: int = 0                   # bytes of host memory (CPU replicas)
    parts: list[Part] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    kv_tokens: int = 0

    @property
    def amount(self) -> int:
        """What is reserved per device: VRAM per GPU, or RAM for a CPU replica."""
        return self.per_gpu if self.device == "gpu" else self.ram

    def as_dict(self) -> dict:
        d = asdict(self)
        d["amount"] = self.amount
        return d


def kv_tokens(recipe: Recipe, params: dict) -> int:
    context = int(params.get("context") or 0)
    if recipe.estimate.kv_sequences == "one":
        return context
    return context * int(params.get("concurrency") or 1)


def _dtype_bytes(name: str) -> float:
    return _DTYPE_BYTES.get(str(name).lower().replace("torch.", ""), 2)


def _safetensors(meta: dict, recipe: Recipe, params: dict, gpus: int, ctx: int) -> Estimate:
    cfg = meta.get("config") or {}
    weights = meta.get("weights") or {}
    if not cfg.get("layers") or not cfg.get("kv_heads") or not cfg.get("head_dim"):
        raise EstimateError("the model's config.json lacks the layer, head or head-size counts the estimate needs")
    total_params = sum((weights.get("params") or {}).values())
    dtype = params.get("dtype") or "auto"
    quant = params.get("quantization") or "none"
    if quant == "fp8":
        w = int(weights.get("linear_params", 0) * 1 + weights.get("other_params", 0) * 2)
        wdetail = f"{total_params / 1e9:.2f} B parameters, linear layers in FP8, the rest in 16 bits"
    elif dtype == "auto":
        w = int(weights.get("bytes") or 0)
        wdetail = f"{total_params / 1e9:.2f} B parameters as stored ({cfg.get('dtype')})"
    else:
        w = int(total_params * _dtype_bytes(dtype))
        wdetail = f"{total_params / 1e9:.2f} B parameters x {int(_dtype_bytes(dtype))} B ({dtype})"
    if w <= 0:
        raise EstimateError("the model's weights have no known size")
    max_ctx = int(cfg.get("max_context") or 0)
    if max_ctx and int(params.get("context") or 0) > max_ctx:
        raise EstimateError(f"context: this model supports at most {max_ctx} tokens")
    kv_dtype = 2 if cfg.get("dtype") in (None, "float32") or dtype == "auto" else int(_dtype_bytes(dtype))
    tokens = kv_tokens(recipe, params)
    per_token = 2 * cfg["layers"] * cfg["kv_heads"] * cfg["head_dim"] * kv_dtype
    kv = per_token * tokens
    seqs = "1 sequence" if recipe.estimate.kv_sequences == "one" else f"{params.get('concurrency')} sequences"
    est = Estimate("safetensors-kv", "gpu", gpus, kv_tokens=tokens)
    est.parts = [
        Part("Weights", w // gpus, wdetail + (f", split over {gpus} GPUs" if gpus > 1 else "")),
        Part("KV cache", kv // gpus, f"2 x {cfg['layers']} layers x {cfg['kv_heads']} KV heads x {cfg['head_dim']} x "
                                     f"{tokens} tokens ({params.get('context')} x {seqs}) x {kv_dtype} B"),
        Part("Engine overhead", recipe.estimate.overhead, "activations, CUDA graphs, workspaces (recipe)"),
        Part("CUDA context", ctx, "per process (measured on an A100)"),
    ]
    est.parts += _vision(meta, recipe, params, "safetensors")
    est.per_gpu = sum(p.bytes for p in est.parts)
    _media_note(est, meta, "safetensors", params)
    return est


def _media(meta: dict, fmt: str, params: dict) -> tuple[bool, bool]:
    """(images on, video on) for this model and these parameters."""
    kinds = modalities.of_model(meta, fmt) or []
    images = int(params.get("images", modalities.DEFAULT_IMAGES) or 0)
    videos = int(params.get("videos", modalities.DEFAULT_VIDEOS) or 0)
    return "image" in kinds and images > 0, "video" in kinds and videos > 0


def _vision(meta: dict, recipe: Recipe, params: dict, fmt: str) -> list[Part]:
    """The vision encoder's share beyond the weights (1.3): vLLM profiles it at startup on the
    largest image (and video) before it sizes the KV cache; llama.cpp keeps compute buffers for
    the projector."""
    image, video = _media(meta, fmt, params)
    parts = []
    if image and recipe.estimate.vision_overhead:
        parts.append(Part("Vision encoder", recipe.estimate.vision_overhead,
                          "profiling of the largest image and its encoder cache (recipe)"))
    if video and recipe.estimate.video_overhead:
        parts.append(Part("Video", recipe.estimate.video_overhead, "profiling of the largest video (recipe)"))
    return parts


def _media_note(est: "Estimate", meta: dict, fmt: str, params: dict) -> None:
    image, video = _media(meta, fmt, params)
    if image or video:
        est.notes.append("Images and video take context tokens too (often 256 to 1,600 per image, depending on the "
                         "model and the image's size); leave room for them in the context length.")


def _gguf(meta: dict, recipe: Recipe, params: dict, gpus: int, ctx: int, device: str) -> Estimate:
    g = meta.get("gguf") or {}
    layers = int(g.get("layers") or 0)
    if not layers or not g.get("tensor_bytes"):
        raise EstimateError("the GGUF metadata lacks the layer count or tensor sizes")
    max_ctx = int(g.get("context_length") or 0)
    if max_ctx and int(params.get("context") or 0) > max_ctx:
        raise EstimateError(f"context: this model supports at most {max_ctx} tokens")
    tokens = kv_tokens(recipe, params)
    per_layer_token = int(g.get("kv_heads") or 0) * (int(g.get("key_length") or 0) + int(g.get("value_length") or 0)) * 2
    logits = int(g.get("vocab") or 0) * LLAMA_UBATCH * 4
    est = Estimate("gguf", device, gpus if device == "gpu" else 0, kv_tokens=tokens)
    kv_detail = (f"{layers} layers x {g.get('kv_heads')} KV heads x ({g.get('key_length')} + {g.get('value_length')}) x "
                 f"{tokens} tokens ({params.get('context')} x {params.get('concurrency')}) x 2 B (f16)")
    if device == "cpu":
        est.parts = [
            Part("Weights", int(g["tensor_bytes"]), f"{len(g.get('layer_bytes') or [])} blocks + embeddings, "
                                                     "memory-mapped from the cache"),
            Part("KV cache", per_layer_token * layers * tokens, kv_detail),
            Part("Logits buffer", logits, f"{g.get('vocab')} tokens x {LLAMA_UBATCH} x 4 B"),
            Part("Engine overhead", recipe.estimate.cpu_overhead, "compute buffers (recipe)"),
        ]
        est.parts += _projector(meta, recipe, params)
        est.ram = sum(p.bytes for p in est.parts)
        _media_note(est, meta, "gguf", params)
        return est
    offload = min(int(params.get("gpu_layers", 999)), layers)
    layer_bytes = list(g.get("layer_bytes") or [])
    on_gpu = sum(layer_bytes[layers - offload:]) if offload else 0
    if int(params.get("gpu_layers", 999)) > layers:
        on_gpu += int(g.get("output_bytes") or 0)
    est.parts = [
        Part("Weights", on_gpu // gpus, f"{offload} of {layers} layers on the GPU"
                                        + (" and the output layer" if int(params.get('gpu_layers', 999)) > layers else "")),
        Part("KV cache", per_layer_token * offload * tokens // gpus, kv_detail),
        Part("Logits buffer", logits, f"{g.get('vocab')} tokens x {LLAMA_UBATCH} x 4 B"),
        Part("Engine overhead", recipe.estimate.overhead, "compute buffers (recipe)"),
        Part("CUDA context", ctx, "per process"),
    ]
    est.parts += _projector(meta, recipe, params)
    est.per_gpu = sum(p.bytes for p in est.parts)
    _media_note(est, meta, "gguf", params)
    if offload < layers:
        est.notes.append(f"{layers - offload} layers stay on the CPU (host memory).")
    return est


def _projector(meta: dict, recipe: Recipe, params: dict) -> list[Part]:
    """A GGUF vision model's projector (mmproj): its file, loaded whole (llama.cpp offloads it to the
    GPU by default), plus its compute buffers."""
    m = meta.get("mmproj") if isinstance(meta.get("mmproj"), dict) else None
    if not m:
        return []
    parts = [Part("Projector (mmproj)", int(m.get("size") or 0), f"{m.get('path')}, loaded whole")]
    return parts + _vision(meta, recipe, params, "gguf")


def _files(meta: dict, recipe: Recipe, gpus: int, ctx: int, device: str) -> Estimate:
    w = int(meta.get("weights_bytes") or meta.get("size_bytes") or 0)
    if w <= 0:
        raise EstimateError("the model's files have no known size")
    est = Estimate("files", device, gpus if device == "gpu" else 0)
    if device == "cpu":
        est.parts = [Part("Model files", w), Part("Engine overhead", recipe.estimate.cpu_overhead, "recipe")]
        est.ram = sum(p.bytes for p in est.parts)
    else:
        est.parts = [Part("Model files", w // gpus), Part("Engine overhead", recipe.estimate.overhead, "recipe"),
                     Part("CUDA context", ctx, "per process")]
        est.per_gpu = sum(p.bytes for p in est.parts)
    return est


def estimate(meta: dict, recipe: Recipe, params: dict, *, device: str, gpu_context: int) -> Estimate:
    """The memory one replica needs on each device. `params` are validated already."""
    gpus = int(params.get("gpus") or 0) if device == "gpu" else 0
    if device == "gpu" and gpus < 1:
        raise EstimateError("a GPU replica needs at least one GPU")
    declared = params.get("vram")
    if declared:
        est = Estimate("declared", device, gpus)
        est.parts = [Part("Declared", int(declared), "set on the deployment")]
        if device == "gpu":
            est.per_gpu = int(declared)
        else:
            est.ram = int(declared)
        return est
    if meta.get("declared_only"):
        raise EstimateError("the server cannot read a local model: declare its memory (vram) on the deployment")
    method = recipe.estimate.method
    if method == "safetensors-kv":
        if device != "gpu":
            raise EstimateError(f"{recipe.name} runs on GPUs only")
        return _safetensors(meta, recipe, params, gpus, gpu_context)
    if method == "gguf":
        return _gguf(meta, recipe, params, gpus, gpu_context, device)
    if method == "files":
        return _files(meta, recipe, gpus, gpu_context, device)
    est = Estimate("fixed", device, gpus)
    est.parts = [Part("Recipe constant", int(recipe.estimate.vram or 0))]
    if device == "gpu":
        est.parts.append(Part("CUDA context", gpu_context, "per process"))
        est.per_gpu = sum(p.bytes for p in est.parts)
    else:
        est.ram = int(recipe.estimate.vram or 0)
    return est


def gpu_memory_utilization(reserved: int, card_total: int, gpu_context: int) -> float:
    """vLLM's --gpu-memory-utilization for a replica that may use `reserved` bytes of a card.

    vLLM sizes itself to `utilization x the card's TOTAL memory` and refuses to start unless that
    much is free (measured after its own CUDA context exists). So the fraction is the reservation
    minus the context, over the total, rounded DOWN to 1/1000 (at most ~80 MiB on an 80 GB card):
    the process then stays inside what the scheduler reserved, whatever else runs on the card."""
    if card_total <= 0:
        raise EstimateError("the card's total memory is unknown")
    frac = max(0, reserved - gpu_context) / card_total
    util = int(frac * 1000) / 1000
    if util < 0.01:
        raise EstimateError("the reservation is too small for vLLM")
    return min(util, 0.99)


def headroom_note(per_gpu: int, available: Optional[int]) -> Optional[str]:
    if available is None:
        return None
    return f"{gib(available - per_gpu)} left on the card afterwards" if available >= per_gpu else None
