"""Which media a model takes in chat (1.3), read when it is inspected, and what a deployment of it
accepts at the gateway.

- **safetensors** (transformers `config.json`, or its `thinker_config` for Omni models): images when
  it has a `vision_config` or an image token; audio when it has an `audio_config` (or an audio token
  without one); video when it has a video token, or the repository has a
  `video_preprocessor_config.json`. Qwen3-VL, Gemma 4, InternVL 3.5, Mistral Small 3.1 and the Omni
  models are recognised this way, text-only models have none of these keys.
- **GGUF** (llama.cpp): only through a multimodal projector file (`mmproj-*.gguf`) chosen with the
  model; its header says `clip.has_vision_encoder` / `clip.has_audio_encoder`. llama-server treats
  video as frames of the vision encoder, so vision means images and video.

A catalog entry added before 1.3 has no `modalities` in its metadata: for safetensors that means
"unknown" (the gateway passes media on and the engine answers); for GGUF, no projector, so text only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from forgeyard.config import get_settings

KINDS = ("image", "audio", "video")
# When a recipe does not declare the parameters: images (and audio clips) per request, videos.
DEFAULT_IMAGES = 4
DEFAULT_VIDEOS = 0


def from_config(config: dict, paths: list[str]) -> list[str]:
    """Media a transformers model takes, from its config.json and the repository's file names."""
    cfgs = [config]
    if isinstance(config.get("thinker_config"), dict):
        cfgs.append(config["thinker_config"])

    def has(*keys: str) -> bool:
        return any(c.get(k) not in (None, {}, [], False) for c in cfgs for k in keys)

    out = []
    if has("vision_config", "image_token_id", "image_token_index"):
        out.append("image")
    if has("audio_config") or (has("audio_token_id", "audio_token_index") and not any("audio_config" in c for c in cfgs)):
        out.append("audio")
    if has("video_token_id", "video_token_index") or any(p.rsplit("/", 1)[-1] == "video_preprocessor_config.json"
                                                            for p in paths):
        out.append("video")
    return out


def vision_tile_px(config: dict) -> int:
    """The side in pixels of the square one image token covers, for vision towers whose token count
    grows with the image's resolution (Qwen2-VL, Qwen2.5-VL, Qwen3-VL: patch_size x spatial_merge_size);
    0 for the others (Gemma and similar take a fixed number of tokens per image)."""
    for c in (config, config.get("thinker_config") if isinstance(config.get("thinker_config"), dict) else {}):
        v = c.get("vision_config") if isinstance(c.get("vision_config"), dict) else {}
        patch, merge = v.get("patch_size"), v.get("spatial_merge_size")
        if isinstance(patch, int) and isinstance(merge, int) and 0 < patch <= 64 and 0 < merge <= 8:
            return patch * merge
    return 0


def from_mmproj(metadata: dict) -> list[str]:
    """Media a llama.cpp projector (mmproj GGUF) adds: vision means images and video."""
    out = []
    if metadata.get("clip.has_vision_encoder"):
        out += ["image", "video"]
    if metadata.get("clip.has_audio_encoder"):
        out.append("audio")
    return sorted(out, key=KINDS.index)


def of_model(meta: dict, fmt: str) -> Optional[list[str]]:
    """The model's media: a list (maybe empty), or None when unknown (added before 1.3)."""
    if "modalities" in (meta or {}):
        return list(meta["modalities"] or [])
    if fmt == "gguf":
        return []
    return None


@dataclass(frozen=True)
class Accepts:
    """What a deployment takes at the gateway, per request."""

    limits: tuple                 # ((kind, at most), …) for every kind it takes
    files: bool                   # `file` parts (PDF) are passed on
    max_body: int                 # bytes
    known: bool = True            # False: the model's media are unknown (a catalog entry from before 1.3)

    def limit(self, kind: str) -> int:
        return dict(self.limits).get(kind, 0)

    @property
    def kinds(self) -> list[str]:
        return [k for k, n in self.limits if n > 0]


def accepts(recipe, meta: dict, fmt: str, params: dict) -> Accepts:
    """From the recipe's `media` block, the model's media and the deployment's parameters."""
    declared = list(recipe.media.inputs) if recipe.media is not None else list(KINDS)
    model = of_model(meta, fmt)
    kinds = [k for k in declared if model is None or k in model]
    images = _count(params.get("images"), DEFAULT_IMAGES)
    videos = _count(params.get("videos"), DEFAULT_VIDEOS)
    per = {"image": images, "audio": images, "video": videos}
    limits = tuple((k, per[k]) for k in KINDS if k in kinds)
    files = bool(recipe.media is not None and recipe.media.files)
    mb = params.get("max_body_mb")
    mb = int(mb) if isinstance(mb, int) and not isinstance(mb, bool) and mb > 0 else get_settings().gateway_max_body_mb
    return Accepts(limits=limits, files=files, max_body=mb * 1024**2, known=model is not None)


def _count(value, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default
