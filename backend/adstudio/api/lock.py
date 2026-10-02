"""App lock routes. These are the only /api routes reachable while locked."""
from fastapi import APIRouter, Body, Request

from ..core.errors import AppError

router = APIRouter(prefix="/lock")
LOCK_EXEMPT = ("/api/v1/lock/status", "/api/v1/lock/unlock")


def _c(request: Request):
    return request.app.state.c


def _status(request: Request) -> dict:
    c = _c(request)
    return c.applock.status(float(c.settings.all()["security.idle_lock_minutes"]))


@router.get("/status")
def status(request: Request):
    c = _c(request)
    c.applock.check_idle(float(c.settings.all()["security.idle_lock_minutes"]))
    return _status(request)


def _pass(body: dict) -> str:
    p = body.get("passphrase")
    if not isinstance(p, str):
        raise AppError("`passphrase` is required", code="invalid_body")
    return p


@router.post("/unlock")
def unlock(request: Request, body: dict = Body(...)):
    _c(request).applock.unlock(_pass(body))
    return _status(request)


@router.post("/lock")
def lock(request: Request):
    _c(request).applock.lock()
    return _status(request)


@router.post("/setup")
def setup(request: Request, body: dict = Body(...)):
    _c(request).applock.setup(_pass(body))
    return _status(request)


@router.post("/disable")
def disable(request: Request, body: dict = Body(...)):
    _c(request).applock.disable(_pass(body))
    return _status(request)
