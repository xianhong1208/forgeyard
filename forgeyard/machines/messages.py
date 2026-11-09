"""Validation of what agents send. Odd hardware strings are cut short, not refused: a long
model name must never keep a machine from connecting."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from forgeyard.machines.protocol import VENDORS


def _short(limit: int):
    def cut(value: Any) -> Any:
        if value is None:
            return None
        return str(value).strip()[:limit]

    return BeforeValidator(cut)


def _bytes(value: Any) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 0 <= n < 2**62 else None


def _percent(value: Any) -> Optional[int]:
    try:
        n = round(float(value))
    except (TypeError, ValueError):
        return None
    return min(100, max(0, n))


def _celsius(value: Any) -> Optional[int]:
    try:
        n = round(float(value))
    except (TypeError, ValueError):
        return None
    return n if -60 <= n <= 250 else None


def _detail(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    if len(json.dumps(value, default=str)) > 8192:
        return {"truncated": True}
    return value


Bytes = Annotated[Optional[int], BeforeValidator(_bytes)]
Percent = Annotated[Optional[int], BeforeValidator(_percent)]
Celsius = Annotated[Optional[int], BeforeValidator(_celsius)]
Detail = Annotated[dict, BeforeValidator(_detail)]


class _Msg(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AcceleratorIn(_Msg):
    key: Annotated[str, _short(160)]
    vendor: Literal[VENDORS]  # type: ignore[valid-type]
    index: int = Field(default=0, ge=0, le=4096)
    model: Annotated[str, _short(160)]
    arch: Annotated[Optional[str], _short(32)] = None
    uuid: Annotated[Optional[str], _short(128)] = None
    vram_total: Bytes = None
    vram_free: Bytes = None
    vram_used: Bytes = None
    unified_memory: bool = False
    driver: Annotated[Optional[str], _short(64)] = None
    utilization: Percent = None
    temperature: Celsius = None
    detail: Detail = Field(default_factory=dict)


class CollectorStatus(_Msg):
    ok: bool = False
    count: int = Field(default=0, ge=0, le=4096)
    source: Annotated[Optional[str], _short(64)] = None
    reason: Annotated[Optional[str], _short(400)] = None


class InventoryIn(_Msg):
    accelerators: list[AcceleratorIn] = Field(default_factory=list, max_length=256)
    collectors: dict[Annotated[str, _short(32)], CollectorStatus] = Field(default_factory=dict, max_length=16)


class HelloIn(_Msg):
    v: int
    type: Literal["hello"]
    agent_version: Annotated[str, _short(32)]
    hostname: Annotated[Optional[str], _short(255)] = None
    labels: dict[Annotated[str, _short(64)], Annotated[str, _short(128)]] = Field(default_factory=dict, max_length=32)
    platform: dict[Annotated[str, _short(32)], Annotated[str, _short(128)]] = Field(default_factory=dict, max_length=16)
    runtimes: list[Annotated[str, _short(32)]] = Field(default_factory=list, max_length=8)
    simulated: bool = False
    inventory: InventoryIn


class MetricIn(_Msg):
    key: Annotated[str, _short(160)]
    vram_free: Bytes = None
    vram_used: Bytes = None
    utilization: Percent = None
    temperature: Celsius = None


class HeartbeatIn(_Msg):
    v: int
    type: Literal["heartbeat"]
    seq: int = 0
    metrics: list[MetricIn] = Field(default_factory=list, max_length=256)


class InventoryMsgIn(_Msg):
    v: int
    type: Literal["inventory"]
    inventory: InventoryIn
