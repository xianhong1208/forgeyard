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
server refuses at start a model it cannot run. The recipe is listed after the others that serve the model, so a model
vLLM already ran keeps vLLM as its first choice (which engine a model defaults to is decided with the catalog's
per-recipe rules, 1.5.4's next step).

Until the built-in recipe's pin moves to a vision image from 1.5.4 (the release that adds these tasks publishes it;
a small PR moves the pin right after, docs/development.md), deploying on it is refused (vision_image_too_old), as
1.4.4's zero-shot detectors were.
"""

from __future__ import annotations

from typing import Optional

from forgeyard.models import vision

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


def offered(recipe, task: str, meta: dict) -> bool:
    """Whether a recipe that serves `task` and the model's format is offered for this model: every recipe but the
    transformers server's, which only for the models it runs."""
    return not is_server(recipe, task) or runs(task, meta)


def order(names: list[str], all_recipes: dict, task: Optional[str]) -> list[str]:
    """`names` (recipes that serve a model) with the transformers server's last, the others in their order."""
    def last(n: str) -> bool:
        return is_server(all_recipes[n][0], task)
    return [n for n in names if not last(n)] + [n for n in names if last(n)]


def deploy_refusal(task: str, meta: dict, recipe) -> Optional[tuple[str, str]]:
    """Why this model cannot be deployed with `recipe` (an error code and a message), or None: a model the transformers
    server does not run, or a recipe whose vision image predates these tasks (SINCE)."""
    if not is_server(recipe, task):
        return None
    if not runs(task, meta):
        what = ("a text embedding model: deploy it on vLLM (vllm-pooling); this engine embeds images, with a CLIP-like "
                "model (CLIP, SigLIP, SigLIP 2) or DINOv2") if task == "embed" else \
            "not a text classifier with two labels or more"
        return "incompatible", f"Forgeyard's transformers server does not run this model: it is {what}."
    return vision.old_image(recipe, "Image embeddings and text classification on Forgeyard's transformers server",
                            SINCE)
