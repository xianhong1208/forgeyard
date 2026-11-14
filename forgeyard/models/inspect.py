"""Look at a model before anyone downloads it: which files, how large, and what the estimator
needs (architecture from config.json, parameter counts from the safetensors headers, the GGUF
metadata). Runs on the server, over the guarded fetcher; a token (for gated repositories) is used
for these requests only and never stored here.
"""

from __future__ import annotations

import json
from typing import Optional
from urllib.parse import quote

from forgeyard.models import fetch, gguf, modalities, safetensors, sources
from forgeyard.models.sources import Source, SourceError

MAX_SHARDS = 256


class InspectError(Exception):
    pass


def _auth(token: Optional[str]) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _hf_error(e: fetch.FetchError, repo: str) -> InspectError:
    if e.status in (401, 403):
        return InspectError(f"{repo} is gated or private: give a Hugging Face token that may read it")
    if e.status == 404:
        return InspectError(f"{repo} (or that revision) does not exist on Hugging Face")
    return InspectError(str(e))


def config_summary(config: dict) -> dict:
    """The fields the KV-cache estimate needs, from a transformers config.json (multimodal
    configs keep them in text_config; a speech-only model such as Qwen3-TTS in talker_config, the
    language model that writes the audio codes; Omni chat models, which also have a talker, are read as
    before)."""
    text = config
    if isinstance(config.get("text_config"), dict):
        text = config["text_config"]
    elif isinstance(config.get("talker_config"), dict) and "thinker_config" not in config:
        text = config["talker_config"]
    heads = int(text.get("num_attention_heads") or 0)
    hidden = int(text.get("hidden_size") or text.get("d_model") or 0)
    head_dim = text.get("head_dim") or (hidden // heads if heads else 0)
    return {
        "architectures": config.get("architectures") or [],
        "model_type": config.get("model_type"),
        "layers": int(text.get("num_hidden_layers") or text.get("n_layer") or 0),
        "heads": heads,
        "kv_heads": int(text.get("num_key_value_heads") or heads or 0),
        "head_dim": int(head_dim or 0),
        "hidden_size": hidden,
        "vocab": int(text.get("vocab_size") or 0),
        "max_context": int(text.get("max_position_embeddings") or 0),
        "dtype": str(config.get("torch_dtype") or config.get("dtype") or text.get("torch_dtype") or "bfloat16"),
        "quantization": (config.get("quantization_config") or {}).get("quant_method"),
    }


MAX_VOICES = 200


def voices_of(config: dict) -> list[str]:
    """The built-in voices a speech model's config.json names (Qwen3-TTS CustomVoice:
    talker_config.spk_id), in the order the file lists them; [] when it names none."""
    talker = config.get("talker_config") if isinstance(config.get("talker_config"), dict) else {}
    spk = talker.get("spk_id")
    if not isinstance(spk, dict):
        return []
    return [str(k)[:64] for k in spk if isinstance(k, str) and k.strip()][:MAX_VOICES]


def license_of(info: dict) -> Optional[str]:
    """The licence a Hugging Face repository declares: its card's `license` (or `license_name` for
    "other"), else a `license:` tag. Lower case, as Hugging Face writes them (apache-2.0, mit, gemma…)."""
    card = info.get("cardData") if isinstance(info.get("cardData"), dict) else {}
    lic = card.get("license")
    if isinstance(lic, list):
        lic = lic[0] if lic else None
    if lic == "other" and isinstance(card.get("license_name"), str):
        lic = card["license_name"]
    if not isinstance(lic, str) or not lic.strip():
        lic = next((t.split(":", 1)[1] for t in info.get("tags") or [] if isinstance(t, str) and t.startswith("license:")),
                   None)
    return lic.strip().lower()[:64] if isinstance(lic, str) and lic.strip() else None


MAX_COMPONENT_SHARDS = 64
VAE_SCALE = 8


def _check_components(given: dict, files: list[dict]) -> dict:
    """Component files named by the caller (an image model for stable-diffusion.cpp): known roles,
    files of the repository."""
    paths = {f["path"] for f in files}
    out = {}
    for role, path in (given or {}).items():
        if role not in recipes_components():
            raise InspectError(f"components: {role!r} is not one of {', '.join(recipes_components())}")
        if path in (None, "", "none"):
            continue
        if not isinstance(path, str) or path not in paths:
            raise InspectError(f"components.{role}: {path} is not a file of this repository")
        out[role] = path
    return out


def recipes_components() -> tuple:
    from forgeyard.models.recipes import IMAGE_COMPONENTS

    return IMAGE_COMPONENTS


def _image_files(out: dict, files: list[dict], fmt: str, file: Optional[str], components: Optional[dict]) -> None:
    """An image model for stable-diffusion.cpp (1.3): one checkpoint with everything in it (Stable
    Diffusion 1.x / 2.x / XL: `-m`), or a diffusion model with its text encoder(s) and VAE beside it in
    the same repository (Z-Image, FLUX: `--diffusion-model`, `--llm` or `--t5xxl` / `--clip_l`, `--vae`).
    Read from the file names only: the sizes are what the estimate needs."""
    choices = sources.image_file_choices(files, fmt)
    out["gguf_files" if fmt == "gguf" else "image_files"] = choices
    chosen = file or (choices[0]["path"] if len(choices) == 1 else sources.default_gguf(choices))
    if chosen is None:
        raise InspectError("this repository has several model files: choose one")
    if chosen not in {c["path"] for c in choices}:
        raise InspectError(f"{chosen} is not one of its model files")
    comps = _check_components(components, files) if components is not None else sources.image_components(files, chosen)
    side, multiple = sources.image_size_guess(chosen)
    split = any(r in comps for r in ("llm", "t5xxl", "clip_l", "clip_g"))
    if split:
        side, multiple = max(side, 1024), 16
    out["file"] = chosen
    out["task_hint"] = "image"
    out["modalities"] = []
    out["image"] = {"size": side, "multiple": multiple, "components": comps, "single": not split}


# Text-to-video pipelines (1.3, vLLM-Omni's /v1/videos): their model_index.json class. Image-to-video ones
# (WanImageToVideoPipeline) need an image with every request, which the gateway does not send in 1.3.
VIDEO_PIPELINES = ("WanPipeline",)


def is_video_pipeline(name: Optional[str]) -> bool:
    n = str(name or "")
    return n in VIDEO_PIPELINES or (n.endswith("VideoPipeline") and "ImageToVideo" not in n and "VideoToVideo" not in n)


def _video_native(index: dict, url, token: Optional[str], policy: fetch.Policy) -> dict:
    """A video pipeline's own size, frame rate and the multiple its sizes take (1.3). Wan's VAE packs
    8 x 8 pixels (16 x 16 for Wan2.2's, `scale_factor_spatial`) and its transformer 2 x 2 of those into
    a token: Wan2.1 makes 832x480 at 16 fps, sizes a multiple of 16; Wan2.2-TI2V-5B (expand_timesteps)
    1280x704 at 24 fps, a multiple of 32."""
    spatial, patch = 8, 2
    try:
        vae = json.loads(fetch.get_bytes(url("vae/config.json"), policy, headers=_auth(token), limit=1024**2))
        if isinstance(vae, dict) and isinstance(vae.get("scale_factor_spatial"), int) and 1 <= vae["scale_factor_spatial"] <= 64:
            spatial = vae["scale_factor_spatial"]
        tr = json.loads(fetch.get_bytes(url("transformer/config.json"), policy, headers=_auth(token), limit=1024**2))
        ps = tr.get("patch_size") if isinstance(tr, dict) else None
        if isinstance(ps, list) and len(ps) == 3 and all(isinstance(x, int) and 1 <= x <= 8 for x in ps):
            patch = ps[1]
    except (fetch.FetchError, ValueError):
        pass
    wan22 = bool(index.get("expand_timesteps")) or spatial == 16
    size, fps = ([1280, 704], 24) if wan22 else ([832, 480], 16)
    return {"size": size, "multiple": spatial * patch, "fps": fps}


def _diffusers(out: dict, files: list[dict], url, ranged, token: Optional[str], policy: fetch.Policy) -> None:
    """A diffusers pipeline (1.3, vLLM-Omni): model_index.json says which components it loads; each
    one's safetensors headers give its parameters (the estimate counts them in 16 bits, as the engine
    holds them), and the UNet's or transformer's config its native size. A text-to-video pipeline
    (WanPipeline) is a video model: its native size, frame rate and size multiple instead."""
    index = json.loads(fetch.get_bytes(url("model_index.json"), policy, headers=_auth(token), limit=1024**2))
    if not isinstance(index, dict):
        raise InspectError("model_index.json is not an object")
    names = sources.diffusers_components(index)
    selected = sources.select_diffusers(files, names)
    comps: dict = {}
    for name in names:
        shards = [f["path"] for f in selected if f["path"].startswith(name + "/") and f["path"].endswith(".safetensors")]
        if not shards:
            continue
        if len(shards) > MAX_COMPONENT_SHARDS:
            raise InspectError(f"{name}: more than {MAX_COMPONENT_SHARDS} safetensors files")
        w = safetensors.merge([safetensors.read_remote(ranged(p)) for p in shards])
        params = sum(w["params"].values())
        dtype = max(w["params"], key=w["params"].get) if w["params"] else None
        comps[name] = {"class": str(index[name][1])[:64], "params": params, "bytes": w["bytes"], "dtype": dtype}
    if not comps:
        raise InspectError("model_index.json names no component with safetensors weights")
    pipeline = str(index.get("_class_name") or "")[:64] or None
    if is_video_pipeline(pipeline):
        out["diffusers"] = {"pipeline": pipeline, "components": comps}
        out["video"] = _video_native(index, url, token, policy)
        out["task_hint"] = "video"
        out["modalities"] = []
        out["files"] = selected
        return
    side, multiple = 1024, 16
    if "unet" in comps:
        multiple = 64
        try:
            cfg = json.loads(fetch.get_bytes(url("unet/config.json"), policy, headers=_auth(token), limit=1024**2))
            sample = cfg.get("sample_size") if isinstance(cfg, dict) else None
            if isinstance(sample, int) and 16 <= sample <= 512:
                side = sample * VAE_SCALE
        except (fetch.FetchError, ValueError):
            side = 512
    out["diffusers"] = {"pipeline": str(index.get("_class_name") or "")[:64] or None, "components": comps}
    out["image"] = {"size": side, "multiple": multiple}
    out["modalities"] = []
    out["files"] = selected


def inspect_hf(repo: str, ref: Optional[str], file: Optional[str], *, endpoint: str, policy: fetch.Policy,
               token: Optional[str] = None, mmproj: Optional[str] = None, task: Optional[str] = None,
               components: Optional[dict] = None) -> dict:
    """`mmproj` (GGUF only): the projector file to use; None picks one when the repository has
    any, "none" uses none. `task` (1.3): "image" reads a GGUF or safetensors repository as an image
    model for stable-diffusion.cpp (its file and the `components` beside it: found by name when not
    given)."""
    ref = (ref or "main").strip()
    if not (sources.REVISION_RE.match(ref) or sources.REF_RE.match(ref)) or ".." in ref:
        raise InspectError("revision: a branch, tag or commit")
    ep = endpoint.rstrip("/")
    try:
        info = json.loads(fetch.get_bytes(f"{ep}/api/models/{repo}/revision/{quote(ref, safe='')}?blobs=true",
                                          policy, headers=_auth(token), limit=32 * 1024**2))
    except fetch.FetchError as e:
        raise _hf_error(e, repo) from None
    except ValueError:
        raise InspectError("Hugging Face answered with something that is not JSON") from None
    sha = info.get("sha")
    if not isinstance(sha, str) or not sources.REVISION_RE.match(sha):
        raise InspectError("Hugging Face did not say which commit that revision is")
    files = []
    for s in info.get("siblings") or []:
        path = s.get("rfilename")
        try:
            path = sources.safe_relpath(path)
        except SourceError:
            continue
        entry = {"path": path, "size": int(s.get("size") or 0)}
        lfs = s.get("lfs") or {}
        if lfs.get("sha256"):
            entry["sha256"] = lfs["sha256"]
            entry["size"] = int(lfs.get("size") or entry["size"])
        elif s.get("blobId"):
            entry["oid"] = s["blobId"]
        files.append(entry)
    paths = [f["path"] for f in files]
    fmt = sources.detect_format(paths, info.get("library_name"))
    out = {
        "kind": "hf", "repo": repo, "ref": ref, "revision": sha, "format": fmt,
        "pipeline_tag": info.get("pipeline_tag"), "library": info.get("library_name"),
        "task_hint": sources.task_hint(info.get("pipeline_tag"), info.get("tags") or []) or (
            "chat" if fmt == "gguf" else None),
        "gated": bool(info.get("gated")), "private": bool(info.get("private")),
        "license": license_of(info),
    }

    def url(path: str) -> str:
        return f"{ep}/{repo}/resolve/{sha}/{quote(path)}"

    def ranged(path: str):
        return lambda a, b: fetch.get_range(url(path), policy, a, b, headers=_auth(token))

    image = (task or out["task_hint"]) == "image"
    if fmt == "diffusers":
        out["task_hint"] = "image"
    try:
        if fmt == "diffusers":
            _diffusers(out, files, url, ranged, token, policy)
        elif image and fmt in ("gguf", "safetensors"):
            _image_files(out, files, fmt, file, components)
        elif fmt == "gguf":
            choices = sources.gguf_choices(files)
            out["gguf_files"] = choices
            chosen = file or sources.default_gguf(choices)
            if chosen is None:
                raise InspectError("this repository has several GGUF files: choose one")
            if chosen not in {c["path"] for c in choices}:
                raise InspectError(f"{chosen} is not one of its GGUF files")
            out["file"] = chosen
            out["gguf"] = gguf.read_remote(ranged(chosen)).summary()
            projectors = sources.mmproj_choices(files)
            out["mmproj_files"] = projectors
            pick = sources.default_mmproj(projectors) if mmproj is None else (None if mmproj in ("", "none") else mmproj)
            if pick is not None and pick not in {p["path"] for p in projectors}:
                raise InspectError(f"{pick} is not one of its projector (mmproj) files")
            out["modalities"] = []
            if pick:
                header = gguf.read_remote(ranged(pick))
                size = next(p["size"] for p in projectors if p["path"] == pick)
                out["mmproj"] = {"path": pick, "size": size,
                                 "projector": str(header.metadata.get("clip.projector_type") or "")[:40] or None}
                out["modalities"] = modalities.from_mmproj(header.metadata)
        elif fmt == "safetensors":
            config = json.loads(fetch.get_bytes(url("config.json"), policy, headers=_auth(token), limit=4 * 1024**2))
            out["config"] = config_summary(config)
            out["modalities"] = modalities.from_config(config, paths)
            if voices_of(config):
                out["voices"] = voices_of(config)
            if modalities.vision_tile_px(config):
                out["vision_tile_px"] = modalities.vision_tile_px(config)
            shards = [p for p in paths if p.endswith(".safetensors") and "/" not in p]
            if not shards:
                shards = [p for p in paths if p.endswith(".safetensors")]
            if len(shards) > MAX_SHARDS:
                raise InspectError(f"more than {MAX_SHARDS} safetensors files")
            out["weights"] = safetensors.merge([safetensors.read_remote(ranged(p)) for p in shards])
        elif fmt == "ctranslate2":
            out["weights_bytes"] = sum(f["size"] for f in files if f["path"] == "model.bin")
        elif fmt == "onnx":
            # speaches' Kokoro (model.onnx + voices.bin) and Piper (model.onnx + config.json) models.
            out["weights_bytes"] = sum(f["size"] for f in files
                                       if f["path"].lower().endswith((".onnx", ".onnx_data", ".bin")))
            if "config.json" in paths:   # Piper: the voice's sample rate (its pcm answers say none)
                config = json.loads(fetch.get_bytes(url("config.json"), policy, headers=_auth(token), limit=1024**2))
                rate = (config.get("audio") or {}).get("sample_rate") if isinstance(config, dict) else None
                if isinstance(rate, int) and 8000 <= rate <= 192000:
                    out["sample_rate"] = rate
        else:
            raise InspectError("no safetensors, GGUF, CTranslate2, ONNX or diffusers weights in this repository")
    except fetch.FetchError as e:
        raise _hf_error(e, repo) from None
    except (gguf.GGUFError, safetensors.SafetensorsError, ValueError) as e:
        raise InspectError(str(e)) from None
    if fmt == "diffusers":
        selected = out["files"]
    elif "image" in out:
        selected = sources.select_files(files, fmt, out["file"], components=out["image"]["components"])
    else:
        selected = sources.select_files(files, fmt, out.get("file"), (out.get("mmproj") or {}).get("path"))
    out["files"] = selected
    out["size_bytes"] = sum(f["size"] for f in selected)
    if "image" in out and fmt != "diffusers":
        # What each part weighs, for the estimate: the model file (all parts of a split GGUF) and each component.
        by_path = {f["path"]: f["size"] for f in selected}
        comps = out["image"]["components"]
        out["image"]["bytes"] = {"model": sum(v for k, v in by_path.items() if k not in comps.values()),
                                 **{r: by_path.get(p, 0) for r, p in comps.items()}}
    if fmt == "safetensors" and "weights" in out and out["weights"]["bytes"] == 0:
        out["weights"]["bytes"] = sum(f["size"] for f in selected if f["path"].endswith(".safetensors"))
    return out


def inspect_url(url: str, *, policy: fetch.Policy) -> dict:
    name = sources.url_filename(url)
    try:
        header = gguf.read_remote(lambda a, b: fetch.get_range(url, policy, a, b))
        with fetch.open_url(url, policy, headers={"Range": "bytes=0-0"}) as resp:
            total = resp.headers.get("Content-Range", "").rsplit("/", 1)[-1]
        size = int(total) if total.isdigit() else header.data_offset + header.tensor_bytes
    except fetch.FetchError as e:
        raise InspectError(str(e)) from None
    except gguf.GGUFError as e:
        raise InspectError(str(e)) from None
    return {"kind": "url", "url": url, "format": "gguf", "file": name, "gguf": header.summary(),
            "files": [{"path": name, "size": size}], "size_bytes": size, "task_hint": "chat"}


def inspect_source(src: Source, *, ref: Optional[str], file: Optional[str], endpoint: str, policy: fetch.Policy,
                   url_policy: fetch.Policy, local_dirs: list[str], token: Optional[str] = None,
                   fmt: Optional[str] = None, size_bytes: Optional[int] = None, mmproj: Optional[str] = None,
                   task: Optional[str] = None, components: Optional[dict] = None) -> dict:
    if src.kind == "hf":
        return inspect_hf(src.repo, ref, file, endpoint=endpoint, policy=policy, token=token, mmproj=mmproj,
                          task=task, components=components)
    if src.kind == "url":
        return inspect_url(src.url, policy=url_policy)
    if not sources.local_allowed(src.path, local_dirs):
        raise InspectError(f"{src.path} is not under a directory allowed for local models "
                           f"({', '.join(local_dirs) or 'none configured'})")
    if fmt not in ("safetensors", "gguf", "ctranslate2", "onnx", "diffusers"):
        raise InspectError("a local model needs its format (safetensors, gguf, ctranslate2, onnx or diffusers)")
    if fmt == "gguf" and not file:
        raise InspectError("a local GGUF model needs the file name")
    # The server cannot read the machine's disk: deployments of local models declare their memory.
    return {"kind": "local", "path": src.path, "format": fmt, "file": file, "files": [],
            "size_bytes": int(size_bytes or 0), "declared_only": True}
