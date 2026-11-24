"""The built-in `transformers` recipe's models (1.5.4, #141 and #144): image embeddings and text classification on
Forgeyard's own transformers server, the vision image (images/vision) started with --task embed or --task classify.

Which catalog models it runs, read from the config.json the catalog kept (the server's own rule,
images/vision/app.py `embed_family` and `text_classifier_problem`, so what is offered is what loads):

- **embed**: a CLIP-like model (vision.ZERO_SHOT_CLASSIFY: CLIP, SigLIP, SigLIP 2, AltCLIP, Chinese-CLIP, ALIGN,
  MetaCLIP 2), texts and images in one space, or DINOv2 (`Dinov2Model`, with registers too), images only. A text
  embedding model is not offered it: vLLM (vllm-pooling) serves those, faster.
- **classify**: every text classification model of the catalog (a sequence classifier with two labels or more, not a
  regression head: forgeyard/models/textcls.py).

A local model (the server cannot read its config.json) is offered it too: whoever adds it answers for it, and the
server refuses at start a model it cannot run.

**Which engine serves what (1.5.4, the per-recipe rules).** vLLM's pooling runner (vllm-pooling, or any recipe of the
`vllm` engine serving embed or classify) is offered only the models it runs:

- **classify**: not an NLI (zero-shot) model, and an architecture vLLM runs as a classifier
  (forgeyard/models/textcls.py `vllm_refusal`); the others go to the transformers server;
- **embed**: a text embedding model. A CLIP-like model or DINOv2 is the transformers server's ("use the transformers
  engine"); a multimodal embedding model vLLM would run with chat-style messages (Qwen3-VL-Embedding, GME: a vision
  tower in a model added as embed) is refused for now, until it is accepted on a GPU host (#141, Q4).

So **the default engine** (the first of a model's recipes: the deploy form, the add preview) stays vLLM for everything
vLLM ran before 1.5.4, and is the transformers server for what vLLM cannot run. A model no engine runs is refused when
it is added (`add_refusal`), and a catalog entry added before these rules is refused at deploy (`deploy_refusal`).

What the catalog keeps of an embedding model (`meta.embed`, from its config.json, `embed_summary`): its family
(clip, siglip, …, image-only, or text), what it takes (`modalities`: text and image, image, or text), its vector
size and its longest text in tokens, where config.json says them. An entry from before 1.5.4 has none: it is read
from the config summary kept with it (`embed_of`).
"""

from __future__ import annotations

from typing import Optional

from forgeyard.models import textcls, vision

ENGINE = "forgeyard-vision"
TASKS = ("embed", "classify")
SINCE = (1, 5, 4)                 # the vision image that first answers --task embed and --task classify
IMAGE_ONLY = {"dinov2": "Dinov2Model", "dinov2_with_registers": "Dinov2WithRegistersModel"}
_IMAGE_ONLY_ARCHS = {a.lower() for a in IMAGE_ONLY.values()}


def is_server(recipe, task: Optional[str]) -> bool:
    """`recipe` runs Forgeyard's transformers server for `task`: the vision image's engine, serving embed or classify
    (the built-in `transformers` recipe, or a custom copy of it)."""
    return recipe is not None and getattr(recipe, "engine", None) == ENGINE and task in TASKS \
        and task in (getattr(recipe, "tasks", None) or [])


def _config(meta: dict) -> dict:
    return meta.get("config") if isinstance(meta.get("config"), dict) else {}


def embed_family(meta: dict) -> Optional[str]:
    """"text+image" (a CLIP-like model), "image" (DINOv2), or None (any other model: a text embedder)."""
    cfg = _config(meta)
    if vision.zero_shot_classifier(cfg):
        return "text+image"
    archs = {a.lower() for a in cfg.get("architectures") or [] if isinstance(a, str)}
    kind = str(cfg.get("model_type") or "").strip().lower().replace("-", "_")
    if archs & _IMAGE_ONLY_ARCHS or (not archs and kind in IMAGE_ONLY):
        return "image"
    return None


