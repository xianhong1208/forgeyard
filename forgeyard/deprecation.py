"""Apps (user-made workloads that are not models) are deprecated in 1.4 and removed in 1.5 (#88, #89).

Forgeyard is about running and managing models; an app drifts from that. Until 1.5 everything keeps
working, and says so in three places:

- **Headers** on every answer of the deprecated parts, errors included: `Deprecation` (RFC 9745, the
  date it was announced as `@<epoch>`) and `Link: <the guide's reference>; rel="deprecation"`. No
  `Sunset`: 1.5 has no release date yet. The deprecated parts are
  - `apps`: the apps part of `/api/workloads`: creating one, and listing, reading, exposing, stopping,
    starting, deleting one or reading its logs (a model deployment's workload is not deprecated: it is
    how models run);
  - `templates`: `GET /api/workloads/templates`;
  - `x`: the gateway's `/x/<name>/…` (and the console's own route to an exposed vision app).
- **The server log**: a warning per kind at most once an hour, so a busy caller does not flood it.
- **Setup health** (forgeyard/advisor.py, `apps-deprecated`): a warning while any app exists.

A route marks its answer with `flag(request, kind)` as soon as it knows the request is about an app;
`DeprecationMiddleware` adds the headers when the answer starts (also to an error raised after the
flag) and logs. `/x` is marked by its path alone.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from starlette.requests import Request

log = logging.getLogger("forgeyard.deprecation")

REMOVED_IN = "1.5"
# When it was announced (1.4, issue #88). RFC 9745 wants a date, as "@<epoch>".
DEPRECATED_SINCE = datetime(2026, 11, 16, tzinfo=timezone.utc)
DEPRECATION_HEADER = f"@{int(DEPRECATED_SINCE.timestamp())}"
GUIDE = "/guide/en/reference#apps-deprecated"
LINK_HEADER = f'<{GUIDE}>; rel="deprecation"; type="text/html"'
LOG_EVERY_S = 3600.0

KINDS = ("apps", "templates", "x")
WHAT = {
    "apps": "Apps (workloads that are not models) and their part of /api/workloads are",
    "templates": "The app templates (GET /api/workloads/templates) are",
    "x": "The gateway's /x/<name>/ passthrough to apps is",
}
INSTEAD = ("deploy models instead (a model Forgeyard has no engine for runs with a custom engine, Models -> "
           "Engines)")

STATE_KEY = "fy_deprecated"

_lock = threading.Lock()
_last: dict[str, float] = {}


def message(kind: str) -> str:
    return f"{WHAT[kind]} deprecated and will be removed in Forgeyard {REMOVED_IN}: {INSTEAD}. See {GUIDE}"


def warn(kind: str, what: str = "", *, clock: Callable[[], float] = time.monotonic) -> bool:
    """Log the notice for `kind`, at most once per LOG_EVERY_S. Returns True when it was logged now."""
    now = clock()
    with _lock:
        last = _last.get(kind)
        if last is not None and now - last < LOG_EVERY_S:
            return False
        _last[kind] = now
    log.warning("[deprecated] %s%s", message(kind), f" (seen: {what})" if what else "")
    return True


def reset_for_tests() -> None:
    with _lock:
        _last.clear()


def flag(request: Request, kind: str) -> None:
    """Mark this request's answer as deprecated (headers and the log line come from the middleware)."""
    assert kind in KINDS, kind
    request.state.fy_deprecated = kind


def kind_of_path(path: str) -> Optional[str]:
    return "x" if path == "/x" or path.startswith("/x/") else None


class DeprecationMiddleware:
    """Pure ASGI: adds `Deprecation` and `Link` to a flagged answer when it starts, and logs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        by_path = kind_of_path(scope.get("path", ""))

        async def send_marked(message):
            if message.get("type") == "http.response.start":
                kind = by_path or _flagged(state)
                if kind:
                    headers = [(k, v) for k, v in (message.get("headers") or [])
                               if k.lower() not in (b"deprecation",)]
                    headers.append((b"deprecation", DEPRECATION_HEADER.encode()))
                    headers.append((b"link", LINK_HEADER.encode()))
                    message = {**message, "headers": headers}
                    warn(kind, f"{scope.get('method', '')} {scope.get('path', '')}")
            await send(message)

        await self.app(scope, receive, send_marked)


def _flagged(state) -> Optional[str]:
    try:
        return state.get(STATE_KEY) if isinstance(state, dict) else getattr(state, STATE_KEY, None)
    except Exception:  # noqa: BLE001 - a notice never breaks an answer
        return None
