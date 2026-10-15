"""FastAPI application factory. ``app.main:app`` is the ASGI entry point.

Run locally: ``cd backend && .venv/bin/python -m uvicorn app.main:app --reload --port 8000``.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.agent.service import SQLAgent
from app.api.body_limit import BodySizeLimitMiddleware
from app.api.errors import register_error_handlers
from app.api.rate_limit import RateLimiter
from app.api.routes import router
from app.api.spa import mount_spa
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: str) -> None:
    """Log to stderr at ``level``; handlers already installed (by uvicorn or pytest) are kept."""
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    logging.basicConfig(level=numeric_level, format=_LOG_FORMAT)
    logging.getLogger("app").setLevel(numeric_level)


def _add_root_info(app: FastAPI) -> None:
    """Without a built UI, answer ``GET /`` with pointers instead of a bare 404."""

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"name": "QueryPilot API", "docs": "/api/docs", "health": "/api/health"}


def create_app(settings: Settings | None = None, *, agent: SQLAgent | None = None) -> FastAPI:
    """Build the FastAPI app.

    ``agent`` is injected by tests; otherwise a real ``SQLAgent`` is built once during startup.
    """
    if settings is None:
        settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        warmup: asyncio.Task[bool] | None = None
        if agent is None:
            app.state.agent = SQLAgent(settings)
            # Prime the local model's prompt cache in the background so the first user requests are as
            # fast as later ones. A no-op for hosted and mock providers, and it never raises.
            warmup = asyncio.create_task(app.state.agent.warm_up(), name="llm-warmup")
        app.state.warmup_task = warmup
        gateway = getattr(app.state.agent, "gateway", None)
        logger.info(
            "QueryPilot API %s ready (provider=%s, model=%s)",
            __version__,
            settings.llm_provider,
            getattr(gateway, "model", settings.llm_model),
        )
        try:
            yield
        finally:
            if warmup is not None and not warmup.done():
                warmup.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await warmup

    app = FastAPI(
        title="QueryPilot API",
        summary="Natural language to validated, read-only SQL with a LangGraph agent.",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.rate_limiter = RateLimiter(
        settings.rate_limit_per_minute, settings.rate_limit_per_day, enabled=settings.rate_limit_enabled
    )
    if agent is not None:
        app.state.agent = agent

    # Added first, so CORS (the outer middleware) also decorates its 413 responses.
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    register_error_handlers(app)
    app.include_router(router)

    static_dir = settings.resolved_static_dir
    if static_dir is None or not mount_spa(app, static_dir):
        _add_root_info(app)
    return app


app = create_app()
