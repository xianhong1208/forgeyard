"""Engine recipes: YAML that says how to run an inference engine for a kind of model, per
hardware variant. Adding an engine is adding a recipe, not changing Forgeyard's code.

    name: vllm                       a-z, 0-9, '-'; unique
    engine, version, title, description
    tasks: [chat]                    model tasks it serves: chat, embed, rerank, asr, tts, image, video
    formats: [safetensors]           model formats it reads: safetensors, gguf, ctranslate2, onnx,
                                     diffusers (a repository with model_index.json)
    api: {chat: openai-chat}         per task, the protocol the gateway (M4) speaks to it
    upstream_model: served_name      what the engine expects in a request's `model` field:
                                     served_name, or repo (the model's Hugging Face id)
    share: exclusive | vram          default GPU sharing
    port: 8000                       the port the engine listens on inside a container
    health: {path, start_period, interval, timeout, failure_threshold}
    estimate: {method, overhead, cpu_overhead, kv_sequences, vram}
    params: {name: {type: int|choice|size, default, min, max, choices, label, help, advanced}}
                                     advanced: true puts it in the deploy form's collapsed
                                     Advanced section (its default works untouched)
    args: [...]                      the engine's arguments, with {placeholders}
    optional_args: [{if: {param: value}, args: [...]}, {if_model: mmproj, args: [...]}]
                                     if_model: added only when the model has it (mmproj: a
                                     llama.cpp projector file, {mmproj_file}; image, audio, video:
                                     the model takes that medium)
    tts: {formats: [wav, pcm], default_format: wav, stream_formats: [audio], voices: /props, ...}
                                     1.3, text to speech (/v1/audio/speech): what the engine makes and
                                     how the gateway talks to it (see Speech below)
    image: {request: fields, output_formats: [png, jpeg, webp], max_n: 10}
                                     1.3, image generation (/v1/images/generations): how the gateway
                                     sends the request's extras (see ImageGen below)
    video: {request: vllm-omni, frames_step: 4}
                                     1.3, video generation (/v1/videos, asynchronous jobs): how the
                                     gateway asks the engine for a video (see VideoGen below)
    media: {inputs: [image, video, audio], files: false}
                                     1.3: what the engine takes in chat besides text; the gateway
                                     sends a model only the media it takes, at most `images`
                                     (images, audio clips) and `videos` per request when the
                                     recipe declares those parameters, and bodies up to its
                                     `max_body_mb` parameter (else GATEWAY_MAX_BODY_MB). `files`:
                                     `file` parts (PDF) are passed on. No media block: media are
                                     passed on and the engine answers
    env: {NAME: value}               with {placeholders}
    api_key_env: VLLM_API_KEY        the variable the engine reads its API key from (M4): each
                                     replica gets a random key there, which only the gateway
                                     sends, so the replica's port cannot be used around it
    variants:                        tried in order; the first that fits a machine is used
      - name: cuda
        when: {vendor: nvidia, compute_capability: ">=8.0", driver: ">=580"}
        runtime: container
        image: vllm/vllm-openai:v0.32.0
        entrypoint: [vllm, serve]
      - name: vulkan
        when: {vendor: amd}
        runtime: container
        amd_kfd: false                 AMD cards get only their render node, not /dev/kfd (ROCm's
                                       compute interface): Vulkan needs nothing else. Default true
      - name: cpu-process
        when: {vendor: cpu, arch: [x86_64]}
        runtime: process
        engine: {url, sha256, binary}  a pinned release (.tar.gz or .zip) the agent downloads
                                       (sha256-checked), or
        command: /opt/x/bin/server     a program already installed on the machines

Placeholders: {model_path} {model_file} {model_hub} {served_name} {host} {port} {context} {concurrency}
{kv_tokens} {gpus} {gpu_util} {gpu_layers} {threads} {dtype} {repo} {mmproj_file} {image_max_pixels}
{gpu_util_main} {gpu_util_codec} (two-stage engines: the share of each stage, see estimate.codec_overhead)
{width} {height} {max_pixels} (image models: the deployment's default size and its largest image in pixels)
{keep_seconds} (video models: how long the engine keeps a finished video, the deployment's keep_minutes and an hour)
{vae_file} {llm_file} {clip_l_file} {clip_g_file} {t5xxl_file} (image models made of separate files: the
component's path) {gpu_budget_gib} (the reservation in GiB, less the CUDA context) and any declared param.
Model paths become ${MODEL_PATH}, ${MODEL_FILE} and ${MODEL_HUB}, which the agent fills in (the
mount inside a container, the cache directory for a process).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from forgeyard.workloads.spec import ENV_KEY_RE, IMAGE_RE, RESERVED_ENV, RESERVED_ENV_PREFIXES, parse_bytes

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "recipes"
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")
PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
TASKS = ("chat", "embed", "rerank", "asr", "tts", "image", "video")
FORMATS = ("safetensors", "gguf", "ctranslate2", "onnx", "diffusers")
APIS = ("openai-chat", "openai-embeddings", "rerank", "openai-audio", "openai-speech", "openai-images",
        "openai-videos")
ESTIMATE_METHODS = ("safetensors-kv", "gguf", "files", "fixed", "diffusers")
BUILTIN_PLACEHOLDERS = {"model_path", "model_file", "model_hub", "served_name", "host", "port", "context",
                        "concurrency", "kv_tokens", "gpus", "gpu_util", "gpu_layers", "threads", "dtype", "repo",
                        "mmproj_file", "image_max_pixels", "gpu_util_main", "gpu_util_codec", "width", "height",
                        "max_pixels", "gpu_budget_gib", "keep_seconds"} | {f"{c}_file" for c in ("vae", "llm", "clip_l", "clip_g", "t5xxl")}
MEDIA_KINDS = ("image", "audio", "video")
# mmproj: a GGUF projector file; image_pixels: image tokens scale with the resolution (vision_tile_px).
# Image models (1.3): image_single (one checkpoint file with everything: -m), image_split (a diffusion
# model with separate text encoders: --diffusion-model), and each separate component file it has.
IMAGE_COMPONENTS = ("vae", "llm", "clip_l", "clip_g", "t5xxl")
MODEL_FEATURES = ("mmproj", "image_pixels") + MEDIA_KINDS + ("image_single", "image_split") + IMAGE_COMPONENTS
# Parameters every recipe has, handled by Forgeyard itself.
COMMON_PARAMS = ("gpus", "share", "vram", "extra_args")
MAX_RECIPE_BYTES = 64 * 1024


class RecipeError(Exception):
    pass


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _version_tuple(text: str) -> tuple:
    parts = re.findall(r"\d+", str(text))
    return tuple(int(p) for p in parts) if parts else ()


_CMP_RE = re.compile(r"^\s*(>=|<=|==|>|<)?\s*([0-9][0-9.]*)\s*$")


def version_ok(actual: Optional[str], constraint: Optional[str]) -> bool:
    """`">=8.0"` against `"8.6"`; dotted numbers compared numerically. No actual value: no match."""
    if not constraint:
        return True
    if actual is None or not _version_tuple(actual):
        return False
    m = _CMP_RE.match(constraint)
    if not m:
        return False
    op, want = m.group(1) or ">=", _version_tuple(m.group(2))
    have = _version_tuple(actual)
    # Compare on the constraint's precision: "580" matches driver "580.95.05".
    have = have[:len(want)] + (0,) * max(0, len(want) - len(have))
    return {">=": have >= want, ">": have > want, "<=": have <= want, "<": have < want, "==": have == want}[op]


class When(_M):
    vendor: Literal["nvidia", "amd", "cpu"]
    # AMD gfx targets ("gfx1100") or CPU machine architectures ("x86_64").
    arch: Optional[list[Annotated[str, Field(max_length=32)]]] = None
    compute_capability: Optional[str] = Field(default=None, max_length=16)  # NVIDIA, e.g. ">=8.0"
    driver: Optional[str] = Field(default=None, max_length=16)              # e.g. ">=580"

    @field_validator("compute_capability", "driver")
    @classmethod
    def _cmp(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _CMP_RE.match(v):
            raise ValueError("use a comparison such as >=8.0")
        return v


class Engine(_M):
    """A prebuilt engine the agent downloads for a process variant: a .tar.gz or .zip release, pinned
    by its SHA-256, and the program inside it."""
    url: str = Field(max_length=1024)
    sha256: str
    binary: str = Field(max_length=256)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("engine.url must be an https:// URL")
        return v

    @field_validator("sha256")
    @classmethod
    def _sha(cls, v: str) -> str:
        v = v.lower().removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", v):
            raise ValueError("engine.sha256 must be 64 hex characters")
        return v

    @field_validator("binary")
    @classmethod
    def _binary(cls, v: str) -> str:
        if v.startswith("/") or ".." in v.split("/") or not re.fullmatch(r"[A-Za-z0-9._/+-]+", v):
            raise ValueError("engine.binary is a relative path inside the archive")
        return v


class Variant(_M):
    name: str = Field(max_length=40)
    when: When
    runtime: Literal["container", "process"]
    image: Optional[str] = Field(default=None, max_length=255)
    entrypoint: Optional[list[Annotated[str, Field(max_length=256)]]] = Field(default=None, max_length=8)
    engine: Optional[Engine] = None
    command: Optional[str] = Field(default=None, max_length=1024)  # process: an installed program
    args: Optional[list[Annotated[str, Field(max_length=4096)]]] = Field(default=None, max_length=128)
    env: dict[str, Annotated[str, Field(max_length=1024)]] = Field(default_factory=dict, max_length=32)
    # AMD containers: pass /dev/kfd (ROCm/HIP) besides each card's render node. False for engines
    # that use only the render node (Vulkan), so the container gets nothing it does not use.
    amd_kfd: bool = True

    @model_validator(mode="after")
    def _runtime(self) -> "Variant":
        if not self.amd_kfd and (self.when.vendor != "amd" or self.runtime != "container"):
            raise ValueError(f"variant {self.name}: amd_kfd is for AMD container variants")
        if self.runtime == "container":
            if not self.image or not IMAGE_RE.match(self.image):
                raise ValueError(f"variant {self.name}: a container variant needs an image")
            if self.engine is not None or self.command:
                raise ValueError(f"variant {self.name}: engine and command are for process variants")
        else:
            if (self.engine is None) == (not self.command):
                raise ValueError(f"variant {self.name}: a process variant needs either an engine to download "
                                 "or the absolute path of an installed command")
            if self.command and (not self.command.startswith("/") or "\x00" in self.command):
                raise ValueError(f"variant {self.name}: command must be an absolute path")
            if self.image or self.entrypoint:
                raise ValueError(f"variant {self.name}: image and entrypoint are for container variants")
        return self

    @property
    def gpu(self) -> bool:
        return self.when.vendor != "cpu"


class Param(_M):
    type: Literal["int", "choice", "size", "float"]
    default: Union[int, float, str]
    min: Optional[float] = None
    max: Optional[float] = None
    choices: Optional[list[str]] = None
    label: Optional[str] = Field(default=None, max_length=80)
    help: Optional[str] = Field(default=None, max_length=300)
    advanced: bool = False   # shown in the deploy form's collapsed Advanced section

    @model_validator(mode="after")
    def _shape(self) -> "Param":
        if self.type == "choice" and not self.choices:
            raise ValueError("a choice parameter needs choices")
        return self

    def coerce(self, name: str, value):
        if self.type in ("int", "float"):
            if isinstance(value, bool):
                raise ValueError(f"{name} must be a number")
            try:
                n = int(value) if self.type == "int" else float(value)
                if self.type == "int" and float(value) != n:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError(f"{name} must be {'a whole number' if self.type == 'int' else 'a number'}") from None
            if self.min is not None and n < self.min:
                raise ValueError(f"{name} must be at least {self.min:g}")
            if self.max is not None and n > self.max:
                raise ValueError(f"{name} must be at most {self.max:g}")
            return n
        if self.type == "size":
            return parse_bytes(value)
        value = str(value)
        if value not in (self.choices or []):
            raise ValueError(f"{name} must be one of {', '.join(self.choices or [])}")
        return value


class Health(_M):
    path: str = "/health"
    start_period: int = Field(default=900, ge=0, le=7200)
    interval: int = Field(default=10, ge=1, le=300)
    timeout: int = Field(default=5, ge=1, le=60)
    failure_threshold: int = Field(default=3, ge=1, le=20)


class Estimate(_M):
    method: Literal[ESTIMATE_METHODS]  # type: ignore[valid-type]
    # Engine overhead per GPU (activations, CUDA graphs, workspaces) and in RAM for CPU variants.
    overhead: int = 0
    cpu_overhead: int = 0
    # KV cache for how many sequences: every concurrent one, or a single full-length one (pooling).
    kv_sequences: Literal["concurrency", "one"] = "concurrency"
    # Fixed method: the memory it takes.
    vram: Optional[int] = None
    # 1.3: on top, for a model that takes images (and video, when enabled): what the vision encoder's
    # startup profiling (vLLM) or the projector's compute buffers (llama.cpp) take. The vision
    # weights are inside the model's own (safetensors) or the mmproj file (GGUF, counted separately).
    vision_overhead: int = 0
    video_overhead: int = 0
    # 1.3, two-stage speech engines (vLLM-Omni): what the second stage (the audio decoder, code2wav)
    # takes on the same card, with its own CUDA context. The first stage (the talker) is estimated
    # like any LLM; {gpu_util_main} and {gpu_util_codec} split the reservation between them.
    codec_overhead: int = 0
    # 1.3, text to speech on llama.cpp: each concurrent request (slot) keeps its own audio pipeline (the
    # code predictor and the decoder's buffers), counted per `concurrency` for tts models.
    speech_overhead: int = 0
    # 1.3, image generation (method diffusers): what one image takes while it is made, per megapixel of
    # its size (the denoiser's activations; `parallel`: the images of a request are made together, so
    # they add up; else one after another), and what decoding it takes (the VAE, per megapixel, once).
    activation_per_mp: int = 0
    vae_per_mp: int = 0
    parallel: bool = True
    # Weights the engine holds in 16 bits whatever the file stores (vLLM-Omni casts float32 to bfloat16);
    # False: the files' own size (stable-diffusion.cpp keeps a GGUF's quantisation).
    cast_16bit: bool = False
    # 1.3, video generation (method diffusers): the denoiser's activations per thousand tokens of the
    # longest, largest video the deployment allows (tokens = latent frames x the frame's patches); the
    # VAE's decode is `vae_per_mp` per megapixel of one frame (it decodes a few frames at a time).
    activation_per_ktoken: int = 0

    @field_validator("overhead", "cpu_overhead", "vram", "vision_overhead", "video_overhead", "codec_overhead",
                     "speech_overhead", "activation_per_mp", "vae_per_mp", "activation_per_ktoken", mode="before")
    @classmethod
    def _size(cls, v):
        return None if v is None else (0 if v in (0, "0") else parse_bytes(v))


class OptionalArgs(_M):
    if_: dict[str, Union[str, int]] = Field(default_factory=dict, alias="if")
    if_model: Optional[Literal[MODEL_FEATURES]] = None  # type: ignore[valid-type]
    args: list[Annotated[str, Field(max_length=4096)]] = Field(max_length=32)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _condition(self) -> "OptionalArgs":
        if not self.if_ and self.if_model is None:
            raise ValueError("optional_args: each needs `if` or `if_model`")
        return self


class Media(_M):
    """What the engine takes in chat besides text (1.3)."""
    inputs: list[Literal[MEDIA_KINDS]] = Field(default_factory=list)  # type: ignore[valid-type]
    files: bool = False


SPEECH_FORMATS = ("mp3", "opus", "aac", "flac", "wav", "pcm")
STREAM_FORMATS = ("audio", "sse")


class Speech(_M):
    """Text to speech (1.3): what the engine answers at /v1/audio/speech, and what the gateway fills in.

        formats: [mp3, flac, wav, pcm]  the response_format values it makes; others get a 400 at the gateway
        default_format: mp3             sent when a request names none (so the gateway knows what comes back)
        stream_formats: [audio]         stream_format values it takes: audio (raw bytes as they are made), sse
        voices: /v1/audio/voices        the GET path that lists its voices (llama.cpp: /props), or none
        default_voice: af_heart         the voice for requests that name none, or an OpenAI voice the model
                                        does not have (else the first voice the engine lists)
        language_field: lang            set from the text's script when a request gives none (Chinese,
                                        Japanese, Korean): engines that otherwise assume English
        frames_field: n_predict         the most audio frames, derived from the input's length when a
                                        request gives none (engines with a small fixed default)
        cjk: false                      the engine cannot speak Chinese, Japanese or Korean (speaches
                                        deletes those characters): such input is refused with a 400
        pcm_rate: 24000                 the sample rate of `pcm` answers that do not say theirs
    """
    formats: list[Literal[SPEECH_FORMATS]] = Field(default_factory=lambda: ["wav"], min_length=1)  # type: ignore[valid-type]
    default_format: Literal[SPEECH_FORMATS] = "wav"  # type: ignore[valid-type]
    stream_formats: list[Literal[STREAM_FORMATS]] = Field(default_factory=list)  # type: ignore[valid-type]
    voices: Optional[str] = Field(default=None, max_length=128)
    default_voice: Optional[str] = Field(default=None, max_length=64)
    language_field: Optional[Literal["lang", "language"]] = None
    frames_field: Optional[Literal["n_predict", "max_new_tokens"]] = None
    cjk: bool = True
    pcm_rate: int = Field(default=24000, ge=8000, le=192000)

    @model_validator(mode="after")
    def _shape(self) -> "Speech":
        if self.default_format not in self.formats:
            raise ValueError("tts.default_format must be one of tts.formats")
        if self.voices is not None and not re.fullmatch(r"/[A-Za-z0-9/_.-]{0,127}", self.voices):
            raise ValueError("tts.voices is a path such as /v1/audio/voices")
        return self


IMAGE_FORMATS = ("png", "jpeg", "webp")


class ImageGen(_M):
    """Image generation (1.3): how the gateway talks to the engine at /v1/images/generations.

        request: fields                 the extras (negative_prompt, num_inference_steps, guidance_scale,
                                        seed) go as fields of the request (vLLM-Omni); sd-cpp: inside the
                                        prompt as <sd_cpp_extra_args>{…}</sd_cpp_extra_args>
                                        (stable-diffusion.cpp's sd-server), built by the gateway
        output_formats: [png, jpeg]     the output_format values it makes
        max_n: 10                       the most images the engine makes for one request
    """
    request: Literal["fields", "sd-cpp"] = "fields"
    output_formats: list[Literal[IMAGE_FORMATS]] = Field(default_factory=lambda: ["png"], min_length=1)  # type: ignore[valid-type]
    max_n: int = Field(default=10, ge=1, le=64)


class VideoGen(_M):
    """Video generation (1.3): how the gateway asks the engine for a video at /v1/videos.

        request: vllm-omni              vLLM-Omni's asynchronous jobs: POST /v1/videos (multipart) answers
                                        a job id; GET /v1/videos/{id} its status; GET …/content the MP4;
                                        DELETE /v1/videos/{id} cancels it or deletes its file
        frames_step: 4                  the engine makes k x frames_step + 1 frames (Wan's VAE packs four
                                        frames into one latent frame): the gateway rounds seconds x fps to
                                        that, so what is made and counted is known
    """
    request: Literal["vllm-omni"] = "vllm-omni"
    frames_step: int = Field(default=4, ge=1, le=16)


class Recipe(_M):
    name: str
    title: str = Field(max_length=80)
    description: str = Field(default="", max_length=600)
    engine: str = Field(max_length=40)
    version: str = Field(max_length=40)
    tasks: list[Literal[TASKS]] = Field(min_length=1)  # type: ignore[valid-type]
    formats: list[Literal[FORMATS]] = Field(min_length=1)  # type: ignore[valid-type]
    api: dict[Literal[TASKS], Literal[APIS]]  # type: ignore[valid-type]
    upstream_model: Literal["served_name", "repo"] = "served_name"
    share: Literal["exclusive", "vram"] = "exclusive"
    port: int = Field(default=8000, ge=1, le=65535)
    health: Health = Field(default_factory=Health)
    estimate: Estimate
    params: dict[str, Param] = Field(default_factory=dict, max_length=24)
    args: list[Annotated[str, Field(max_length=4096)]] = Field(default_factory=list, max_length=128)
    optional_args: list[OptionalArgs] = Field(default_factory=list, max_length=16)
    media: Optional[Media] = None
    tts: Optional[Speech] = None
    image: Optional[ImageGen] = None
    video: Optional[VideoGen] = None
    env: dict[str, Annotated[str, Field(max_length=1024)]] = Field(default_factory=dict, max_length=32)
    # Where the engine reads its API key (M4). None: the engine has no key and its port answers
    # anyone who can reach it (the gateway still works).
    api_key_env: Optional[str] = Field(default=None, max_length=64)
    variants: list[Variant] = Field(min_length=1, max_length=16)
    builtin: bool = False

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("name: 1-40 characters, a-z, 0-9 and '-'")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "Recipe":
        missing = [t for t in self.tasks if t not in self.api]
        if missing:
            raise ValueError(f"api: say which protocol serves {', '.join(missing)}")
        names = [v.name for v in self.variants]
        if len(set(names)) != len(names):
            raise ValueError("variants: names must be unique")
        for p in self.params:
            if p in COMMON_PARAMS or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", p):
                raise ValueError(f"params: {p!r} is reserved or not a valid name")
        known = BUILTIN_PLACEHOLDERS | set(self.params)
        texts = list(self.args) + list(self.env.values())
        for oa in self.optional_args:
            texts += oa.args
            for k in oa.if_:
                if k not in self.params:
                    raise ValueError(f"optional_args: {k!r} is not a parameter")
        for v in self.variants:
            texts += list(v.args or []) + list(v.env.values())
        for text in texts:
            for ph in PLACEHOLDER_RE.findall(text):
                if ph not in known:
                    raise ValueError(f"unknown placeholder {{{ph}}}")
        envs = dict(self.env)
        for v in self.variants:
            envs.update(v.env)
        for key in envs:
            if not ENV_KEY_RE.match(key) or key in RESERVED_ENV or key.startswith(RESERVED_ENV_PREFIXES):
                raise ValueError(f"env: {key} cannot be set by a recipe")
        for name, p in self.params.items():
            try:
                p.coerce(name, p.default)
            except ValueError as e:
                raise ValueError(f"params.{name}: the default is not valid ({e})") from None
        if self.api_key_env is not None:
            key = self.api_key_env
            if not ENV_KEY_RE.match(key) or key in RESERVED_ENV or key.startswith(RESERVED_ENV_PREFIXES):
                raise ValueError(f"api_key_env: {key} cannot be used")
            if key in envs:
                raise ValueError(f"api_key_env: {key} is also set in env; Forgeyard sets it per replica")
        if self.estimate.method == "fixed" and not self.estimate.vram:
            raise ValueError("estimate: the fixed method needs vram")
        if self.tts is not None and "tts" not in self.tasks:
            raise ValueError("tts: only for recipes that serve the tts task")
        if "tts" in self.tasks and self.api.get("tts") != "openai-speech":
            raise ValueError("api: tts is served as openai-speech")
        if self.image is not None and "image" not in self.tasks:
            raise ValueError("image: only for recipes that serve the image task")
        if "image" in self.tasks and self.api.get("image") != "openai-images":
            raise ValueError("api: image is served as openai-images")
        if "image" in self.tasks and self.estimate.method not in ("diffusers", "fixed"):
            raise ValueError("estimate: an image recipe uses the diffusers (or fixed) method")
        if self.video is not None and "video" not in self.tasks:
            raise ValueError("video: only for recipes that serve the video task")
        if "video" in self.tasks and self.api.get("video") != "openai-videos":
            raise ValueError("api: video is served as openai-videos")
        if "video" in self.tasks and self.estimate.method not in ("diffusers", "fixed"):
            raise ValueError("estimate: a video recipe uses the diffusers (or fixed) method")
        if "video" in self.tasks and len(self.tasks) > 1:
            raise ValueError("tasks: a video recipe serves only the video task (its jobs have their own API)")
        return self

    @property
    def imagegen(self) -> ImageGen:
        """Its image settings (the defaults when it declares none)."""
        return self.image or ImageGen()

    @property
    def videogen(self) -> VideoGen:
        """Its video settings (the defaults when it declares none)."""
        return self.video or VideoGen()

    @property
    def speech(self) -> Speech:
        """Its speech settings (the defaults when it declares none)."""
        return self.tts or Speech()

    def api_for(self, task: str) -> str:
        return self.api[task]

    def variant(self, name: str) -> Optional[Variant]:
        return next((v for v in self.variants if v.name == name), None)


def parse_recipe(text: str, *, builtin: bool = False) -> Recipe:
    if len(text.encode("utf-8")) > MAX_RECIPE_BYTES:
        raise RecipeError("a recipe is at most 64 KB")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RecipeError(f"not valid YAML: {str(e).splitlines()[0]}") from None
    if not isinstance(data, dict):
        raise RecipeError("a recipe is a YAML mapping")
    data["builtin"] = builtin
    try:
        return Recipe.model_validate(data)
    except ValidationError as e:
        err = e.errors()[0]
        loc = ".".join(str(p) for p in err.get("loc", ()))
        raise RecipeError(f"{loc}: {str(err.get('msg', 'invalid')).removeprefix('Value error, ')}") from None


_builtin_cache: Optional[dict[str, tuple[Recipe, str]]] = None


def builtin_recipes() -> dict[str, tuple[Recipe, str]]:
    """{name: (recipe, its YAML)} for the recipes shipped with Forgeyard."""
    global _builtin_cache
    if _builtin_cache is None:
        out = {}
        for path in sorted(BUILTIN_DIR.glob("*.yaml")):
            text = path.read_text(encoding="utf-8")
            recipe = parse_recipe(text, builtin=True)
            out[recipe.name] = (recipe, text)
        _builtin_cache = dict(sorted(out.items()))
    return _builtin_cache


# ------------------------------------------------------------------ matching hardware
def variant_matches(variant: Variant, accel: dict) -> Optional[str]:
    """Why this variant cannot run on this accelerator (a dict like the inventory's), or None."""
    w = variant.when
    if accel.get("vendor") != w.vendor:
        return f"needs {w.vendor}"
    arch = accel.get("arch")
    if w.vendor == "nvidia":
        if w.compute_capability and not version_ok(arch, w.compute_capability):
            return f"compute capability {arch or '?'} is not {w.compute_capability}"
    elif w.arch and arch not in w.arch:
        return f"{arch or 'unknown architecture'} is not one of {', '.join(w.arch)}"
    if w.driver and not version_ok(accel.get("driver"), w.driver):
        return f"driver {accel.get('driver') or 'unknown'} is not {w.driver}"
    return None


def render_text(text: str, values: dict) -> str:
    """Fill {placeholders}; anything else in braces (JSON, Jinja) is left alone."""
    return PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)
