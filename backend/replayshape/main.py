"""Aplicação FastAPI — Replay Shape (Cloud + Edge no mesmo processo durante o MVP)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api import admin, internal, public
from .api.deps import ctx_of, error_response
from .config import REPO_ROOT, Settings, load_settings
from .services import cleanup, seed
from .services.context import AppContext

log = logging.getLogger("replayshape")
FRONTEND_DIR = REPO_ROOT / "frontend"
ADMIN_DIR = REPO_ROOT / "admin"

CSP = (
    "default-src 'self'; img-src 'self' data:; media-src 'self' blob: https:; style-src 'self'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx = AppContext(settings)
        app.state.ctx = ctx
        cleanup.startup_recovery(ctx)
        seed.ensure_admin(ctx)
        seed.ensure_demo_data(ctx)
        cleanup.check_disk(ctx)
        ctx.buffers.sync()
        if settings.start_background:
            ctx.start_background()
        log.info("Replay Shape %s iniciado (env=%s, demo=%s, pagamento=%s)", __version__, settings.app_env,
                 settings.demo_mode, ctx.provider.name)
        try:
            yield
        finally:
            ctx.shutdown()

    app = FastAPI(
        title="Replay Shape API",
        version=__version__,
        description="Captura e venda automatizada de replays. Endpoints públicos, administrativos e internos.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if not request.url.path.startswith("/api/docs"):
            h.setdefault("Content-Security-Policy", CSP)
        if request.url.path.startswith("/api/"):
            h.setdefault("Cache-Control", "no-store")
        if settings.cookie_secure:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            return error_response(exc.status_code, exc.detail["code"], exc.detail["message"])
        return error_response(exc.status_code, "HTTP_ERROR", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error_response(422, "VALIDATION", "Dados inválidos.")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("erro não tratado em %s", request.url.path)
        return error_response(500, "INTERNAL_ERROR", "Erro interno. Tente novamente.")

    app.include_router(public.router)
    app.include_router(admin.router)
    app.include_router(internal.webhooks)
    app.include_router(internal.internal)

    @app.get("/api/v1/health", tags=["saúde"])
    def health(request: Request):
        ctx = ctx_of(request)
        snaps = ctx.buffers.snapshot()
        return {
            "status": "ok",
            "version": __version__,
            "demo_mode": settings.demo_mode,
            "payment_provider": ctx.provider.name,
            "disk_low": ctx.disk_low,
            "cameras_online": sum(1 for s in snaps.values() if s["state"] == "online"),
            "cameras_total": len(snaps),
        }

    # --- frontend estático -------------------------------------------------
    def spa(_: Request = None):
        return FileResponse(FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    for route in ("/", "/m/{code}", "/o/{order_id}", "/recuperar", "/privacidade"):
        app.add_api_route(route, spa, include_in_schema=False, methods=["GET"])

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def admin_index():
        return FileResponse(ADMIN_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/admin/assets", StaticFiles(directory=ADMIN_DIR), name="admin-assets")
    app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")
    return app


def run() -> None:  # pragma: no cover
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=8000, proxy_headers=True)


if __name__ == "__main__":  # pragma: no cover
    run()
