from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


async def by_email(db: AsyncSession, email: str) -> User | None:
    return await db.scalar(select(User).where(User.email == email))


async def by_google_sub(db: AsyncSession, subject: str) -> User | None:
    return await db.scalar(select(User).where(User.google_sub == subject))
