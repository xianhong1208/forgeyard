"""The agent's wheel, built from the code this server runs, so an agent installed from it is always
the server's own version.

    python -m forgeyard.install.wheel [OUT_DIR]     # writes forgeyard-<version>-py3-none-any.whl

Standard library only: the Docker image builds the same wheel with a bare Python. The build is
reproducible (sorted entries, fixed timestamps), so the same source gives the same bytes.

The wheel holds the whole `forgeyard` package except the built web UI, but declares only what the
agent imports (a test checks that). Installed from it, `forgeyard agent …` works; the server does
not (it needs the dependencies in pyproject.toml).
"""

from __future__ import annotations

import base64
import hashlib
import io
import sys
import zipfile
from functools import lru_cache
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent
LICENSE = PACKAGE.parent / "LICENSE"
NAME = "forgeyard"
# What `forgeyard agent` imports (tests/test_install.py checks the agent's modules against it).
REQUIRES = ("websockets>=15", "h11>=0.16", "pydantic>=2.7")
EXTRAS = {"nvidia": ("nvidia-ml-py>=12.535",)}
EXCLUDED_DIRS = {"__pycache__", "static"}
_EPOCH = (2020, 1, 1, 0, 0, 0)


def version() -> str:
    from forgeyard import __version__

    return __version__


def filename(ver: str | None = None) -> str:
    return f"{NAME}-{ver or version()}-py3-none-any.whl"


def _files() -> list[tuple[str, Path]]:
    out = []
    for path in sorted(PACKAGE.rglob("*")):
        rel = path.relative_to(PACKAGE.parent)
        if not path.is_file() or EXCLUDED_DIRS & set(rel.parts[1:-1]):
            continue
        if path.suffix in (".pyc", ".pyo") or path.name.endswith(".tmp"):
            continue
        out.append((rel.as_posix(), path))
    return out


def _metadata(ver: str) -> str:
    lines = ["Metadata-Version: 2.1", f"Name: {NAME}", f"Version: {ver}",
             "Summary: The Forgeyard agent: joins a Forgeyard server and runs its work on this machine.",
             "License: Apache-2.0", "Requires-Python: >=3.13"]
    lines += [f"Requires-Dist: {r}" for r in REQUIRES]
    for extra, reqs in EXTRAS.items():
        lines.append(f"Provides-Extra: {extra}")
        lines += [f'Requires-Dist: {r}; extra == "{extra}"' for r in reqs]
    return "\n".join(lines) + "\n"


def _digest(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def build(ver: str | None = None) -> bytes:
    ver = ver or version()
    info = f"{NAME}-{ver}.dist-info"
    entries: list[tuple[str, bytes]] = [(name, path.read_bytes()) for name, path in _files()]
    entries += [
        (f"{info}/METADATA", _metadata(ver).encode()),
        *([(f"{info}/LICENSE", LICENSE.read_bytes())] if LICENSE.is_file() else []),
        (f"{info}/WHEEL", b"Wheel-Version: 1.0\nGenerator: forgeyard\nRoot-Is-Purelib: true\nTag: py3-none-any\n"),
        (f"{info}/entry_points.txt", b"[console_scripts]\nforgeyard = forgeyard.cli:main\n"),
    ]
    record = "".join(f"{name},{_digest(data)},{len(data)}\n" for name, data in entries) + f"{info}/RECORD,,\n"
    entries.append((f"{info}/RECORD", record.encode()))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zi = zipfile.ZipInfo(name, date_time=_EPOCH)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, data)
    return buf.getvalue()


@lru_cache(maxsize=1)
def cached() -> tuple[str, bytes, str]:
    """(file name, bytes, sha256 hex) of the running version's wheel, built once per process."""
    data = build()
    return filename(), data, hashlib.sha256(data).hexdigest()


def main(argv: list[str]) -> int:
    out = Path(argv[0] if argv else "dist")
    out.mkdir(parents=True, exist_ok=True)
    path = out / filename()
    path.write_bytes(build())
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