def runs(task: str, meta: dict) -> bool:
    """The transformers server runs this catalog model as `task` (an unread local model: assumed so)."""
    if meta.get("kind") == "local":
        return True
    if task == "embed":
        return embed_family(meta) is not None
    if task == "classify":
        s = meta.get("classify") if isinstance(meta.get("classify"), dict) else None
        return s is not None and (s.get("labels") or 0) >= 2 and s.get("problem") != "regression"
    return False


def is_vllm(recipe, task: Optional[str]) -> bool:
    """`recipe` serves `task` (embed or classify) on vLLM's pooling runner: the built-in vllm-pooling, or a custom
    recipe of the vllm engine."""
    return recipe is not None and getattr(recipe, "engine", None) == "vllm" and task in TASKS \
        and task in (getattr(recipe, "tasks", None) or [])


# What vLLM cannot embed yet (Q4): a multimodal model it would run with chat-style messages.
MULTIMODAL_EMBED_LATER = ("This is a multimodal embedding model (a vision tower in config.json: Qwen3-VL-Embedding, "
                          "GME and the like). vLLM serves these with chat-style messages, which Forgeyard does not "
                          "offer yet: they are planned for a later 1.5 patch, after an acceptance on a GPU host. For "
                          "image embeddings today, use a CLIP-like model (CLIP, SigLIP, SigLIP 2) or DINOv2 on "
                          "Forgeyard's transformers server (transformers).")
IMAGE_EMBED_ON_TRANSFORMERS = ("vLLM's pooling runner does not embed images with this model ({family}): deploy it on "
                               "Forgeyard's transformers server (transformers), which embeds CLIP-like models and "
                               "DINOv2.")


def _has_vision_tower(meta: dict) -> bool:
    mods = meta.get("modalities")
    return isinstance(mods, list) and any(k in mods for k in ("image", "video", "audio"))


def vllm_refusal(task: str, meta: dict) -> Optional[str]:
    """Why vLLM's pooling runner cannot serve this catalog model as `task` (a message), or None. An unread local
    model is assumed to run."""
    if meta.get("kind") == "local":
        return None
    if task == "classify":
        s = meta.get("classify") if isinstance(meta.get("classify"), dict) else None
        return textcls.vllm_refusal(s) if s is not None else None
    if task == "embed":
        fam = embed_family(meta)
        if fam is not None:
            return IMAGE_EMBED_ON_TRANSFORMERS.format(family="a CLIP-like model" if fam == "text+image"
                                                      else "DINOv2, images only")
        if _has_vision_tower(meta):
            return MULTIMODAL_EMBED_LATER
    return None


def offered(recipe, task: str, meta: dict) -> bool:
    """Whether a recipe that serves `task` and the model's format is offered for this model: the transformers
    server's only for the models it runs, vLLM's pooling runner only for those it runs (1.5.4), every other recipe
    as before."""
    if is_server(recipe, task):
        return runs(task, meta)
    if is_vllm(recipe, task):
        return vllm_refusal(task, meta) is None
    return True


def order(names: list[str], all_recipes: dict, task: Optional[str]) -> list[str]:
    """`names` (recipes that serve a model) with the transformers server's last, the others in their order: a model
    vLLM runs keeps vLLM first (its default), one only the transformers server runs has that one alone."""
    def last(n: str) -> bool:
        return is_server(all_recipes[n][0], task)
    return [n for n in names if not last(n)] + [n for n in names if last(n)]


def add_refusal(task: str, meta: dict, fmt: Optional[str], all_recipes: dict) -> Optional[str]:
    """Why a model cannot be added as `task` (embed or classify): no recipe that serves the task and its format is
    offered it. The reason vLLM gives, else the transformers server's. None when one serves it (or for another
    task, a local model, or a format no recipe of the task takes: that keeps its own message)."""
    if task not in TASKS or meta.get("kind") == "local":
        return None
    candidates = [r for r, _ in all_recipes.values() if fmt in r.formats and task in r.tasks]
    if not candidates or any(offered(r, task, meta) for r in candidates):
        return None
    if task == "embed" and _has_vision_tower(meta) and embed_family(meta) is None:
        return MULTIMODAL_EMBED_LATER
    why = deploy_refusal(task, meta, next((r for r in candidates if is_server(r, task)), None))
    return why[1] if why else vllm_refusal(task, meta)


