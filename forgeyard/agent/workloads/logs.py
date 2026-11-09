"""Process output on disk: kept to the last N MB per replica, read back as a tail.

The process writes straight to `output.log` (an O_APPEND descriptor it inherited), so its
output keeps landing on disk while the agent is down. The agent trims by copy-and-truncate:
when the file passes half the budget it is copied to `output.log.1` and emptied, so the pair
never holds more than the budget. (A line written in the instant between copy and truncate can
be lost: the same trade-off as logrotate's copytruncate.)
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

LOG_NAME = "output.log"


def paths(log_dir: Path) -> tuple[Path, Path]:
    return log_dir / LOG_NAME, log_dir / (LOG_NAME + ".1")


def trim(log_dir: Path, max_bytes: int) -> bool:
    current, older = paths(log_dir)
    try:
        size = current.stat().st_size
    except FileNotFoundError:
        return False
    if size <= max_bytes // 2:
        return False
    shutil.copyfile(current, older)
    os.chmod(older, 0o600)
    with open(current, "r+b") as fh:
        fh.truncate(0)
    return True


def _tail_bytes(path: Path, limit: int) -> bytes:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read()
    except FileNotFoundError:
        return b""


def tail(log_dir: Path, lines: int, max_chars: int = 190_000) -> tuple[str, bool]:
    """The last `lines` lines of output (both files), and whether older output was cut off."""
    current, older = paths(log_dir)
    data = _tail_bytes(current, max_chars * 2)
    if data.count(b"\n") < lines and len(data) < max_chars * 2:
        data = _tail_bytes(older, max_chars * 2) + data
    text = data.decode("utf-8", "replace")
    parts = text.splitlines()
    truncated = len(parts) > lines
    parts = parts[-lines:]
    out = "\n".join(parts)
    if len(out) > max_chars:
        out, truncated = out[-max_chars:], True
    return out, truncated
