"""What every collector returns, and small helpers they share."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Result:
    """One collector's answer. A collector that finds nothing says why in `reason`, never raises."""

    accelerators: list[dict] = field(default_factory=list)
    ok: bool = False
    source: Optional[str] = None
    reason: Optional[str] = None
    raw: Optional[dict] = None

    def status(self) -> dict:
        out: dict[str, Any] = {"ok": self.ok, "count": len(self.accelerators)}
        if self.source:
            out["source"] = self.source
        if self.reason:
            out["reason"] = self.reason
        return out


def text(value: Any) -> Optional[str]:
    """Old pynvml / ctypes return bytes, newer ones str."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    value = str(value).strip().rstrip("\x00")
    return value or None


def number(value: Any) -> Optional[float]:
    """A number from 42, "42", "42 %", {"value": 42, "unit": "%"}; None for "N/A" and the like."""
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().split()[0])
    except (ValueError, IndexError):
        return None


def integer(value: Any) -> Optional[int]:
    n = number(value)
    return None if n is None else int(n)


def error_text(e: BaseException) -> str:
    msg = str(e).strip() or type(e).__name__
    return msg[:200]
