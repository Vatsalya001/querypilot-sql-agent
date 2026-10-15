"""Request body size cap.

Valid chat requests are small (a message of at most ``MAX_INPUT_CHARS`` characters plus a short
history), but without a cap the server buffers a body of any size in memory before validating it. On a
small free-tier container a single oversized upload could exhaust memory, so larger bodies get a 413.
"""

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import http_error_code

MAX_BODY_BYTES = 2 * 1024 * 1024
"""2 MiB: above the largest body ``ChatRequest`` accepts (about 400,000 characters, even as 4-byte UTF-8)."""


class BodySizeLimitMiddleware:
    """Pure ASGI middleware that refuses request bodies larger than ``max_bytes`` with a JSON 413.

    A declared ``Content-Length`` is checked before the app runs. Bodies without one (chunked uploads)
    are counted as they stream in, and going over the cap raises ``HTTPException(413)`` from ``receive``.
    FastAPI re-raises that to the JSON error handlers, so both paths return the same error body.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.detail = f"Request body is too large (limit: {max_bytes:,} bytes)."

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > self.max_bytes:
            response = JSONResponse({"detail": self.detail, "code": http_error_code(413)}, status_code=413)
            await response(scope, receive, send)
            return
        await self.app(scope, self._counting(receive), send)

    def _counting(self, receive: Receive) -> Receive:
        """Wrap ``receive`` so that reading more than ``max_bytes`` of body fails with a 413."""
        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(status_code=413, detail=self.detail)
            return message

        return counting_receive
