"""Serve the built single-page app from the same origin as the API.

``/assets`` is mounted as static files. A catch-all GET returns real files from the static root (favicon,
robots.txt, ...) and ``index.html`` for every other non-API path, so client-side routes survive a reload.
Unknown ``/api/*`` paths keep returning JSON 404s, and API routes keep their JSON 405 for other methods.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Match
from starlette.types import Scope

logger = logging.getLogger(__name__)

_API_PREFIX = "api"


def _is_api_path(path: str) -> bool:
    return path == _API_PREFIX or path.startswith(f"{_API_PREFIX}/")


_HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")


def _api_methods(app: FastAPI, scope: Scope, catch_all: object) -> list[str]:
    """Methods for which another route of ``app`` matches the request path (empty: no such route)."""
    routes = [route for route in app.router.routes if getattr(route, "endpoint", None) is not catch_all]
    return [
        method for method in _HTTP_METHODS
        if any(route.matches({**scope, "method": method})[0] is Match.FULL for route in routes)
    ]


def mount_spa(app: FastAPI, static_dir: Path) -> bool:
    """Serve the SPA built into ``static_dir``; call it after the API routes are registered.

    Returns False (and serves nothing) when ``static_dir`` has no ``index.html``.
    """
    root = static_dir.resolve()
    index = root / "index.html"
    if not index.is_file():
        logger.warning("No index.html in %s; the web UI is not served", root)
        return False
    if (root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa(path: str, request: Request) -> FileResponse:
        if _is_api_path(path):
            # This catch-all also matches API routes that exist for other methods (GET /api/chat): keep their 405.
            allowed = _api_methods(app, request.scope, spa)
            if allowed:
                raise HTTPException(status_code=405, detail="Method Not Allowed", headers={"Allow": ", ".join(allowed)})
            raise HTTPException(status_code=404, detail="Not Found")
        if path:
            try:
                candidate = (root / path).resolve()
                is_file = candidate.is_file() and candidate.is_relative_to(root)
            except (OSError, ValueError):  # NUL byte, over-long name, ...: not a file, so it is a client route
                is_file = False
            if is_file:
                return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    logger.info("Serving the web UI from %s", root)
    return True