def deploy_refusal(task: str, meta: dict, recipe) -> Optional[tuple[str, str]]:
    """Why this model cannot be deployed with `recipe` (an error code and a message), or None: a model the transformers
    server does not run, or a recipe whose vision image predates these tasks (SINCE); a model vLLM's pooling runner
    does not run (1.5.4: an entry added before these rules, an NLI model or a DINOv2 added as embed, say)."""
    if is_vllm(recipe, task):
        why = vllm_refusal(task, meta)
        return ("incompatible", why) if why else None
    if not is_server(recipe, task):
        return None
    if not runs(task, meta):
        what = ("a text embedding model: deploy it on vLLM (vllm-pooling); this engine embeds images, with a CLIP-like "
                "model (CLIP, SigLIP, SigLIP 2) or DINOv2") if task == "embed" else \
            "not a text classifier with two labels or more"
        return "incompatible", f"Forgeyard's transformers server does not run this model: it is {what}."
    return vision.old_image(recipe, "Image embeddings and text classification on Forgeyard's transformers server",
                            SINCE)


# ------------------------------------------------------------------ meta.embed (1.5.4)
SIGLIP_TYPES = ("siglip", "siglip2")
# Defaults transformers' configs fill in when config.json leaves them out (google/siglip2-base-patch16-224 names
# almost nothing): the vector size and the text length.
_DEFAULT_DIM = {"siglip": 768, "siglip2": 768, "clip": 512, "dinov2": 384, "dinov2_with_registers": 384}
_DEFAULT_TEXT = {"siglip": 64, "siglip2": 64, "clip": 77, "chinese_clip": 512, "altclip": 514, "align": 64,
                 "metaclip_2": 77}


def _int(v) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def embed_summary(config: dict) -> dict:
    """meta.embed of a model added as embed: {"family", "modalities", "dimensions"?, "text_max_tokens"?}, from its
    raw config.json. family: the model_type of a CLIP-like model (clip, siglip, siglip2, …), "image-only" (DINOv2),
    or "text" (a text embedding model, vLLM's)."""
    if not isinstance(config, dict):
        return {"family": "text", "modalities": ["text"]}
    kind = str(config.get("model_type") or "").strip().lower().replace("-", "_")
    text = config.get("text_config") if isinstance(config.get("text_config"), dict) else {}
    look = config.get("vision_config") if isinstance(config.get("vision_config"), dict) else {}
    fam = embed_family({"config": config})
    if fam == "text+image":
        dim = _int(config.get("projection_dim")) or (_int(text.get("projection_size")) or _int(look.get("hidden_size"))
                                                     if kind in SIGLIP_TYPES else None) or _DEFAULT_DIM.get(kind)
        out = {"family": kind or "clip", "modalities": ["text", "image"], "dimensions": dim,
               "text_max_tokens": _int(text.get("max_position_embeddings")) or _DEFAULT_TEXT.get(kind)}
    elif fam == "image":
        out = {"family": "image-only", "modalities": ["image"],
               "dimensions": _int(config.get("hidden_size")) or _DEFAULT_DIM.get(kind)}
    else:
        dim = _int(config.get("hidden_size")) or _int(text.get("hidden_size"))
        out = {"family": "text", "modalities": ["text"], "dimensions": dim}
    return {k: v for k, v in out.items() if v is not None}


def embed_of(meta: dict) -> dict:
    """What an embedding model takes and makes: meta.embed, or (an entry from before 1.5.4) read from the config
    summary kept with it; a local model, whose config the server never read, as a text model."""
    e = meta.get("embed") if isinstance(meta.get("embed"), dict) else None
    if e is not None and isinstance(e.get("modalities"), list):
        return e
    fam = embed_family(meta)
    if fam == "text+image":
        return {"family": "clip-like", "modalities": ["text", "image"]}
    if fam == "image":
        return {"family": "image-only", "modalities": ["image"]}
    return {"family": "text", "modalities": ["text"]}
