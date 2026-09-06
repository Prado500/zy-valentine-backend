import secrets
import uuid
from datetime import UTC, datetime, timedelta

from anyio import CapacityLimiter, to_thread
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
    owner = await users.by_email(db, email)
    if owner:
        # Solo se devuelve la cuenta si su sub coincide exactamente: dos primeros
        # inicios de sesión simultáneos son la misma identidad, no una vinculación.
        if owner.google_sub != claims["sub"]:
            raise ApiError(
                409, "ACCOUNT_LINK_REQUIRED", "Se requiere vinculación explícita de cuenta."
            )
        if not owner.is_active:
            raise ApiError(401, "INVALID_CREDENTIALS", "No se pudo iniciar sesión.")
        return owner
    name = str(claims.get("name") or email).strip()[:120] or email[:120]
    # ON CONFLICT DO NOTHING (cualquier restricción única): dos primeros inicios de
    # sesión simultáneos del mismo sub no producen error ni una segunda identidad.
    created_id = await db.scalar(
        pg_insert(User)
        .values(
            id=uuid.uuid4(),
            email=email,
            name=name,
            google_sub=claims["sub"],
            email_verified=True,
        )
        .on_conflict_do_nothing()
        .returning(User.id)
    )
    await db.commit()
    if created_id is None:
        user = await users.by_google_sub(db, claims["sub"])
        if not user:
            raise ApiError(
                409, "ACCOUNT_LINK_REQUIRED", "Se requiere vinculación explícita de cuenta."
            )
        if not user.is_active:
            raise ApiError(401, "INVALID_CREDENTIALS", "No se pudo iniciar sesión.")
        return user
    return await db.get(User, created_id)


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
