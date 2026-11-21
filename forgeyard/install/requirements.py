"""The agent's dependencies, pinned with their hashes (1.4.5, #172), for install.sh.

install.sh installs the agent's wheel (checked against its SHA-256) without dependencies, and the dependencies
from these files with `--require-hashes`: each one at the version uv.lock resolved, accepted only with one of the
SHA-256 hashes PyPI published for its files (every platform's wheel and the sdist, as uv.lock records them). A
file changed on PyPI or on the way is refused, and nothing outside the list is installed.

    python -m forgeyard.install.requirements     # rewrite both files from uv.lock (after `uv lock`)

The files live in the package (the server serves them; a Docker image has no uv.lock), and a test checks that
they are what uv.lock gives (tests/test_install.py, FY-INST-020).
"""

from __future__ import annotations

import hashlib
import re
import sys
import tomllib
from functools import lru_cache
from pathlib import Path

from forgeyard.install import wheel

HERE = Path(__file__).resolve().parent
LOCK = HERE.parent.parent / "uv.lock"
# name served at /install/<name> -> the extra it is for ("" = the agent itself)
FILES = {"agent-requirements.txt": "", "agent-requirements-nvidia.txt": "nvidia"}
HEADER = ("# The Forgeyard agent's dependencies{what}, pinned with their hashes: generated from uv.lock by\n"
          "# `python -m forgeyard.install.requirements`. install.sh installs them with --require-hashes.\n")


def _name(requirement: str) -> str:
    return re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0].strip().lower().replace("_", "-")


def _closure(packages: dict[str, dict], roots: list[str]) -> dict[str, set[str]]:
    """Each package reached from `roots`, with the markers of the paths that reach it ("" = always)."""
    out: dict[str, set[str]] = {}
    todo = [(r, "") for r in roots]
    while todo:
        name, marker = todo.pop()
        if name not in packages:
            raise ValueError(f"{name} is not in uv.lock")
        seen = out.setdefault(name, set())
        if marker in seen or "" in seen:
            continue
        seen.add(marker)
        for dep in packages[name].get("dependencies", []):
            m = dep.get("marker", "")
            both = f"({marker}) and ({m})" if marker and m else marker or m
            todo.append((dep["name"], both))
    return out


def generate(lock_text: str, extra: str = "") -> str:
    lock = tomllib.loads(lock_text)
    packages: dict[str, dict] = {}
    for p in lock["package"]:
        if p["name"] in packages:
            raise ValueError(f"uv.lock has {p['name']} more than once; pin it for the agent by hand")
        packages[p["name"]] = p
    roots = [_name(r) for r in (wheel.EXTRAS[extra] if extra else wheel.REQUIRES)]
    base = set(_closure(packages, [_name(r) for r in wheel.REQUIRES])) if extra else set()
    lines = [HEADER.format(what=f" for the [{extra}] extra" if extra else "")]
    for name, markers in sorted(_closure(packages, roots).items()):
        if name in base:
            continue  # in the agent's own file already
        p = packages[name]
        if "registry" not in p.get("source", {}):
            raise ValueError(f"{name} does not come from a package index")
        hashes = sorted({f["hash"] for f in [*p.get("wheels", []), *([p["sdist"]] if "sdist" in p else [])]})
        if not hashes or not all(h.startswith("sha256:") for h in hashes):
            raise ValueError(f"uv.lock has no SHA-256 for {name}")
        marker = "" if "" in markers else " or ".join(f"({m})" for m in sorted(markers))
        line = f"{name}=={p['version']}" + (f" ; {marker}" if marker else "")
        lines.append(" \\\n    ".join([line, *(f"--hash={h}" for h in hashes)]) + "\n")
    return "".join(lines)


@lru_cache(maxsize=None)
def served(name: str) -> tuple[bytes, str]:
    """(bytes, sha256 hex) of a file install.sh downloads."""
    data = (HERE / name).read_bytes()
    return data, hashlib.sha256(data).hexdigest()


def main(argv: list[str]) -> int:
    text = (Path(argv[0]) if argv else LOCK).read_text()
    for name, extra in FILES.items():
        (HERE / name).write_text(generate(text, extra))
        print(HERE / name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
