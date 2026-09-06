import secrets
from datetime import UTC, datetime, timedelta

from anyio import CapacityLimiter, to_thread
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.security import DUMMY_HASH, digest, password_hasher
from app.models.user import AuthSession, User
from app.repositories import users
from app.schemas.auth import Login, Register


async def register(db: AsyncSession, payload: Register, limiter: CapacityLimiter) -> User:
    if await users.by_email(db, payload.email):
        raise ApiError(409, "EMAIL_IN_USE", "No se puede registrar ese correo.")
    hashed = await to_thread.run_sync(password_hasher.hash, payload.password, limiter=limiter)
    user = User(email=payload.email, name=payload.name, password_hash=hashed)
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ApiError(409, "EMAIL_IN_USE", "No se puede registrar ese correo.") from None
    await db.refresh(user)
    return user


async def login(db: AsyncSession, payload: Login, limiter: CapacityLimiter) -> User:
    user = await users.by_email(db, payload.email)
    valid = await to_thread.run_sync(
        password_hasher.verify,
        payload.password,
        user.password_hash if user and user.password_hash else DUMMY_HASH,
        limiter=limiter,
    )
    if not valid or not user or not user.password_hash or not user.is_active:
        raise ApiError(401, "INVALID_CREDENTIALS", "Correo o contraseña incorrectos.")
    return user


async def google_user(db: AsyncSession, claims: dict) -> User:
    user = await users.by_google_sub(db, claims["sub"])
    if user:
        if not user.is_active:
            raise ApiError(401, "INVALID_CREDENTIALS", "No se pudo iniciar sesión.") from None
        return user
    try:
        email = str(TypeAdapter(EmailStr).validate_python(claims.get("email"))).casefold()
    except ValidationError:
        raise ApiError(
            401, "INVALID_GOOGLE_TOKEN", "Google no proporcionó un correo válido."
        ) from None
    if await users.by_email(db, email):
        raise ApiError(409, "ACCOUNT_LINK_REQUIRED", "Se requiere vinculación explícita de cuenta.")
    name = str(claims.get("name") or email).strip()[:120] or email[:120]
    user = User(email=email, name=name, google_sub=claims["sub"], email_verified=True)
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # Two first logins for the same verified subject may race.
        user = await users.by_google_sub(db, claims["sub"])
        if not user:
            raise ApiError(
                409, "ACCOUNT_LINK_REQUIRED", "Se requiere vinculación explícita de cuenta."
            ) from None
        if not user.is_active:
            raise ApiError(401, "INVALID_CREDENTIALS", "No se pudo iniciar sesión.") from None
    await db.refresh(user)
    return user


async def new_session(db: AsyncSession, user: User, previous: str | None, minutes: int) -> str:
    if previous:
        await db.execute(delete(AuthSession).where(AuthSession.token_hash == digest(previous)))
    token = secrets.token_urlsafe(32)
    db.add(
        AuthSession(
            token_hash=digest(token),
            user_id=user.id,
            expires_at=datetime.now(UTC) + timedelta(minutes=minutes),
        )
    )
    await db.commit()
    return token
