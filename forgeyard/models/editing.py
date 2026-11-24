"""What an image model does with images (1.5.5, #143): makes them from text (`generate`), edits the images it is
given (`edit`: how many it takes, 0 for none), or both. Kept in the catalog entry's `meta.image` as `edit` and
`generate`; no new task and no migration.

- **A diffusers pipeline says it itself** (model_index.json's `_class_name`): FLUX.2 [klein] (`Flux2KleinPipeline`)
  makes images from text and edits up to four; Qwen-Image-Edit (`QwenImageEditPipeline`) and LongCat-Image-Edit only
  edit one; an inpainting pipeline needs a mask, which the gateway does not take (1.5.5), so it does neither.
- **A stable-diffusion.cpp file has no pipeline class** (a GGUF or a single safetensors file): what it does is read
  from its file and repository names ("edit", "kontext", FLUX.2 / klein), and the admin can say it when adding the
  model (`image_edit`, `image_generate`).
- A catalog entry from before 1.5.5 has neither key: it is read from its pipeline class when it has one (so an
  edit-only pipeline added as an image model in 1.3 stops being sent text-to-image requests it cannot make), else it
  makes images from text and edits none, as before.
"""

from __future__ import annotations

import re
from typing import Optional

MAX_INPUTS = 16     # the most input images any model is said to take (OpenAI's own limit)

# Pipelines that edit, by model_index.json class: (input images it takes, whether it makes images from text too).
# Counts are what vLLM-Omni 0.32 lets through or what the model's card says it was trained with, at most.
EDIT_PIPELINES: dict[str, tuple[int, bool]] = {
    "Flux2KleinPipeline": (4, True),          # FLUX.2 [klein] 4B / 9B: text to image and reference edits
    "Flux2Pipeline": (4, True),               # FLUX.2 [dev]
    "FluxKontextPipeline": (1, True),         # FLUX.1 Kontext: an image is optional
    "QwenImageEditPipeline": (1, False),      # Qwen-Image-Edit
    "QwenImageEditPlusPipeline": (3, False),  # Qwen-Image-Edit-2509 / 2511: one to three images
    "LongCatImageEditPipeline": (1, False),
    "OmniGen2Pipeline": (3, True),
}


def from_pipeline(name: Optional[str]) -> tuple[int, bool]:
    """(input images it edits, whether it makes images from text) for a diffusers pipeline class."""
    n = str(name or "")
    if n in EDIT_PIPELINES:
        return EDIT_PIPELINES[n]
    if "Inpaint" in n:
        return 0, False             # needs a mask (not taken in 1.5.5)
    if "Edit" in n or "Img2Img" in n or "ImageToImage" in n or "InstructPix2Pix" in n:
        return 1, False
    return 0, True


_KLEIN = re.compile(r"flux[-_.]?2|klein")


def from_names(*names: Optional[str]) -> tuple[int, bool]:
    """The same for a stable-diffusion.cpp model file, from its file and repository names."""
    low = " ".join(str(n or "") for n in names).lower()
    if "kontext" in low:
        return 1, True
    if _KLEIN.search(low):
        return 4, True
    if "edit" in low:
        return (3, False) if re.search(r"2509|2511|plus", low) else (1, False)
    return 0, True


def _count(v) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= MAX_INPUTS else None


def of(meta: Optional[dict]) -> tuple[int, bool]:
    """(input images the model edits, whether it makes images from text), from its catalog metadata."""
    meta = meta or {}
    img = meta.get("image") if isinstance(meta.get("image"), dict) else {}
    pipeline = (meta.get("diffusers") or {}).get("pipeline") if isinstance(meta.get("diffusers"), dict) else None
    edit, generate = from_pipeline(pipeline) if pipeline else (0, True)
    if _count(img.get("edit")) is not None:
        edit = img["edit"]
    if isinstance(img.get("generate"), bool):
        generate = img["generate"]
    return edit, generate


def describe(meta: dict, *, edit: Optional[int] = None, generate: Optional[bool] = None) -> None:
    """Write `edit` and `generate` into an inspected image model's `meta.image`: from its pipeline class (diffusers),
    else from what the admin said, else from its names."""
    img = meta.get("image")
    if not isinstance(img, dict):
        return
    pipeline = (meta.get("diffusers") or {}).get("pipeline") if isinstance(meta.get("diffusers"), dict) else None
    if pipeline:
        e, g = from_pipeline(pipeline)
    else:
        e, g = from_names(meta.get("file"), meta.get("repo"))
        e = edit if edit is not None else e
        g = generate if generate is not None else g
    img["edit"], img["generate"] = e, g
