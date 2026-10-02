"""FastAPI app factory: security middleware, RFC 7807 errors, routers, optional static SPA."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .. import __version__
from ..core.container import Container
from ..core.errors import AppError
from ..core.security import COOKIE_NAME
from . import automation, chat, documents, knowledge, lock, ops, organization, review, routes, search, types_fields

STATIC_DIR = Path(__file__).parent.parent / "static"  # frontend/dist copied here at release time


def _problem(status: int, code: str, title: str, detail: str) -> JSONResponse:
    return JSONResponse({"type": f"urn:adstudio:{code}", "title": title, "detail": detail, "code": code},
                        status_code=status, media_type="application/problem+json")


def create_app(container: Container) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        container.stop()

    app = FastAPI(title="AI Document Studio", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.c = container
    auth = container.auth

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not auth.host_ok(request.headers.get("host")):
            return _problem(403, "bad_host", "Forbidden", "Unexpected Host header")
        if not auth.origin_ok(request.headers.get("origin")):
            return _problem(403, "bad_origin", "Forbidden", "Cross-origin request rejected")
        if request.url.path.startswith("/api/") and not auth.valid_session(request.cookies.get(COOKIE_NAME)):
            return _problem(401, "unauthenticated", "Unauthorized", "Open the app via the launcher link")
        al = container.applock
        if request.url.path.startswith("/api/") and al.enabled:
            al.check_idle(float(container.settings.all()["security.idle_lock_minutes"]))
            if al.locked and request.url.path not in lock.LOCK_EXEMPT:
                return _problem(423, "locked", "Locked", "The app is locked. Enter your passphrase to continue.")
            if request.url.path not in ("/api/v1/events", "/api/v1/lock/status"):
                al.touch()  # background polling and the SSE stream do not count as user activity
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        return resp

    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError):
        return _problem(exc.status, exc.code, exc.__class__.__name__, exc.detail)

    app.include_router(routes.router, prefix="/api/v1")
    app.include_router(documents.router, prefix="/api/v1")
    app.include_router(types_fields.router, prefix="/api/v1")
    app.include_router(search.router, prefix="/api/v1")
    app.include_router(chat.router, prefix="/api/v1")
    app.include_router(review.router, prefix="/api/v1")
    app.include_router(organization.router, prefix="/api/v1")
    app.include_router(automation.router, prefix="/api/v1")
    app.include_router(knowledge.router, prefix="/api/v1")
    app.include_router(lock.router, prefix="/api/v1")
    app.include_router(ops.router, prefix="/api/v1")

    @app.get("/", include_in_schema=False)
    async def index(request: Request, t: str | None = None):
        if t is not None:  # one-time launcher token -> session cookie, then drop it from the URL
            if not auth.consume_one_time(t):
                return _problem(403, "bad_token", "Forbidden", "Launch link already used or invalid")
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE_NAME, auth.session_token, httponly=True, samesite="strict", path="/")
            return resp
        index_html = STATIC_DIR / "index.html"
        if index_html.exists():
            return FileResponse(index_html)
        return JSONResponse({"app": "AI Document Studio", "version": __version__,
                             "note": "Frontend not built; run `npm run build` in frontend/."})

    if (STATIC_DIR / "assets").exists():
        from fastapi.staticfiles import StaticFiles
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    return app
