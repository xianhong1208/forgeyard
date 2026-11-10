"""The recommended models (forgeyard/models/recommended.yaml): a list people pick from on Add a model
instead of having to know repository names. Read and checked once; a broken file is an error at
the first request (and in the tests), never a half-shown list.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from forgeyard.models import recipes, sources

FILE = Path(__file__).with_name("recommended.yaml")
MODEL_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]{0,62}[a-z0-9])?$")


class RecommendedError(ValueError):
    pass


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9-]{1,32}$")
    title: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=300)


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    title: str = Field(min_length=1, max_length=120)
    source: str
    revision: Optional[str] = None
    file: Optional[str] = None
    group: str
    task: Literal["chat", "embed", "rerank", "asr"]
    format: Literal["safetensors", "gguf", "ctranslate2"]
    recipe: str
    device: Literal["gpu", "cpu"] = "gpu"
    download_gib: float = Field(gt=0, le=10_000)
    memory_gib: float = Field(gt=0, le=10_000)
    gated: bool = False
    engine_args: dict[str, list[str]] = Field(default_factory=dict)
    note: str = Field(min_length=1, max_length=200)


class Recommended(BaseModel):
    model_config = ConfigDict(extra="forbid")
    groups: list[Group]
    models: list[Entry]


def check(data: dict) -> Recommended:
    """Parse and cross-check the file's content. Raises RecommendedError naming the entry."""
    try:
        rec = Recommended.model_validate(data)
    except ValidationError as e:
        raise RecommendedError(str(e)) from None
    groups = {g.id for g in rec.groups}
    if len(groups) != len(rec.groups):
        raise RecommendedError("two groups have the same id")
    builtin = {n: r for n, (r, _) in recipes.builtin_recipes().items()}
    names, keys = set(), set()
    for e in rec.models:
        where = f"recommended model {e.name!r}"
        if not MODEL_NAME_RE.match(e.name):
            raise RecommendedError(f"{where}: the name must be a catalog name (a-z, 0-9, '.', '_', '-')")
        if e.name in names:
            raise RecommendedError(f"{where}: the name is used twice")
        names.add(e.name)
        try:
            src = sources.parse_source(e.source)
        except sources.SourceError as err:
            raise RecommendedError(f"{where}: {err}") from None
        if src.kind != "hf":
            raise RecommendedError(f"{where}: only hf:// sources are recommended")
        key = (str(src), e.revision, e.file)
        if key in keys:
            raise RecommendedError(f"{where}: the same source is listed twice")
        keys.add(key)
        if e.revision is not None and not (sources.REF_RE.match(e.revision) or sources.REVISION_RE.match(e.revision)):
            raise RecommendedError(f"{where}: revision is a branch, tag, ref or commit")
        if e.group not in groups:
            raise RecommendedError(f"{where}: no group {e.group!r}")
        if (e.format == "gguf") != bool(e.file):
            raise RecommendedError(f"{where}: a GGUF entry names its file, and only a GGUF entry does")
        if e.file:
            try:
                sources.safe_relpath(e.file)
            except sources.SourceError as err:
                raise RecommendedError(f"{where}: {err}") from None
        recipe = builtin.get(e.recipe)
        if recipe is None:
            raise RecommendedError(f"{where}: no built-in recipe {e.recipe!r}")
        if e.task not in recipe.tasks or e.format not in recipe.formats:
            raise RecommendedError(f"{where}: {e.recipe} does not serve {e.format} {e.task} models")
        if not any(v.gpu == (e.device == "gpu") for v in recipe.variants):
            raise RecommendedError(f"{where}: {e.recipe} has no {e.device.upper()} variant")
        for rname, args in e.engine_args.items():
            if rname not in builtin:
                raise RecommendedError(f"{where}: engine_args for an unknown recipe {rname!r}")
            if len(args) > 32 or any(len(a) > 4096 or any(c in a for c in "\x00\r\n") for a in args):
                raise RecommendedError(f"{where}: engine_args.{rname}: at most 32 one-line arguments")
    return rec


def load(path: Path = FILE) -> Recommended:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise RecommendedError(f"{path.name}: {e}") from None
    if not isinstance(data, dict):
        raise RecommendedError(f"{path.name}: not a mapping")
    return check(data)


@functools.lru_cache(maxsize=1)
def recommended() -> Recommended:
    return load()
