"""Apps were removed in 1.5.3 (#89), after being deprecated in 1.4 (#88).

An app was a workload that is not a model deployment: someone's own container or process, made through
`POST /api/workloads`, perhaps from a template and exposed behind the gateway at `/x/<name>/`. Forgeyard is
about running and managing models, so 1.5.3 removed them: the upgrade (migration 0019) stops and deletes
the apps that were left, writing one audit entry per app (and per API key that could call `/x/`), and the
parts of the API that only apps used are gone.

So that a script or a program that still calls them learns why, instead of a bare 404, those addresses
answer **410 Gone** for the rest of 1.5.x (they become ordinary 404s in 1.6):

- `apps`: creating an app (`POST /api/workloads`), exposing one (`PUT /api/workloads/{id}/gateway`) and
  stopping, starting or deleting one (`POST /api/workloads/{id}/stop|start`, `DELETE /api/workloads/{id}`);
  a model deployment's workload is stopped, started and deleted through its deployment, and the answer
  names it;
- `templates`: `GET /api/workloads/templates`;
- `x`: the gateway's `/x` and `/x/…`, any method, in the OpenAI error shape the gateway always used;
- `vision-apps`: the console's route to an exposed vision app, `POST /api/playground/vision/<name>/<task>`.

Reading the model deployments' workloads (`GET /api/workloads`, `GET /api/workloads/{id}` and its replicas'
logs) stays: that is how the console shows what runs where.

Every 410 carries `Deprecation` (RFC 9745, the date the removal was announced) and a `Link` to the guide's
reference, which says what to use instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from starlette.responses import JSONResponse

REMOVED_IN = "1.5.3"
# When the removal was announced (1.4, #88). RFC 9745 wants a date, as "@<epoch>".
DEPRECATED_SINCE = datetime(2026, 11, 16, tzinfo=timezone.utc)
DEPRECATION_HEADER = f"@{int(DEPRECATED_SINCE.timestamp())}"
GUIDE = "/guide/en/reference#apps-removed"
RELEASE_NOTES = f"https://github.com/xianhong1208/forgeyard/releases/tag/v{REMOVED_IN}"
LINK_HEADER = f'<{GUIDE}>; rel="deprecation"; type="text/html", <{RELEASE_NOTES}>; rel="help"'
CODE = "apps_removed"
# What the upgrade writes in the audit log: one entry per app it removed, one per key that could call /x/.
AUDIT_APP = "workload.removed_in_upgrade"
AUDIT_KEY = "api_key.x_access_removed"

WHAT = {
    "apps": "Apps (workloads that are not models) and their part of /api/workloads were",
    "templates": "The app templates (GET /api/workloads/templates) were",
    "x": "The gateway's /x/<name>/ passthrough to apps was",
    "vision-apps": "Exposed vision apps were",
}
INSTEAD = {
    "apps": "deploy a model instead (a model Forgeyard has no engine for runs with a custom engine: Models, "
            "Engines)",
    "templates": "deploy a vision model from Models instead (the built-in vision engine runs detection, "
                 "segmentation, depth and image classification models)",
    "x": "call a model deployment at /v1 instead",
    "vision-apps": "deploy a vision model from Models and use /api/playground/vision/<task> or /v1/vision/<task>",
}


def message(kind: str, extra: str = "") -> str:
    return (f"{WHAT[kind]} removed in Forgeyard {REMOVED_IN}: {INSTEAD[kind]}.{(' ' + extra) if extra else ''} "
            f"See {GUIDE} and the release notes, {RELEASE_NOTES}")


def headers() -> dict[str, str]:
    return {"Deprecation": DEPRECATION_HEADER, "Link": LINK_HEADER}


def body(kind: str, extra: str = "", **more) -> dict:
    return {"code": CODE, "message": message(kind, extra), "removed_in": REMOVED_IN, "see": GUIDE,
            "release_notes": RELEASE_NOTES, **more}


def gone(kind: str, extra: str = "", **more) -> JSONResponse:
    """410 in the console API's shape ({"detail": {code, message, …}})."""
    assert kind in WHAT, kind
    return JSONResponse({"detail": body(kind, extra, **more)}, status_code=410, headers=headers())


def gone_gateway(kind: str = "x") -> JSONResponse:
    """410 in the gateway's (OpenAI's) shape ({"error": {message, type, param, code, …}}), for /x/."""
    b = body(kind)
    err = {"message": b.pop("message"), "type": "invalid_request_error", "param": None, "code": b.pop("code"), **b}
    return JSONResponse({"error": err}, status_code=410, headers=headers())


def deployment_hint(dep: Optional[tuple]) -> str:
    """For the workload of a model deployment: where to stop, start or delete it instead."""
    if dep is None:
        return ""
    return (f"This workload belongs to the deployment {dep[1]}: stop, start or delete the deployment instead "
            f"(/api/deployments/{dep[0]}).")
