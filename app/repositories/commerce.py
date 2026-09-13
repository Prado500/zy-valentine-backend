"""Persistencia del dominio comercial. Sin reglas de negocio ni HTTP."""

import uuid

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.commerce import (
    Letter,
    LetterDelivery,
    LetterPhoto,
    Payment,
    PaymentEvent,
    Purchase,
    UserIdentityDocument,
)


async def identity_by_user(db: AsyncSession, user_id: uuid.UUID) -> UserIdentityDocument | None:
    return await db.get(UserIdentityDocument, user_id)


async def identity_by_hash(db: AsyncSession, document_hash: str) -> UserIdentityDocument | None:
    return await db.scalar(
        select(UserIdentityDocument).where(UserIdentityDocument.document_hash == document_hash)
    )


async def purchase_by_idempotency(
    db: AsyncSession, user_id: uuid.UUID, key: str
) -> Purchase | None:
    return await db.scalar(
        select(Purchase).where(Purchase.user_id == user_id, Purchase.idempotency_key == key)
    )


async def purchase_by_reference(db: AsyncSession, reference: str) -> Purchase | None:
    return await db.scalar(select(Purchase).where(Purchase.external_reference == reference))


async def owned_purchase(
    db: AsyncSession, purchase_id: uuid.UUID, user_id: uuid.UUID
) -> Purchase | None:
    return await db.scalar(
        select(Purchase).where(Purchase.id == purchase_id, Purchase.user_id == user_id)
    )


async def lock_purchase(db: AsyncSession, purchase_id: uuid.UUID) -> Purchase | None:
    """Bloqueo transaccional: dos pestañas o un doble clic se serializan aquí."""
    return await db.scalar(select(Purchase).where(Purchase.id == purchase_id).with_for_update())


async def purchases_of_user(db: AsyncSession, user_id: uuid.UUID) -> list[Purchase]:
    result = await db.scalars(
        select(Purchase).where(Purchase.user_id == user_id).order_by(Purchase.created_at.desc())
    )
    return list(result)


async def payment_of_purchase(db: AsyncSession, purchase_id: uuid.UUID) -> Payment | None:
    return await db.scalar(
        select(Payment)
        .where(Payment.purchase_id == purchase_id)
        .order_by(Payment.verified_at.desc())
        .limit(1)
    )


async def payment_by_provider_id(
    db: AsyncSession, provider: str, provider_payment_id: str
) -> Payment | None:
    return await db.scalar(
        select(Payment).where(
            Payment.provider == provider, Payment.provider_payment_id == provider_payment_id
        )
    )


async def event_exists(db: AsyncSession, provider: str, event_id: str) -> PaymentEvent | None:
    return await db.scalar(
        select(PaymentEvent).where(
            PaymentEvent.provider == provider, PaymentEvent.event_id == event_id
        )
    )


async def letter_of_purchase(db: AsyncSession, purchase_id: uuid.UUID) -> Letter | None:
    return await db.scalar(select(Letter).where(Letter.purchase_id == purchase_id))


async def letters_with_purchase(db: AsyncSession, purchase_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    if not purchase_ids:
        return set()
    result = await db.scalars(
        select(Letter.purchase_id).where(Letter.purchase_id.in_(purchase_ids))
    )
    return set(result)


async def owned_letter(db: AsyncSession, letter_id: uuid.UUID, user_id: uuid.UUID) -> Letter | None:
    return await db.scalar(select(Letter).where(Letter.id == letter_id, Letter.user_id == user_id))


async def letter_by_slug(db: AsyncSession, slug: str) -> Letter | None:
    return await db.scalar(select(Letter).where(Letter.public_slug == slug))


async def letters_of_user(db: AsyncSession, user_id: uuid.UUID) -> list[Letter]:
    result = await db.scalars(
        select(Letter).where(Letter.user_id == user_id).order_by(Letter.created_at.desc())
    )
    return list(result)


async def photos_of_letter(db: AsyncSession, letter_id: uuid.UUID) -> list[LetterPhoto]:
    result = await db.scalars(
        select(LetterPhoto).where(LetterPhoto.letter_id == letter_id).order_by(LetterPhoto.position)
    )
    return list(result)


async def photo_of_letter(
    db: AsyncSession, letter_id: uuid.UUID, photo_id: uuid.UUID
) -> LetterPhoto | None:
    return await db.scalar(
        select(LetterPhoto).where(LetterPhoto.id == photo_id, LetterPhoto.letter_id == letter_id)
    )


async def photo_at_position(
    db: AsyncSession, letter_id: uuid.UUID, position: int
) -> LetterPhoto | None:
    return await db.scalar(
        select(LetterPhoto).where(
            LetterPhoto.letter_id == letter_id, LetterPhoto.position == position
        )
    )


async def deliveries_of_letter(db: AsyncSession, letter_id: uuid.UUID) -> list[LetterDelivery]:
    result = await db.scalars(
        select(LetterDelivery)
        .where(LetterDelivery.letter_id == letter_id)
        .order_by(LetterDelivery.created_at.desc())
    )
    return list(result)


def dedications_statement(user_id: uuid.UUID) -> Select:
    """Compras posventa del usuario con su carta, si la hay, en un solo ``LEFT JOIN``.

    Una consulta para todo el panel: ni una por compra ni una por foto o entrega. Se
    traen las compras pagadas (con carta o sin ella) y, además, cualquier carta ya
    publicada aunque su compra se haya anulado después. Se separa del ``execute`` para
    poder comprobar el SQL sin base de datos.
    """
    return (
        select(Purchase, Letter)
        .outerjoin(Letter, Letter.purchase_id == Purchase.id)
        .where(
            Purchase.user_id == user_id,
            or_(Purchase.status == "paid", Letter.status == "published"),
        )
        .order_by(Purchase.created_at.desc())
    )


async def dedications_of_user(
    db: AsyncSession, user_id: uuid.UUID
) -> list[tuple[Purchase, Letter | None]]:
    result = await db.execute(dedications_statement(user_id))
    return [(purchase, letter) for purchase, letter in result.all()]
