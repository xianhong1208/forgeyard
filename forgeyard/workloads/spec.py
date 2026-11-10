"""The workload spec: what to run, with what, where. Shared by the server (validation on create)
and the agent (validated again before anything starts).

Kept small and explicit on purpose:

    name       a-z, 0-9 and '-', unique
    kind       service (runs until stopped, health-checked, restarted) | task (runs to the end)
    runtime    {type: container, image, command?, args?} | {type: process, command, args?, workdir?}
    env        explicit key/values only. A workload starts from an EMPTY environment: never the
               agent's own, never host credentials. GPU visibility and FORGEYARD_* are set by
               Forgeyard and cannot be declared.
    resources  cpu (cores), memory (bytes or "4Gi"), gpus {count | indices, vendor?}
    ports      services: [{name, port}]; the agent picks the host port from its range
    health     services: {type: http|tcp, port, path, interval, timeout, failure_threshold}
    restart    services: always | on-failure | never
    max_duration, retries, retry_backoff   tasks
    replicas   1..16, fixed
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
PORT_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,14}$")
# An image reference: registry/name:tag@digest. Never starting with '-', so it can never be read
# as a docker/podman flag.
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,254}$")

# Set by Forgeyard on every replica. A spec that declares them would undo GPU isolation, so they
# are refused, not silently overridden.
RESERVED_ENV = frozenset({
    "CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER",
    "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "GPU_DEVICE_ORDINAL",
    "PORT",
})
RESERVED_ENV_PREFIXES = ("FORGEYARD_", "PORT_")

MAX_REPLICAS = 16
MAX_GPUS = 16
MAX_DURATION = 30 * 86400
VENDORS = ("nvidia", "amd")

_UNITS = {"": 1, "k": 1000, "m": 1000**2, "g": 1000**3, "t": 1000**4,
          "ki": 1024, "mi": 1024**2, "gi": 1024**3, "ti": 1024**4}
_MEM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]i?)?b?\s*$", re.IGNORECASE)


def parse_bytes(value) -> int:
    """512 -> 512, "512Mi" -> 536870912, "4G" -> 4000000000, "1.5Gi" -> 1610612736."""
    if isinstance(value, bool):
        raise ValueError("memory must be a number of bytes or a size such as 4Gi")
    if isinstance(value, (int, float)):
        n = int(value)
    else:
        m = _MEM_RE.match(str(value))
        if not m:
            raise ValueError("memory must be a number of bytes or a size such as 512Mi or 4Gi")
        n = int(float(m.group(1)) * _UNITS[(m.group(2) or "").lower()])
    if n < 4 * 1024**2 or n > 2**50:
        raise ValueError("memory must be between 4 MiB and 1 PiB")
    return n


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _no_nul(values: list[str], what: str) -> list[str]:
    for v in values:
        if "\x00" in v:
            raise ValueError(f"{what} must not contain NUL characters")
    return values


class ContainerRuntime(_Model):
    type: Literal["container"]
    image: str = Field(max_length=255)
    # Like Kubernetes: `command` replaces the image's entrypoint, `args` its default arguments.
    command: Optional[list[Annotated[str, Field(max_length=4096)]]] = Field(default=None, max_length=64)
    args: list[Annotated[str, Field(max_length=4096)]] = Field(default_factory=list, max_length=256)

    @field_validator("image")
    @classmethod
    def _image(cls, v: str) -> str:
        v = v.strip()
        if not IMAGE_RE.match(v):
            raise ValueError("image must be an image reference such as python:3.13-slim or ghcr.io/org/app:1.2")
        return v

    @field_validator("command")
    @classmethod
    def _command(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is not None and (not v or not v[0].strip()):
            raise ValueError("command, when given, needs at least the program to run")
        return _no_nul(v, "command") if v is not None else None

    @field_validator("args")
    @classmethod
    def _args(cls, v: list[str]) -> list[str]:
        return _no_nul(v, "args")


class ProcessRuntime(_Model):
    type: Literal["process"]
    # A program name (looked up on the basic PATH) or an absolute path.
    command: str = Field(min_length=1, max_length=4096)
    args: list[Annotated[str, Field(max_length=4096)]] = Field(default_factory=list, max_length=256)
    workdir: Optional[str] = Field(default=None, max_length=4096)

    @field_validator("command")
    @classmethod
    def _command(cls, v: str) -> str:
        v = v.strip()
        if not v or "\x00" in v:
            raise ValueError("command must be a program name or an absolute path")
        if "/" in v and not v.startswith("/"):
            raise ValueError("command must be a program name or an absolute path, not a relative path")
        return v

    @field_validator("args")
    @classmethod
    def _args(cls, v: list[str]) -> list[str]:
        return _no_nul(v, "args")

    @field_validator("workdir")
    @classmethod
    def _workdir(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not v.startswith("/") or "\x00" in v:
            raise ValueError("workdir must be an absolute path")
        return v


Runtime = Annotated[Union[ContainerRuntime, ProcessRuntime], Field(discriminator="type")]


class Gpus(_Model):
    # Either how many cards, or exactly which ones (indices on the chosen machine).
    count: Optional[int] = Field(default=None, ge=0, le=MAX_GPUS)
    indices: Optional[list[Annotated[int, Field(ge=0, le=4096)]]] = Field(default=None, max_length=MAX_GPUS)
    vendor: Optional[Literal[VENDORS]] = None  # type: ignore[valid-type]

    @model_validator(mode="after")
    def _one_of(self) -> "Gpus":
        if self.count is not None and self.indices is not None:
            raise ValueError("give either gpus.count or gpus.indices, not both")
        if self.indices is not None:
            if len(set(self.indices)) != len(self.indices):
                raise ValueError("gpus.indices has duplicates")
            self.indices = sorted(self.indices)
        return self

    @property
    def wanted(self) -> int:
        return len(self.indices) if self.indices is not None else (self.count or 0)


class Resources(_Model):
    cpu: Optional[float] = Field(default=None, gt=0, le=1024)
    memory: Optional[int] = None
    gpus: Gpus = Field(default_factory=Gpus)

    @field_validator("memory", mode="before")
    @classmethod
    def _memory(cls, v):
        return None if v in (None, "") else parse_bytes(v)


class Port(_Model):
    name: str = "http"
    # The port inside the container. Ignored for processes: they listen on the host port the
    # agent assigns ($PORT, or ${PORT_<NAME>} in args).
    port: Optional[int] = Field(default=None, ge=1, le=65535)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not PORT_NAME_RE.match(v):
            raise ValueError("port names use a-z, 0-9 and '-', starting with a letter (at most 15)")
        return v


class Health(_Model):
    type: Literal["http", "tcp"]
    port: Optional[str] = None            # a port name; default: the first port
    path: str = Field(default="/", max_length=1024)
    interval: int = Field(default=10, ge=1, le=300)
    timeout: int = Field(default=3, ge=1, le=60)
    failure_threshold: int = Field(default=3, ge=1, le=20)
    initial_delay: int = Field(default=0, ge=0, le=3600)

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if not v.startswith("/") or any(c in v for c in "\r\n\x00 "):
            raise ValueError("health.path must start with / and contain no spaces")
        return v


class WorkloadSpec(_Model):
    name: str
    kind: Literal["service", "task"]
    runtime: Runtime
    env: dict[str, Annotated[str, Field(max_length=8192)]] = Field(default_factory=dict, max_length=64)
    resources: Resources = Field(default_factory=Resources)
    ports: list[Port] = Field(default_factory=list, max_length=8)
    health: Optional[Health] = None
    restart: Literal["always", "on-failure", "never"] = "always"
    max_duration: Optional[int] = Field(default=None, ge=1, le=MAX_DURATION)
    retries: int = Field(default=0, ge=0, le=20)
    retry_backoff: int = Field(default=5, ge=1, le=3600)
    replicas: int = Field(default=1, ge=1, le=MAX_REPLICAS)
    # None: any online machine with enough free resources (first fit); else that machine.
    machine_id: Optional[int] = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not NAME_RE.match(v):
            raise ValueError("name: 1-48 characters, a-z, 0-9 and '-', not starting or ending with '-'")
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        for key, value in v.items():
            if not ENV_KEY_RE.match(key):
                raise ValueError(f"{key!r} is not a valid variable name")
            if key in RESERVED_ENV or key.startswith(RESERVED_ENV_PREFIXES):
                raise ValueError(f"{key} is set by Forgeyard (GPU visibility, ports, replica identity) "
                                 "and cannot be declared")
            if any(c in value for c in "\r\n\x00"):
                raise ValueError(f"the value of {key} must be a single line")
        return v

    @model_validator(mode="after")
    def _kind_rules(self) -> "WorkloadSpec":
        names = [p.name for p in self.ports]
        if len(set(names)) != len(names):
            raise ValueError("ports: names must be unique")
        if self.kind == "task":
            if self.ports:
                raise ValueError("ports are for services; a task has none")
            if self.health is not None:
                raise ValueError("health checks are for services")
        else:
            if self.max_duration is not None:
                raise ValueError("max_duration is for tasks; a service runs until it is stopped")
            if self.retries:
                raise ValueError("retries are for tasks; services use the restart policy")
        if isinstance(self.runtime, ContainerRuntime):
            missing = [p.name for p in self.ports if p.port is None]
            if missing:
                raise ValueError(f"ports: give the container port for {', '.join(missing)}")
        if self.health is not None:
            if not self.ports:
                raise ValueError("a health check needs a port")
            if self.health.port is None:
                self.health.port = self.ports[0].name
            elif self.health.port not in names:
                raise ValueError(f"health.port {self.health.port!r} is not one of the ports")
        gpus = self.resources.gpus
        if gpus.indices is not None:
            if self.machine_id is None:
                raise ValueError("gpus.indices name cards on one machine: choose the machine too")
            if self.replicas > 1 and gpus.indices:
                raise ValueError("gpus.indices can be used by one replica only (cards are not shared)")
        if gpus.wanted == 0:
            gpus.vendor = None
        return self

    @property
    def runtime_type(self) -> str:
        return self.runtime.type


def env_masked(env: dict[str, str]) -> dict[str, str]:
    """What people who may not see values get: the names only."""
    return {k: "••••" for k in env}
