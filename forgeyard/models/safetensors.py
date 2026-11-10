"""safetensors headers: the parameter count of a checkpoint, by dtype, without the weights.

A .safetensors file starts with an 8-byte little-endian header length, then a JSON header that
lists every tensor's dtype and shape. Two small range requests per shard are enough.
"""

from __future__ import annotations

import json
import struct
from typing import Callable

MAX_HEADER = 100 * 1024**2

DTYPE_BYTES = {"F64": 8, "I64": 8, "U64": 8, "F32": 4, "I32": 4, "U32": 4, "F16": 2, "BF16": 2, "I16": 2,
               "U16": 2, "F8_E4M3": 1, "F8_E5M2": 1, "F8_E8M0": 1, "I8": 1, "U8": 1, "BOOL": 1,
               "F4": 0.5, "F6_E2M3": 0.75, "F6_E3M2": 0.75}

# Tensors that stay in 16 bits when vLLM quantises a model to FP8 on the fly (embeddings, the
# output head, norms): only the linear layers' weights shrink.
_KEEP_HIGH = ("embed", "lm_head", "norm", "wte", "wpe", "bias")


class SafetensorsError(Exception):
    pass


def header_length(first8: bytes) -> int:
    if len(first8) < 8:
        raise SafetensorsError("file too short for a safetensors header")
    (n,) = struct.unpack("<Q", first8[:8])
    if n <= 1 or n > MAX_HEADER:
        raise SafetensorsError(f"implausible safetensors header length {n}")
    return n


def parse_header(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise SafetensorsError(f"the safetensors header is not JSON ({e})") from None
    if not isinstance(data, dict):
        raise SafetensorsError("the safetensors header is not an object")
    return data


def count(header: dict) -> dict:
    """{"params": {dtype: n}, "bytes": stored bytes, "linear_params": n, "other_params": n}."""
    params: dict[str, int] = {}
    stored = 0
    linear = other = 0
    for name, info in header.items():
        if name == "__metadata__" or not isinstance(info, dict):
            continue
        dtype = str(info.get("dtype", "")).upper()
        shape = info.get("shape") or []
        n = 1
        for d in shape:
            n *= int(d)
        params[dtype] = params.get(dtype, 0) + n
        offsets = info.get("data_offsets") or [0, 0]
        stored += int(offsets[1]) - int(offsets[0])
        if len(shape) >= 2 and not any(k in name.lower() for k in _KEEP_HIGH):
            linear += n
        else:
            other += n
    return {"params": params, "bytes": stored, "linear_params": linear, "other_params": other}


def read_remote(fetch_range: Callable[[int, int], bytes]) -> dict:
    n = header_length(fetch_range(0, 7))
    raw = fetch_range(8, 8 + n - 1)
    if len(raw) < n:
        raise SafetensorsError("the file ended inside its safetensors header")
    return count(parse_header(raw))


def merge(counts: list[dict]) -> dict:
    out = {"params": {}, "bytes": 0, "linear_params": 0, "other_params": 0}
    for c in counts:
        for k, v in c["params"].items():
            out["params"][k] = out["params"].get(k, 0) + v
        out["bytes"] += c["bytes"]
        out["linear_params"] += c["linear_params"]
        out["other_params"] += c["other_params"]
    return out
