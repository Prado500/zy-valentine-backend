"""Dependencias de FastAPI: construyen los servicios y guardan la puerta.

Esta es la **frontera**: aquí se abre la sesión de base de datos y se ensamblan los
servicios de aplicación con los puertos que viven en ``app.state``. Ningún router
vuelve a ver una ``AsyncSession`` ni un repositorio; recibe ya el servicio.

``get_db`` sigue siendo una dependencia de FastAPI, así que las dos fábricas de
servicios comparten la **misma** sesión dentro de una petición: FastAPI cachea cada
dependencia por petición, y con ello se mantiene una única unidad de trabajo.
"""

import secrets

from fastapi import Depends, Request

from app.core.errors import ApiError
from app.core.security import verify_csrf
from app.models.user import User
from app.services.accounts import AccountService
from app.services.commerce import CommerceService


async def get_db(request: Request):
    """Sesión de la petición. Solo la consumen las fábricas de servicios de abajo."""
    async with request.app.state.sessions() as session:
        yield session


async def commerce_service(request: Request, db=Depends(get_db)) -> CommerceService:
    state = request.app.state
    return CommerceService(
        db=db,
        settings=state.settings,
        storage=state.storage,
        mailer=state.mailer,
        payments=state.payments,
        queue=state.letter_queue,
    )


async def account_service(request: Request, db=Depends(get_db)) -> AccountService:
    state = request.app.state
    return AccountService(
        db=db,
        settings=state.settings,
        hash_limiter=state.hash_limiter,
        google_limiter=state.google_limiter,
        google_verifier=state.google_verifier,
    )


def csrf_guard(request: Request) -> str:
    settings = request.app.state.settings
    origin = request.headers.get("origin")
    if origin and origin not in settings.cors_origins:
        raise ApiError(403, "CSRF_INVALID", "Origen no permitido.")
    token = request.headers.get("x-csrf-token", "")
    cookie = request.cookies.get(settings.csrf_cookie, "")
    if (
        not token
        or len(token) > 1024
        or not token.isascii()
        or not cookie.isascii()
        or not secrets.compare_digest(token, cookie)
        or not verify_csrf(settings.session_secret.get_secret_value(), token, settings.csrf_seconds)
    ):
        raise ApiError(403, "CSRF_INVALID", "Actualiza la protección de sesión e intenta de nuevo.")
    return token


async def auth_limit(request: Request):
    request.app.state.rate_limiter.check(request.client.host if request.client else "unknown")


async def current_user(
    request: Request, service: AccountService = Depends(account_service)
) -> User:
    """Usuario de la sesión en curso, o 401. La consulta vive en el repositorio."""
    return await service.session_user(
        request.cookies.get(request.app.state.settings.session_cookie)
    )
