"""Errors in OpenAI's shape, which every OpenAI client library understands:

    {"error": {"message": "...", "type": "...", "param": null, "code": "..."}}
"""

from __future__ import annotations

from typing import Optional

from starlette.responses import JSONResponse

# HTTP status -> the `type` OpenAI's clients expect for it.
TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "invalid_request_error",
    413: "invalid_request_error",
    421: "invalid_request_error",
    429: "rate_limit_error",
    502: "upstream_error",
    503: "service_unavailable",
    504: "upstream_error",
}


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, *, retry_after: Optional[int] = None,
                 param: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.param = param

    def body(self) -> dict:
        return {"error": {"message": self.message, "type": TYPES.get(self.status, "api_error"),
                          "param": self.param, "code": self.code}}

    def response(self) -> JSONResponse:
        headers = {"Retry-After": str(self.retry_after)} if self.retry_after else None
        return JSONResponse(self.body(), status_code=self.status, headers=headers)
