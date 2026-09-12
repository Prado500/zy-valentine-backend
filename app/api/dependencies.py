"""Dependencias de FastAPI: construyen los servicios y guardan la puerta.

Esta es la **frontera**: aquí se abre la sesión de base de datos y se ensamblan los
servicios de aplicación con los puertos que viven en ``app.state``. Ningún router
vuelve a ver una ``AsyncSession`` ni un repositorio; recibe ya el servicio.

``get_db`` sigue siendo una dependencia de FastAPI, así que las dos fábricas de
servicios comparten la **misma** sesión dentro de una petición: FastAPI cachea cada
dependencia por petición, y con ello se mantiene una única unidad de trabajo.
"""

import logging
import secrets
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Header, Request

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.security import verify_csrf
from app.models.user import User
from app.services.accounts import AccountService
from app.services.commerce import CommerceService

LOG = logging.getLogger("app.csrf")


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


def _is_own_origin(request: Request, settings: Settings, origin: str) -> bool:
    """¿El ``Origin`` es este mismo backend? Cubre Swagger (``/docs``) y cualquier
    página servida por la API. Un navegador solo puede emitir ese ``Origin`` desde
    una página alojada en este host, así que no abre la puerta a terceros.

    Se compara host contra host y no la URL completa: uvicorn corre con
    ``--no-proxy-headers``, así que dentro del contenedor ``request.url.scheme`` es
    ``http`` aunque el navegador hable HTTPS con Azure.
    """
    parsed = urlsplit(origin)
    schemes = ("https",) if settings.secure_cookies else ("http", "https")
    if parsed.scheme not in schemes or not parsed.netloc or parsed.path:
        return False
    own_hosts = {request.url.netloc.lower()}
    if settings.website_hostname:
        own_hosts.add(settings.website_hostname.lower())
    return parsed.netloc.lower() in own_hosts


def csrf_guard(
    request: Request,
    x_csrf_token: Annotated[
        str, Header(description="Token devuelto por GET /api/v1/auth/csrf.")
    ] = "",
) -> str:
    settings = request.app.state.settings
    origin = request.headers.get("origin")
    if (
        origin
        and origin not in settings.cors_origins
        and not _is_own_origin(request, settings, origin)
    ):
        # Solo al log: la respuesta sigue siendo genérica. El origen y la lista no
        # son secretos, y son justo lo que hace falta para leer el Log stream.
        LOG.warning(
            "CSRF: origen rechazado %r; host=%s; permitidos=%s",
            origin,
            request.url.netloc,
            settings.cors_origins,
        )
        raise ApiError(403, "CSRF_INVALID", "Origen no permitido.")
    # Declarada como Header para que aparezca en OpenAPI y Swagger la pueda enviar.
    token = x_csrf_token
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
