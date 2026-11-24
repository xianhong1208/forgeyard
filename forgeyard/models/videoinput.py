"""What a video model does with a start image (1.5.5, #143): kept in the catalog entry's `meta.video.image_input` as
`none` (text to video only), `optional` (text, or text and a start image) or `required` (every request has a start
image). No new task and no migration.

- **The diffusers pipeline says it** (model_index.json's `_class_name`): `WanPipeline` with `expand_timesteps`
  (Wan2.2-TI2V-5B) takes an optional start image; the image-to-video pipelines vLLM-Omni 0.32.0 runs
  (`WanImageToVideoPipeline`, `SanaImageToVideoPipeline`, `HunyuanVideo15ImageToVideoPipeline`) need one; any other
  text-to-video pipeline takes none. Other image-to-video and video-to-video pipelines are not video models here.
- A catalog entry from before 1.5.5 has no key: it is read from its pipeline class, and a `WanPipeline` whose sizes
  are multiples of 32 is Wan2.2-TI2V-5B (its VAE packs 16 x 16 pixels, Wan2.1's and Wan2.2-T2V-A14B's 8 x 8), so a
  TI2V-5B deployment added in 1.3 takes start images after the upgrade without being added again.
"""

from __future__ import annotations

from typing import Optional

KINDS = ("none", "optional", "required")

# Image-to-video pipelines vLLM-Omni 0.32.0 lists (docs/models/supported_models.md at v0.32.0); each needs an image.
REQUIRED_PIPELINES = ("WanImageToVideoPipeline", "SanaImageToVideoPipeline", "HunyuanVideo15ImageToVideoPipeline")
TI2V_PIPELINES = ("WanPipeline",)   # with expand_timesteps (Wan2.2-TI2V-5B): text, or text and a start image


def from_pipeline(name: Optional[str], *, expand_timesteps: bool = False) -> str:
    """none | optional | required for a video pipeline class (model_index.json's `_class_name` and its
    `expand_timesteps`)."""
    n = str(name or "")
    if n in REQUIRED_PIPELINES:
        return "required"
    if n in TI2V_PIPELINES and expand_timesteps:
        return "optional"
    return "none"


def _pipeline(meta: dict) -> Optional[str]:
    d = meta.get("diffusers")
    return d.get("pipeline") if isinstance(d, dict) else None


def of(meta: Optional[dict]) -> str:
    """none | optional | required, from a video model's catalog metadata."""
    meta = meta or {}
    v = meta.get("video") if isinstance(meta.get("video"), dict) else {}
    if v.get("image_input") in KINDS:
        return v["image_input"]
    pipeline = _pipeline(meta)
    return from_pipeline(pipeline, expand_timesteps=pipeline in TI2V_PIPELINES and v.get("multiple") == 32)


def takes_image(meta: Optional[dict]) -> bool:
    return of(meta) != "none"
