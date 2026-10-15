"""Serve the built single-page app from the same origin as the API.

``/assets`` is mounted as static files. A catch-all GET returns real files from the static root (favicon,
robots.txt, ...) and ``index.html`` for every other non-API path, so client-side routes survive a reload.
Unknown ``/api/*`` paths keep returning JSON 404s.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

_API_PREFIX = "api"


def _is_api_path(path: str) -> bool:
    return path == _API_PREFIX or path.startswith(f"{_API_PREFIX}/")


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
    async def spa(path: str) -> FileResponse:
        if _is_api_path(path):
            raise HTTPException(status_code=404, detail="Not Found")
        if path:
            candidate = (root / path).resolve()
            if candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    logger.info("Serving the web UI from %s", root)
    return True
