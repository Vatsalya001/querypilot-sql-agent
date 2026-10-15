"""API error type and exception handlers.

Every error leaves the API as JSON shaped ``{"detail": str, "code": str}`` (plus ``retry_after`` on
rate limits and ``errors`` on validation failures). Stack traces never reach the client.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

INTERNAL_ERROR_DETAIL = "Internal server error"

AGENT_ERROR_STATUS: dict[str, int] = {
    "llm_not_configured": 503,
    "rate_limited": 429,
    "llm_error": 502,
    "internal": 500,
}
"""HTTP status for each ``AgentRunError.code`` (unknown codes map to 500)."""

_HTTP_ERROR_CODES: dict[int, str] = {404: "not_found", 405: "method_not_allowed", 413: "payload_too_large"}


class APIError(Exception):
    """An error with a stable machine-readable ``code``, rendered as a JSON error body."""

    def __init__(self, status_code: int, code: str, detail: str, *, retry_after: int | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.retry_after = retry_after

    def to_response(self) -> JSONResponse:
        """Render the error as a JSON response (with a ``Retry-After`` header when applicable)."""
        body: dict[str, Any] = {"detail": self.detail, "code": self.code}
        headers: dict[str, str] = {}
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
            headers["Retry-After"] = str(self.retry_after)
        return JSONResponse(status_code=self.status_code, content=body, headers=headers)


def agent_error_status(code: str) -> int:
    """Return the HTTP status for an agent error code."""
    return AGENT_ERROR_STATUS.get(code, 500)


def http_error_code(status_code: int) -> str:
    """Return the machine-readable ``code`` used for a plain HTTP error status."""
    return _HTTP_ERROR_CODES.get(status_code, "http_error")


def _format_location(loc: tuple[Any, ...] | list[Any]) -> str:
    """Turn a validation location such as ``("body", "history", 3, "role")`` into ``history.3.role``.

    An error about the body as a whole (location ``("body",)``) is reported as ``body``.
    """
    parts = [str(part) for part in loc if part != "body"]
    if not parts and list(loc) == ["body"]:
        return "body"
    return ".".join(parts)


def _simplify_error(err: dict[str, Any]) -> dict[str, str]:
    """Reduce one Pydantic/FastAPI error to ``field``, ``message`` and ``type`` (the input is dropped)."""
    error_type = str(err.get("type", "value_error"))
    loc = tuple(err.get("loc", ()))
    if error_type == "json_invalid":  # FastAPI reports ("body", <character offset>)
        reason = str((err.get("ctx") or {}).get("error") or "malformed JSON")
        position = f" at position {loc[1]}" if len(loc) > 1 else ""
        return {"field": "body", "message": f"invalid JSON ({reason}{position})", "type": error_type}
    return {
        "field": _format_location(loc),
        "message": str(err.get("msg", "Invalid value")),
        "type": error_type,
    }


def _summarize_validation_errors(errors: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    """Build a one-line human summary plus a JSON-safe error list (request input is never echoed)."""
    simplified = [_simplify_error(err) for err in errors]
    summary = "; ".join(f"{e['field']}: {e['message']}" if e["field"] else e["message"] for e in simplified)
    return summary or "Invalid request.", simplified


async def _handle_api_error(request: Request, exc: APIError) -> JSONResponse:
    return exc.to_response()


async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    detail, errors = _summarize_validation_errors(list(exc.errors()))
    return JSONResponse(
        status_code=422, content={"detail": detail, "code": "validation_error", "errors": errors}
    )


async def _handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": http_error_code(exc.status_code)},
        headers=exc.headers,
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # Starlette re-raises the exception after this response is sent, so the server logs the traceback.
    logger.error("Unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": INTERNAL_ERROR_DETAIL, "code": "internal"})


def register_error_handlers(app: FastAPI) -> None:
    """Install the JSON error handlers on ``app``."""
    app.add_exception_handler(APIError, _handle_api_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _handle_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _handle_http_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _handle_unexpected_error)
