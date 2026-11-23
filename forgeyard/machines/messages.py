"""Validation of what agents send. Odd hardware strings are cut short, not refused: a long
model name must never keep a machine from connecting."""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from datetime import datetime
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from forgeyard.machines.protocol import DOWNLOAD_STATES, MAX_LOG_CHARS, REPLICA_STATES, RUNTIMES, VENDORS
from forgeyard.models.cache_keys import parse

log = logging.getLogger("forgeyard.machines")


def _short(limit: int):
    def cut(value: Any) -> Any:
        if value is None:
            return None
        return str(value).strip()[:limit]

    return BeforeValidator(cut)


def _last(limit: int):
    """Output text: the end is what matters, and whitespace is kept as it was."""
    def cut(value: Any) -> Any:
        return None if value is None else str(value)[-limit:]

    return cut


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


def _count(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if 0 <= n <= 100_000 else 0


def _download(value: Any) -> Optional[dict]:
    """{"phase": "model"|"engine"|"image", "done": bytes, "total": bytes, "file"?: name, "via"?: "direct" |
    "server", and for an image "layers", "layers_done", "unpacking"}: small, or dropped. `done` never exceeds a
    known total."""
    if not isinstance(value, dict):
        return None
    done, total = _bytes(value.get("done")) or 0, _bytes(value.get("total")) or 0
    out = {"phase": str(value.get("phase") or "model")[:16], "done": min(done, total) if total else done,
           "total": total}
    if out["phase"] == "image":
        out["layers"] = _count(value.get("layers"))
        out["layers_done"] = min(_count(value.get("layers_done")), out["layers"])
        if value.get("unpacking") is True:
            out["unpacking"] = True
    if value.get("file"):
        out["file"] = str(value["file"])[:200]
    if value.get("waiting"):
        out["waiting"] = str(value["waiting"])[:200]
    # 1.4 ("relay"): how the bytes come: "server" (through the server's relay) or "direct".
    if value.get("via") in ("direct", "server"):
        out["via"] = value["via"]
    return out


START_PHASES = ("model", "waiting", "image", "engine")
MAX_START_SECONDS = 7 * 86400


def _seconds(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return round(value, 1) if 0 <= value <= MAX_START_SECONDS else None


def _start(value: Any) -> Optional[dict]:
    """1.5.0 ("start_times"): {"seconds": total, "phases": {model|waiting|image|engine: seconds}}, each between 0 and
    a week; anything else is dropped (a start the server then measures itself)."""
    if not isinstance(value, dict):
        return None
    total = _seconds(value.get("seconds"))
    if total is None:
        return None
    raw = value.get("phases") if isinstance(value.get("phases"), dict) else {}
    phases = {k: v for k in START_PHASES if (v := _seconds(raw.get(k))) is not None}
    return {"seconds": total, "phases": phases}


_ENGINE_ID = re.compile(r"^[0-9a-f]{16}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _model_cache(value: Any) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    cached = [str(k)[:400] for k in (value.get("cached") or []) if isinstance(k, str)][:512]
    out = {"ok": bool(value.get("ok")), "free_bytes": _bytes(value.get("free_bytes")),
           "total_bytes": _bytes(value.get("total_bytes")), "cached": cached,
           "reason": (str(value["reason"])[:300] if value.get("reason") else None)}
    # 1.4: the engine archives unpacked there, by the first 16 hex digits of their SHA-256.
    engines = [e for e in (value.get("engines") or []) if isinstance(e, str) and _ENGINE_ID.match(e)][:256]
    if engines:
        out["engines"] = engines
    # 1.4 (cache_admin): each model with its size, the engines, what the cache takes, partial downloads.
    if isinstance(value.get("models"), list):
        out["models"] = [e for e in (_cache_model(v) for v in value["models"][:256]) if e is not None]
        detail = value.get("engines_detail") if isinstance(value.get("engines_detail"), list) else []
        out["engines_detail"] = [e for e in (_cache_engine(v) for v in detail[:256]) if e is not None]
        for k in ("used_bytes", "partial_bytes"):
            out[k] = _bytes(value.get(k))
        out["partial_files"] = _count(value.get("partial_files"))
        if value.get("truncated") is True:
            out["truncated"] = True
    return out


def _text(value: Any, limit: int) -> Optional[str]:
    return str(value)[:limit] if isinstance(value, str) and value else None


def _when(value: Any) -> Optional[float]:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if 0 <= n < 1e11 else None


def _cache_model(value: Any) -> Optional[dict]:
    """One model in an agent's cache: dropped (never cut short) when its key is not a cache key, so a
    key the server keeps is always one the agent can find again."""
    if not isinstance(value, dict):
        return None
    key = value.get("key")
    target = parse(key)
    kind = "hf" if target is not None and target["kind"] == "hf" else "url"
    if target is None or target["kind"] == "engine" or target.get("file") or value.get("kind") != kind:
        return None
    out = {"key": key, "kind": kind, "bytes": _bytes(value.get("bytes")) or 0, "files": _count(value.get("files")),
           "completed_at": _when(value.get("completed_at"))}
    if kind == "hf":
        out["repo"] = _text(value.get("repo"), 200)
        out["revision"] = target["revision"]
        if _count(value.get("missing")):
            out["missing"] = _count(value.get("missing"))
        parts = []
        for p in (value.get("parts") or [])[:64] if isinstance(value.get("parts"), list) else []:
            t = parse(p.get("key")) if isinstance(p, dict) else None
            if t is None or not t.get("file") or not p["key"].startswith(key + "/"):
                continue
            parts.append({"key": p["key"], "file": t["file"], "bytes": _bytes(p.get("bytes")) or 0})
        if parts:
            out["parts"] = parts
    else:
        out["file"] = _text(value.get("file"), 200)
        # 1.4 (D8): the SHA-256 the agent computed after it downloaded a URL model; the server records the
        # first one in the catalog entry, and every later download must match it.
        sha = value.get("sha256")
        if isinstance(sha, str) and _SHA256.match(sha):
            out["sha256"] = sha
    return out


def _cache_engine(value: Any) -> Optional[dict]:
    t = parse(value.get("key")) if isinstance(value, dict) else None
    if t is None or t["kind"] != "engine":
        return None
    return {"key": value["key"], "id": t["id"], "name": _text(value.get("name"), 64), "bytes": _bytes(value.get("bytes")) or 0}


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


# 1.4.5 (#170): the note a machine page shows (under "Detection notes") for cards of a vendor this server does not
# know, which a newer agent may report. The cards are left out; the rest of the hello is kept.
UNKNOWN_VENDOR = "unknown vendor"


class InventoryIn(_Msg):
    accelerators: list[AcceleratorIn] = Field(default_factory=list, max_length=256)
    collectors: dict[Annotated[str, _short(32)], CollectorStatus] = Field(default_factory=dict, max_length=16)

    @model_validator(mode="before")
    @classmethod
    def _leave_out_unknown_vendors(cls, value: Any) -> Any:
        """A newer agent may report a kind of card this server does not know (a `vendor` outside VENDORS). That
        card is left out with a warning and a detection note, instead of refusing the whole hello: refused, the
        agent would reconnect and be refused again forever (#170)."""
        if not isinstance(value, dict) or not isinstance(value.get("accelerators"), list):
            return value
        kept, unknown = [], []
        for card in value["accelerators"]:
            vendor = card.get("vendor") if isinstance(card, dict) else None
            if isinstance(vendor, str) and vendor not in VENDORS:
                unknown.append(vendor.strip()[:32])
            else:
                kept.append(card)
        if not unknown:
            return value
        names = ", ".join(sorted(set(unknown)))
        log.warning("An agent reported %d card(s) of a vendor this server does not know (%s); left out. "
                    "A newer agent? Update the server to use them.", len(unknown), names)
        collectors = value.get("collectors") if isinstance(value.get("collectors"), dict) else {}
        if len(collectors) < 16:
            n = len(unknown)
            collectors = {**collectors, UNKNOWN_VENDOR: {
                "ok": False, "count": n, "source": "server",
                "reason": f"{n} {'card' if n == 1 else 'cards'} of a vendor this server does not know ({names}) "
                          f"left out; update the server to use {'it' if n == 1 else 'them'}"}}
        return {**value, "accelerators": kept, "collectors": collectors}


def _port_range(value: Any) -> Optional[dict]:
    """(1.4.2) {"low": 30000, "high": 32767}: the host ports the agent publishes replicas on, or None."""
    if not isinstance(value, dict):
        return None
    low, high = value.get("low"), value.get("high")
    if type(low) is not int or type(high) is not int or not 1 <= low <= high <= 65535:
        return None
    return {"low": low, "high": high}


def _ip(value: Any) -> Optional[str]:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def _connection(value: Any) -> Optional[dict]:
    """(1.4.5, #171) {"address", "port"}: the agent's own end of this connection."""
    if not isinstance(value, dict):
        return None
    address, port = _ip(value.get("address")), value.get("port")
    if address is None or type(port) is not int or not 1 <= port <= 65535:
        return None
    return {"address": address, "port": port}


def _runtime_info(value: Any) -> dict:
    """{"process": {...}, "container": {...}}: small dicts of what each runtime can do; (1.4.2) "ports": the
    agent's host port range (servers before 1.4.2 drop it, so it never reaches them); (1.4.5, #171) "address":
    the address replicas are published on, which the gateway dials, and "connection": the agent's own end of the
    connection, which the server compares with the peer it sees and does not keep."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for name in RUNTIMES:
        item = value.get(name)
        if isinstance(item, dict) and len(json.dumps(item, default=str)) <= 4096:
            out[name] = item
    ports = _port_range(value.get("ports"))
    if ports is not None:
        out["ports"] = ports
    address = value.get("address")
    if isinstance(address, str) and address.strip():
        out["address"] = address.strip()[:64]
    connection = _connection(value.get("connection"))
    if connection is not None:
        out["connection"] = connection
    tls = _tls(value.get("tls"))
    if tls is not None:
        out["tls"] = tls
    gpu = parse_gpu_check(value.get("gpu_check"))
    if gpu is not None:
        out["gpu_check"] = gpu
    return out


# 1.4.5 (#202): what the agent's GPU access check found (agent/gpu_check.py). Only these fields, cut to size.
GPU_CHECK_DEVICES = 64
_DEVICE_PATH = re.compile(r"^/[A-Za-z0-9._/-]{1,255}$")


def parse_gpu_check(value: Any) -> Optional[dict]:
    """{ok, at, user, devices: [{path, vendor, ok, error}], reason}, or None (absent, `ok` null, or not of that shape)."""
    if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
        return None
    devices = []
    for d in value.get("devices") if isinstance(value.get("devices"), list) else []:
        if not isinstance(d, dict) or not isinstance(d.get("ok"), bool):
            continue
        path, vendor = d.get("path"), d.get("vendor")
        if not (isinstance(path, str) and _DEVICE_PATH.match(path) and vendor in ("nvidia", "amd")):
            continue
        item = {"path": path, "vendor": vendor, "ok": d["ok"]}
        if not d["ok"] and isinstance(d.get("error"), str):
            item["error"] = _plain(d["error"], 160)
        devices.append(item)
        if len(devices) >= GPU_CHECK_DEVICES:
            break
    out: dict = {"ok": value["ok"] and all(d["ok"] for d in devices), "devices": devices}
    for key, limit in (("at", 40), ("user", 64), ("reason", 300)):
        if isinstance(value.get(key), str) and value[key].strip():
            out[key] = _plain(value[key], limit)
    if not out["ok"] and "reason" not in out:
        out["reason"] = "a new process could not open the GPUs"
    return out


def _plain(text: str, limit: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()[:limit]


def _tls(value: Any) -> Optional[dict]:
    """(1.4.5, #163) {serial, not_after, name, ca}: the certificate the agent's key guards serve (what the machine's
    page shows; the gateway checks the certificate itself on every connection)."""
    if not isinstance(value, dict):
        return None
    serial, name, ca, not_after = value.get("serial"), value.get("name"), value.get("ca"), value.get("not_after")
    if not (isinstance(serial, str) and re.fullmatch(r"[0-9a-f]{1,64}", serial)):
        return None
    if not (isinstance(name, str) and len(name) <= 128 and isinstance(ca, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", ca)):
        return None
    if not isinstance(not_after, str) or len(not_after) > 40:
        return None
    try:
        datetime.fromisoformat(not_after)
    except ValueError:
        return None
    return {"serial": serial, "not_after": not_after, "name": name, "ca": ca}


def same_end(connection: Optional[dict], peer: Optional[tuple]) -> bool:
    """(1.4.5, #171) Whether the agent's own end of the connection is the peer the server sees: then nothing sits
    between them (no tunnel, no proxy), so the agent runs on the server's own host and network, and a loopback
    address it publishes replicas on is the server's loopback too."""
    if not connection or not peer or len(peer) < 2:
        return False
    return connection.get("address") == _ip(peer[0]) and connection.get("port") == peer[1]


# 1.4.2 (#121): how the agent runs (agent/how_it_runs.py). Kept as the agent sent it, within a size limit; the
# update command (install/update.py) checks every value again when it renders one.
INSTALL_MAX_BYTES = 32 * 1024
INSTALL_METHODS = ("container", "install.sh", "manual", "unknown")


def _install(value: Any) -> Optional[dict]:
    if not isinstance(value, dict) or value.get("method") not in INSTALL_METHODS:
        return None
    try:
        if len(json.dumps(value)) > INSTALL_MAX_BYTES:
            return {"method": value["method"], "known": False, "reason": "the description was too large"}
    except (TypeError, ValueError):
        return None
    return value


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
    # Added in M2, optional so an M1 agent's hello stays valid.
    # (1.4.1: up to 32; an agent sends at most 16, which servers up to 1.4.0 accept.)
    features: list[Annotated[str, _short(32)]] = Field(default_factory=list, max_length=32)
    runtime_info: Annotated[dict, BeforeValidator(_runtime_info)] = Field(default_factory=dict)
    # Added in M3: the model cache (free space, cached models).
    model_cache: Annotated[Optional[dict], BeforeValidator(_model_cache)] = None
    # Added in 1.4.2: how the agent runs, for its update command (older agents do not send it).
    install: Annotated[Optional[dict], BeforeValidator(_install)] = None


class GpuCheckIn(_Msg):
    """1.4.5 ("gpu_check", #202): the agent's GPU access check changed. `gpu_check` None: nothing to check any more."""

    v: int
    type: Literal["gpu_check"]
    gpu_check: Annotated[Optional[dict], BeforeValidator(parse_gpu_check)] = None


class CertRequestIn(_Msg):
    """1.4.5 ("guard_tls"): a CSR for the key the agent's replica guards serve TLS with."""

    v: int
    type: Literal["cert_request"]
    request_id: Annotated[str, _short(64)]
    csr: Annotated[str, _short(8192)]


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


class PortOut(_Msg):
    name: Annotated[str, _short(16)]
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    host_port: Optional[int] = Field(default=None, ge=1, le=65535)
    address: Annotated[Optional[str], _short(64)] = None


class ReplicaStatusIn(_Msg):
    id: Annotated[str, _short(32)]
    generation: int = Field(ge=0)
    state: Literal[REPLICA_STATES]  # type: ignore[valid-type]
    reason: Annotated[Optional[str], _short(400)] = None
    exit_code: Optional[int] = Field(default=None, ge=-255, le=255)
    attempt: int = Field(default=0, ge=0, le=10_000)
    restarts: int = Field(default=0, ge=0, le=10**9)
    final: bool = False
    pid: Optional[int] = Field(default=None, ge=0, le=2**31)
    container: Annotated[Optional[str], _short(128)] = None
    ports: list[PortOut] = Field(default_factory=list, max_length=8)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    log_tail: Annotated[Optional[str], BeforeValidator(_last(16384))] = None
    # M3: a model or engine being downloaded before the replica starts.
    download: Annotated[Optional[dict], BeforeValidator(_download)] = None
    # 1.5.0 ("start_times"): how long the start that made it ready took, once it is.
    start: Annotated[Optional[dict], BeforeValidator(_start)] = None


class DownloadStatusIn(_Msg):
    """1.4 (prefetch): one download asked for in `desired.downloads`, as the agent has it."""

    id: Annotated[str, _short(32)]
    state: Literal[DOWNLOAD_STATES]  # type: ignore[valid-type]
    reason: Annotated[Optional[str], _short(400)] = None
    download: Annotated[Optional[dict], BeforeValidator(_download)] = None


class StatusIn(_Msg):
    v: int
    type: Literal["status"]
    # True: every replica the agent knows is listed, so missing ones are not running there.
    full: bool = False
    replicas: list[ReplicaStatusIn] = Field(default_factory=list, max_length=512)
    # M3: the agent's model cache, when it changed.
    model_cache: Annotated[Optional[dict], BeforeValidator(_model_cache)] = None
    # 1.4 (prefetch): the downloads that changed (all of them in a full report).
    downloads: list[DownloadStatusIn] = Field(default_factory=list, max_length=256)


class LogsIn(_Msg):
    v: int
    type: Literal["logs"]
    request_id: Annotated[str, _short(32)]
    replica: Annotated[str, _short(32)]
    ok: bool = True
    text: str = Field(default="", max_length=MAX_LOG_CHARS)
    truncated: bool = False
    reason: Annotated[Optional[str], _short(400)] = None


class CacheRemovedIn(_Msg):
    """1.4 (cache_admin): the answer to cache_remove / cache_prune."""

    v: int
    type: Literal["cache_removed"]
    request_id: Annotated[str, _short(32)]
    ok: bool = False
    code: Annotated[Optional[str], _short(32)] = None
    reason: Annotated[Optional[str], _short(400)] = None
    freed_bytes: Bytes = None
    in_use: list[Annotated[str, _short(64)]] = Field(default_factory=list, max_length=32)
    model_cache: Annotated[Optional[dict], BeforeValidator(_model_cache)] = None


class RelaySentIn(_Msg):
    """1.4.1 (relay_source): how the send of a cached model file to the server ended."""

    v: int
    type: Literal["relay_sent"]
    request_id: Annotated[str, _short(32)]
    ok: bool = False
    code: Annotated[Optional[str], _short(32)] = None
    reason: Annotated[Optional[str], _short(400)] = None
    bytes: Bytes = None


class LoadedImageIn(BaseModel):
    """One image an uploaded file held, as the agent's engine shows it once loaded (1.4, "image_load")."""

    model_config = ConfigDict(extra="ignore")
    id: Annotated[str, _short(80)]
    tags: list[Annotated[str, _short(255)]] = Field(default_factory=list, max_length=16)
    content: Annotated[Optional[str], _short(80)] = None
    os: Annotated[Optional[str], _short(32)] = None
    architecture: Annotated[Optional[str], _short(32)] = None


class ListedImageIn(BaseModel):
    """One image in an agent's container engine (1.4.1, "image_save"): what can be copied from it."""

    model_config = ConfigDict(extra="ignore")
    id: Annotated[str, _short(80)]
    tags: list[Annotated[str, _short(255)]] = Field(default_factory=list, max_length=16)
    content: Annotated[Optional[str], _short(80)] = None
    size: Bytes = None
    created: Annotated[Optional[str], _short(40)] = None
    os: Annotated[Optional[str], _short(32)] = None
    architecture: Annotated[Optional[str], _short(32)] = None


class ImageListedIn(_Msg):
    """1.4.1 (image_save): the answer to image_list."""

    v: int
    type: Literal["image_listed"]
    request_id: Annotated[str, _short(32)]
    ok: bool = False
    reason: Annotated[Optional[str], _short(400)] = None
    images: list[ListedImageIn] = Field(default_factory=list, max_length=256)
    # 1.4.2 ("image_remove"): the image the agent itself runs from (never removed), and the space where the engine
    # keeps its images, when the agent can see it.
    agent_image: Annotated[Optional[str], _short(80)] = None
    free_bytes: Bytes = None
    total_bytes: Bytes = None


class ImageSavedIn(_Msg):
    """1.4.1 (image_save): how the send of an image ended, and which image it was."""

    v: int
    type: Literal["image_saved"]
    request_id: Annotated[str, _short(32)]
    ok: bool = False
    reason: Annotated[Optional[str], _short(400)] = None
    bytes: Bytes = None
    image: Optional[ListedImageIn] = None


class ImageRemovedIn(_Msg):
    """1.4.2 ("image_remove"): the answer to image_remove: removed (what, the bytes freed, the space after), or why
    not (code not_found | agent_image | in_use | failed, and what uses it)."""

    v: int
    type: Literal["image_removed"]
    request_id: Annotated[str, _short(32)]
    ok: bool = False
    code: Annotated[Optional[str], _short(32)] = None
    reason: Annotated[Optional[str], _short(400)] = None
    in_use: list[Annotated[str, _short(128)]] = Field(default_factory=list, max_length=16)
    image: Optional[ListedImageIn] = None
    freed_bytes: Bytes = None
    free_bytes: Bytes = None
    total_bytes: Bytes = None


class ImageLoadedIn(_Msg):
    """1.4 (image_load): the answer to image_load: what the engine loaded from the uploaded file, or why not."""

    v: int
    type: Literal["image_loaded"]
    request_id: Annotated[str, _short(32)]
    ok: bool = False
    reason: Annotated[Optional[str], _short(400)] = None
    images: list[LoadedImageIn] = Field(default_factory=list, max_length=32)
