"""GGUF metadata, read from the start of the file only (like gpustack's gguf-parser): enough to
know the architecture, the KV-cache shape and the exact size of every tensor, without
downloading the weights. A remote file is read with HTTP range requests, growing the window
until the header (metadata + tensor infos) is complete.

Format: https://github.com/ggml-org/ggml/blob/master/docs/gguf.md
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from typing import Callable, Optional

MAGIC = b"GGUF"
DEFAULT_ALIGNMENT = 32
MAX_HEADER = 128 * 1024**2  # a header larger than this is refused (real ones are a few MB)

# Scalar value types: (struct format, size).
_SCALARS = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
T_STRING, T_ARRAY = 8, 9

# ggml tensor types: (elements per block, bytes per block). From ggml.c's type_traits.
GGML_TYPES: dict[int, tuple[str, int, int]] = {
    0: ("F32", 1, 4), 1: ("F16", 1, 2), 2: ("Q4_0", 32, 18), 3: ("Q4_1", 32, 20),
    6: ("Q5_0", 32, 22), 7: ("Q5_1", 32, 24), 8: ("Q8_0", 32, 34), 9: ("Q8_1", 32, 36),
    10: ("Q2_K", 256, 84), 11: ("Q3_K", 256, 110), 12: ("Q4_K", 256, 144), 13: ("Q5_K", 256, 176),
    14: ("Q6_K", 256, 210), 15: ("Q8_K", 256, 292), 16: ("IQ2_XXS", 256, 66), 17: ("IQ2_XS", 256, 74),
    18: ("IQ3_XXS", 256, 98), 19: ("IQ1_S", 256, 50), 20: ("IQ4_NL", 32, 18), 21: ("IQ3_S", 256, 110),
    22: ("IQ2_S", 256, 82), 23: ("IQ4_XS", 256, 136), 24: ("I8", 1, 1), 25: ("I16", 1, 2),
    26: ("I32", 1, 4), 27: ("I64", 1, 8), 28: ("F64", 1, 8), 29: ("IQ1_M", 256, 56), 30: ("BF16", 1, 2),
    34: ("TQ1_0", 256, 54), 35: ("TQ2_0", 256, 66), 39: ("MXFP4", 32, 17),
}

_BLOCK_RE = re.compile(r"^blk\.(\d+)\.")


class GGUFError(Exception):
    pass


class _Short(Exception):
    """The buffer ends before the header does: read more."""


@dataclass
class Tensor:
    name: str
    dims: list[int]
    type: int
    offset: int

    @property
    def elements(self) -> int:
        n = 1
        for d in self.dims:
            n *= d
        return n

    @property
    def nbytes(self) -> int:
        info = GGML_TYPES.get(self.type)
        if info is None:
            raise GGUFError(f"tensor {self.name} has an unknown ggml type {self.type}")
        _, block, size = info
        return (self.elements + block - 1) // block * size


@dataclass
class GGUFHeader:
    version: int
    metadata: dict
    tensors: list[Tensor]
    header_size: int          # bytes up to the end of the tensor infos
    data_offset: int          # where tensor data starts (aligned)
    array_lengths: dict = field(default_factory=dict)  # long arrays are kept as their length only

    @property
    def architecture(self) -> str:
        return str(self.metadata.get("general.architecture") or "")

    def arch_value(self, key: str, default=None):
        return self.metadata.get(f"{self.architecture}.{key}", default)

    @property
    def tensor_bytes(self) -> int:
        return sum(t.nbytes for t in self.tensors)

    def summary(self) -> dict:
        """What the estimator needs, small enough to store with the catalog entry."""
        layers = int(self.arch_value("block_count") or 0)
        heads = int(self.arch_value("attention.head_count") or 0)
        kv_heads = self.arch_value("attention.head_count_kv", heads)
        if isinstance(kv_heads, list):  # per-layer counts (some architectures)
            kv_heads = max(kv_heads) if kv_heads else heads
        embd = int(self.arch_value("embedding_length") or 0)
        key_len = int(self.arch_value("attention.key_length") or (embd // heads if heads else 0))
        value_len = int(self.arch_value("attention.value_length") or key_len)
        per_layer = [0] * layers
        other = 0
        output = 0
        for t in self.tensors:
            m = _BLOCK_RE.match(t.name)
            if m and int(m.group(1)) < layers:
                per_layer[int(m.group(1))] += t.nbytes
            elif t.name.startswith("output.") or t.name.startswith("output_norm"):
                output += t.nbytes
            else:
                other += t.nbytes
        vocab = self.array_lengths.get("tokenizer.ggml.tokens") or int(self.arch_value("vocab_size") or 0)
        return {
            "architecture": self.architecture,
            "name": self.metadata.get("general.name"),
            "file_type": self.metadata.get("general.file_type"),
            "context_length": int(self.arch_value("context_length") or 0),
            "layers": layers,
            "heads": heads,
            "kv_heads": int(kv_heads or 0),
            "embedding_length": embd,
            "key_length": key_len,
            "value_length": value_len,
            "vocab": int(vocab),
            "tensor_count": len(self.tensors),
            "tensor_bytes": self.tensor_bytes,
            "layer_bytes": per_layer,
            "output_bytes": output,
            "other_bytes": other,
            "header_size": self.header_size,
            "data_offset": self.data_offset,
            "elements": sum(t.elements for t in self.tensors),
        }


class _Reader:
    def __init__(self, buf: bytes):
        self.buf = buf
        self.p = 0

    def take(self, n: int) -> bytes:
        if self.p + n > len(self.buf):
            raise _Short()
        out = self.buf[self.p:self.p + n]
        self.p += n
        return out

    def scalar(self, fmt: str):
        size = struct.calcsize("<" + fmt)
        return struct.unpack("<" + fmt, self.take(size))[0]

    def string(self) -> str:
        n = self.scalar("Q")
        if n > MAX_HEADER:
            raise GGUFError("a string longer than the header limit: not a GGUF file?")
        return self.take(n).decode("utf-8", "replace")

    def skip_string(self) -> None:
        n = self.scalar("Q")
        if n > MAX_HEADER:
            raise GGUFError("a string longer than the header limit: not a GGUF file?")
        self.take(n)


# Arrays longer than this are not kept (the vocabulary, the merges): only their length.
KEEP_ARRAY = 64


def _value(r: _Reader, vtype: int, key: str, lengths: dict):
    if vtype == T_STRING:
        return r.string()
    if vtype == T_ARRAY:
        etype = r.scalar("I")
        n = r.scalar("Q")
        if n > 1 << 32:
            raise GGUFError(f"array {key} claims {n} elements")
        if n > KEEP_ARRAY:
            lengths[key] = n
            if etype == T_STRING:
                for _ in range(n):
                    r.skip_string()
            elif etype in _SCALARS:
                r.take(n * struct.calcsize("<" + _SCALARS[etype]))
            else:
                raise GGUFError(f"array {key} has an unsupported element type {etype}")
            return None
        if etype == T_STRING:
            return [r.string() for _ in range(n)]
        if etype == T_ARRAY:
            raise GGUFError(f"nested arrays are not supported ({key})")
        return [r.scalar(_SCALARS[etype]) for _ in range(n)]
    if vtype in _SCALARS:
        return r.scalar(_SCALARS[vtype])
    raise GGUFError(f"metadata {key} has an unknown type {vtype}")


def parse(buf: bytes) -> GGUFHeader:
    """Parse a GGUF header from the start of a file. Raises _Short when `buf` ends too early."""
    r = _Reader(buf)
    if r.take(4) != MAGIC:
        raise GGUFError("not a GGUF file (bad magic)")
    version = r.scalar("I")
    if version not in (2, 3):
        raise GGUFError(f"GGUF version {version} is not supported")
    n_tensors = r.scalar("Q")
    n_kv = r.scalar("Q")
    if n_tensors > 1 << 20 or n_kv > 1 << 20:
        raise GGUFError("implausible tensor or metadata count")
    metadata: dict = {}
    lengths: dict = {}
    for _ in range(n_kv):
        key = r.string()
        vtype = r.scalar("I")
        value = _value(r, vtype, key, lengths)
        if value is not None:
            metadata[key] = value
    tensors = []
    for _ in range(n_tensors):
        name = r.string()
        n_dims = r.scalar("I")
        if n_dims > 8:
            raise GGUFError(f"tensor {name} has {n_dims} dimensions")
        dims = [r.scalar("Q") for _ in range(n_dims)]
        ttype = r.scalar("I")
        offset = r.scalar("Q")
        tensors.append(Tensor(name, dims, ttype, offset))
    header_size = r.p
    align = int(metadata.get("general.alignment") or DEFAULT_ALIGNMENT)
    data_offset = (header_size + align - 1) // align * align
    return GGUFHeader(version, metadata, tensors, header_size, data_offset, lengths)


def parse_complete(buf: bytes) -> Optional[GGUFHeader]:
    """The header, or None if `buf` is too short to hold all of it."""
    try:
        return parse(buf)
    except (_Short, struct.error):
        return None


def read_remote(fetch_range: Callable[[int, int], bytes], *, first: int = 2 * 1024**2,
                limit: int = MAX_HEADER) -> GGUFHeader:
    """Read a header with range requests: `fetch_range(start, end)` returns those bytes. Starts with
    `first` bytes and doubles until the header is complete (a Qwen3 GGUF needs about 6 MB, most of
    it the vocabulary)."""
    buf = b""
    size = first
    while True:
        start = len(buf)
        chunk = fetch_range(start, size - 1)
        buf += chunk
        header = parse_complete(buf)
        if header is not None:
            return header
        if len(chunk) < size - start:
            raise GGUFError("the file ended before its GGUF header did")
        if size >= limit:
            raise GGUFError(f"the GGUF header is larger than {limit // 1024**2} MB")
        size = min(limit, size * 2)
