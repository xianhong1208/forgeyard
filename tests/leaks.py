"""Processes a test leaves running: found, killed and reported.

Process workloads run in their own session (they must survive the agent), so a test that fails
half-way leaves its engine running. On 2026-11-10 two stand-in engines from FY-RG-008 and FY-RG-009
(one listening on 0.0.0.0) were left running as `nobody`: their scenario timed out on a busy host
between starting a replica and stopping it, and the `finally` only removed the directory.

A process belongs to a test when its command line or working directory is inside that test's
tmp_path or one of the world-readable directories it made (`world_dir`, /tmp/fy-test-*). Every
such directory carries the pid of the pytest run that made it (.owner), so a run never touches
another run's processes.
"""

from __future__ import annotations

import os
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import Iterable

PREFIX = "fy-test-"
OWNER = ".owner"
# Directories made by this run's world_dir(), in order.
made: list[Path] = []


def world_dir(base: str = "/tmp") -> Path:
    """A directory an unprivileged workload user can enter (the pytest tmp dir is 0700)."""
    d = Path(tempfile.mkdtemp(prefix=PREFIX, dir=base))
    os.chmod(d, 0o755)
    (d / OWNER).write_text(str(os.getpid()))
    made.append(d)
    return d


def processes_in(dirs: Iterable[Path]) -> list[tuple[int, str]]:
    """(pid, command line) of every process whose command line or working directory is inside
    one of `dirs` (this process and zombies excluded)."""
    roots = [str(d) for d in dirs]
    if not roots:
        return []
    me = os.getpid()
    out = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            cmd = Path(entry.path, "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue
        try:  # not always readable, even as root (another user's process in a container without ptrace)
            cwd = os.readlink(os.path.join(entry.path, "cwd")).removesuffix(" (deleted)")
        except OSError:
            cwd = ""
        if not cmd:
            continue  # a zombie, or a kernel thread
        if any(r in cmd or cwd == r or cwd.startswith(r + "/") for r in roots):
            out.append((int(entry.name), cmd))
    return out


def kill(found: list[tuple[int, str]], wait: float = 3.0) -> None:
    for pid, _ in found:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    deadline = time.monotonic() + wait
    pids = {pid for pid, _ in found}
    while pids and time.monotonic() < deadline:
        pids = {p for p in pids if Path(f"/proc/{p}").exists() and _not_zombie(p)}
        time.sleep(0.02)


def _not_zombie(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()[0] != "Z"
    except OSError:
        return False


def cleanup(d: Path) -> list[tuple[int, str]]:
    """Kill what still runs from `d`, then remove it. Returns what had to be killed."""
    found = processes_in([d])
    kill(found)
    shutil.rmtree(d, ignore_errors=True)
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stale_dirs(base: str = "/tmp", min_age: float = 3600.0) -> list[Path]:
    """/tmp/fy-test-* left by runs that are gone: their .owner pid is dead, or (made before .owner
    existed) they are older than an hour. Another run's live directories are never listed."""
    out = []
    for d in Path(base).glob(PREFIX + "*"):
        if not d.is_dir() or d in made:
            continue
        try:
            owner = int((d / OWNER).read_text().strip())
        except (OSError, ValueError):
            owner = None
        try:
            age = time.time() - d.stat().st_mtime
        except OSError:
            continue
        if (owner is not None and not _alive(owner)) or (owner is None and age > min_age):
            out.append(d)
    return out


def check(dirs: Iterable[Path]) -> list[tuple[int, str]]:
    """What still runs from `dirs`, killed. The fixtures fail the test (or the run) when it is not empty."""
    found = processes_in(dirs)
    kill(found)
    return found


def describe(found: list[tuple[int, str]]) -> str:
    return "; ".join(f"pid {pid}: {cmd[:160]}" for pid, cmd in found)
