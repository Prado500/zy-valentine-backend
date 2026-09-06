import secrets
from datetime import UTC, datetime

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.security import digest, verify_csrf
from app.models.user import AuthSession, User


async def get_db(request: Request):
    async with request.app.state.sessions() as session:
        yield session


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


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = request.cookies.get(request.app.state.settings.session_cookie)
    if token and len(token) <= 128:
        user = await db.scalar(
            select(User)
            .join(AuthSession)
            .where(
                AuthSession.token_hash == digest(token),
                AuthSession.expires_at > datetime.now(UTC),
                User.is_active.is_(True),
            )
        )
        if user:
            return user
    raise ApiError(401, "UNAUTHENTICATED", "Inicia sesión para continuar.")
