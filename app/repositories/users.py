"""Persistencia de identidad y sesiones. Sin reglas de negocio ni HTTP."""

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import AuthSession, User


async def by_email(db: AsyncSession, email: str) -> User | None:
    return await db.scalar(select(User).where(User.email == email))


async def by_google_sub(db: AsyncSession, subject: str) -> User | None:
    return await db.scalar(select(User).where(User.google_sub == subject))


async def by_session(db: AsyncSession, token_hash: str) -> User | None:
    """Usuario activo dueño de una sesión vigente. La caducidad se filtra en SQL."""
    return await db.scalar(
        select(User)
        .join(AuthSession)
        .where(
            AuthSession.token_hash == token_hash,
            AuthSession.expires_at > datetime.now(UTC),
            User.is_active.is_(True),
        )
    )


async def delete_session(db: AsyncSession, token_hash: str) -> None:
    await db.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash))
    await db.commit()
