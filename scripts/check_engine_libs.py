"""Check that the built-in recipes' process engines find every shared library they link inside the agent image.

    uv run --no-sync python scripts/check_engine_libs.py forgeyard-agent:ci            # exits 1 on any problem
    uv run --no-sync python scripts/check_engine_libs.py IMAGE --arch aarch64           # another architecture

For each process variant of forgeyard/recipes/*.yaml that runs on the architecture (default: this machine's), it
downloads the engine archive, checks its SHA-256, unpacks it the way the agent does (forgeyard/agent/models.py), and
runs `ldd` on every ELF file in it inside IMAGE (`docker run`, the unpacked archive mounted read-only). A library that
`ldd` reports as "not found" fails the check, and so does an archive whose engine program is not among the files
checked. Every ELF file, not only the program: llama.cpp and stable-diffusion.cpp load their CPU backends
(libggml-cpu-*.so) with dlopen, which `ldd` of the program does not follow.

Why (1.5.5): the agent image had no libgomp.so.1, which both release archives link, so every CPU process deployment
on a machine whose agent runs from the image exited with code 127. The acceptance machines run the agent from a
source checkout on a host that has it, so no run caught it. CI runs this after building the image (agent-image job).
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import NamedTuple

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from forgeyard.agent.models import unzip  # noqa: E402  (the agent's own unpacking)
from forgeyard.models.recipes import builtin_recipes  # noqa: E402

PLATFORMS = {"x86_64": "linux/amd64", "aarch64": "linux/arm64"}
MOUNT = "/engine"
# Runs inside the image: every ELF file under MOUNT, then what ldd says about it.
IN_IMAGE = r"""
import os, subprocess
for root, dirs, files in os.walk(%r):
    dirs.sort()
    for name in sorted(files):
        path = os.path.join(root, name)
        if os.path.islink(path):
            continue
        with open(path, "rb") as f:
            if f.read(4) != b"\x7fELF":
                continue
        r = subprocess.run(["ldd", path], capture_output=True, text=True)
        print("== " + path, flush=True)
        print(r.stdout + r.stderr, end="", flush=True)
""" % MOUNT
NOT_FOUND_RE = re.compile(r"^\s*(\S+)\s+=>\s+not found\s*$")


class ProcessEngine(NamedTuple):
    recipe: str
    variant: str
    url: str
    sha256: str
    binary: str

    @property
    def label(self) -> str:
        return f"{self.recipe}/{self.variant}"


def process_engines(arch: str) -> list[ProcessEngine]:
    """The built-in recipes' process variants that run on this machine architecture ("x86_64", "aarch64")."""
    out = []
    for name, (recipe, _) in builtin_recipes().items():
        for v in recipe.variants:
            if v.engine is None:  # a container variant, or a process variant that runs a command of the machine's
                continue
            if v.when.arch and arch not in v.when.arch:
                continue
            out.append(ProcessEngine(name, v.name, v.engine.url, v.engine.sha256, v.engine.binary))
    return out


def parse_report(text: str) -> dict[str, list[str]]:
    """{file inside the image: [libraries ldd did not find]} from the IN_IMAGE program's output. A file ldd says is
    static or not dynamic has no missing libraries."""
    out: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("== "):
            current = line[3:]
            out[current] = []
            continue
        m = NOT_FOUND_RE.match(line)
        if m and current is not None and m.group(1) not in out[current]:
            out[current].append(m.group(1))
    return out


def problems(engine: ProcessEngine, report: dict[str, list[str]]) -> list[str]:
    """What is wrong with one unpacked archive in the image (none: it is fine): each missing library once, with the
    files that need it."""
    needed: dict[str, list[str]] = {}
    for path, libs in report.items():
        for lib in libs:
            needed.setdefault(lib, []).append(path.removeprefix(MOUNT + "/"))
    out = []
    for lib, files in sorted(needed.items()):
        shown = ", ".join(files[:3]) + (f" and {len(files) - 3} more" if len(files) > 3 else "")
        out.append(f"{lib} not found, needed by {len(files)} {'file' if len(files) == 1 else 'files'}: {shown}")
    if f"{MOUNT}/{engine.binary}" not in report:
        out.append(f"{engine.binary} (the engine program) was not checked: not in the archive, or not an ELF file")
    return out


def download(engine: ProcessEngine, dest: Path) -> None:
    digest = hashlib.sha256()
    with httpx.stream("GET", engine.url, follow_redirects=True, timeout=120) as r, open(dest, "wb") as out:
        r.raise_for_status()
        for chunk in r.iter_bytes(1024 * 1024):
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != engine.sha256:
        raise SystemExit(f"{engine.label}: SHA-256 mismatch for {engine.url} (got {digest.hexdigest()[:12]}…)")


def unpack(archive: Path, dest: Path) -> None:
    """As the agent unpacks an engine (forgeyard/agent/models.py, ModelCache's engine fetch)."""
    if zipfile.is_zipfile(archive):
        unzip(archive, dest)
    else:
        with tarfile.open(archive) as tar:
            tar.extractall(dest, filter="data")


def ldd_in_image(image: str, arch: str, unpacked: Path) -> str:
    argv = ["docker", "run", "--rm", "--platform", PLATFORMS[arch], "--network", "none",
            "-v", f"{unpacked}:{MOUNT}:ro", "--entrypoint", "python", image, "-c", IN_IMAGE]
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"ldd inside {image} failed (exit {r.returncode}): {(r.stderr or r.stdout)[-500:]}")
    return r.stdout


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("image", help="the agent image, e.g. forgeyard-agent:ci")
    ap.add_argument("--arch", default=platform.machine(), choices=sorted(PLATFORMS),
                    help="the machine architecture to check (default: this machine's)")
    args = ap.parse_args(argv)
    engines = process_engines(args.arch)
    if not engines:
        print(f"no built-in process engines for {args.arch}")
        return 0
    failed = 0
    for engine in engines:
        with tempfile.TemporaryDirectory(prefix="fy-engine-libs-") as tmp:
            archive, unpacked = Path(tmp) / "archive", Path(tmp) / "engine"
            unpacked.mkdir()
            download(engine, archive)
            unpack(archive, unpacked)
            report = parse_report(ldd_in_image(args.image, args.arch, unpacked))
        found = problems(engine, report)
        if found:
            failed += 1
            print(f"FAIL {engine.label} ({engine.url.rsplit('/', 1)[-1]}, {args.arch}):")
            for line in found:
                print(f"     {line}")
        else:
            print(f"ok   {engine.label} ({engine.url.rsplit('/', 1)[-1]}, {args.arch}): "
                  f"{len(report)} ELF files, every library found")
    if failed:
        print(f"{failed} of {len(engines)} engines miss libraries in {args.image}: add them to Dockerfile.agent")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
