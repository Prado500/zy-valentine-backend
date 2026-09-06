"""Cartas: una compra pagada habilita exactamente una carta.

Concurrencia y idempotencia:

- ``lock_purchase`` toma ``SELECT ... FOR UPDATE`` sobre la compra: doble clic,
  varias pestañas o reintentos se serializan en la base de datos.
- ``letters.purchase_id`` es UNIQUE: aunque dos transacciones pasen la validación,
  la segunda falla en la base y devuelve la carta existente con 200.
- Consultar, republicar o reenviar **no** consume otra compra: no se crea ninguna
  compra desde este módulo.
"""

import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.models.commerce import Letter, LetterPhoto
from app.models.user import User
from app.repositories import commerce
from app.schemas.commerce import LetterCreate, LetterUpdate
from app.services.storage import ALLOWED_CONTENT_TYPES, StorageBackend, sniff_content_type


def is_frozen(letter: Letter, settings: Settings) -> bool:
    """Decisión documentada: el contenido se congela al publicarse."""
    return letter.status == "published" and settings.freeze_letter_after_publish


def public_url(settings: Settings, letter: Letter) -> str | None:
    if letter.status != "published":
        return None
    return f"{settings.public_base_url}/carta/{letter.public_slug}"


async def create(
    db: AsyncSession, settings: Settings, user: User, payload: LetterCreate
) -> tuple[Letter, bool]:
    """Devuelve (carta, creada). Un segundo intento sobre la misma compra devuelve la misma carta."""
    purchase = await commerce.lock_purchase(db, payload.purchaseId)
    if purchase is None or purchase.user_id != user.id:
        raise ApiError(404, "PURCHASE_NOT_FOUND", "La compra no existe para esta cuenta.")
    if purchase.status != "paid":
        raise ApiError(409, "PURCHASE_NOT_PAID", "La compra aún no está confirmada por el pago.")
    existing = await commerce.letter_of_purchase(db, purchase.id)
    if existing:
        # El bloqueo se libera al cerrar la sesión de la petición; no se toca la compra.
        return existing, False
    # ON CONFLICT DO NOTHING sobre purchase_id: si otra transacción ganó la carrera,
    # esta petición devuelve la carta existente en vez de fallar o crear una segunda.
    statement = (
        pg_insert(Letter)
        .values(
            id=uuid.uuid4(),
            purchase_id=purchase.id,
            user_id=user.id,
            public_slug=secrets.token_urlsafe(16),
            status="draft",
            title=payload.title,
            recipient_name=payload.recipientName,
            recipient_email=(
                str(payload.recipientEmail).casefold() if payload.recipientEmail else None
            ),
            body=payload.body,
            theme=payload.theme,
        )
        .on_conflict_do_nothing(index_elements=[Letter.purchase_id])
        .returning(Letter.id)
    )
    created_id = await db.scalar(statement)
    await db.commit()
    if created_id is None:
        existing = await commerce.letter_of_purchase(db, purchase.id)
        if not existing:
            raise ApiError(409, "LETTER_CONFLICT", "No se pudo crear la carta.")
        return existing, False
    return await db.get(Letter, created_id), True


async def update(
    db: AsyncSession, settings: Settings, letter: Letter, payload: LetterUpdate
) -> Letter:
    if is_frozen(letter, settings):
        raise ApiError(409, "LETTER_FROZEN", "La carta publicada no se puede editar.")
    if payload.title is not None:
        letter.title = payload.title
    if payload.recipientName is not None:
        letter.recipient_name = payload.recipientName
    if payload.recipientEmail is not None:
        letter.recipient_email = str(payload.recipientEmail).casefold()
    if payload.body is not None:
        letter.body = payload.body
    if payload.theme is not None:
        letter.theme = payload.theme
    await db.commit()
    await db.refresh(letter)
    return letter


async def publish(db: AsyncSession, settings: Settings, letter: Letter) -> Letter:
    if is_frozen(letter, settings):
        raise ApiError(409, "LETTER_FROZEN", "La carta ya fue publicada y su contenido está fijo.")
    if not letter.recipient_email:
        raise ApiError(422, "RECIPIENT_EMAIL_REQUIRED", "Falta el correo de destino.")
    letter.status = "published"
    letter.published_version += 1
    letter.published_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(letter)
    return letter


async def add_photo(
    db: AsyncSession,
    settings: Settings,
    storage: StorageBackend,
    letter: Letter,
    data: bytes,
    declared_type: str | None,
    caption: str | None,
    position: int | None,
) -> LetterPhoto:
    if is_frozen(letter, settings):
        raise ApiError(409, "LETTER_FROZEN", "La carta publicada no admite nuevas fotos.")
    if not data:
        raise ApiError(422, "EMPTY_PHOTO", "El archivo está vacío.")
    if len(data) > settings.max_photo_bytes:
        raise ApiError(413, "PHOTO_TOO_LARGE", "La foto supera el tamaño permitido.")
    content_type = sniff_content_type(data, declared_type)
    photos = await commerce.photos_of_letter(db, letter.id)
    if len(photos) >= settings.max_photos_per_letter:
        raise ApiError(409, "PHOTO_LIMIT_REACHED", "Se alcanzó el máximo de fotos.")
    if position is None:
        position = (max((photo.position for photo in photos), default=-1)) + 1
    elif any(photo.position == position for photo in photos):
        raise ApiError(409, "PHOTO_POSITION_TAKEN", "Esa posición ya tiene una foto.")
    key = f"letters/{letter.id}/{uuid.uuid4().hex}{ALLOWED_CONTENT_TYPES[content_type]}"
    await storage.put(key, data, content_type)
    photo = LetterPhoto(
        letter_id=letter.id,
        position=position,
        storage_key=key,
        content_type=content_type,
        byte_size=len(data),
        caption=caption,
    )
    db.add(photo)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        await storage.delete(key)
        raise ApiError(409, "PHOTO_POSITION_TAKEN", "Esa posición ya tiene una foto.") from None
    await db.refresh(photo)
    return photo


async def remove_photo(
    db: AsyncSession,
    settings: Settings,
    storage: StorageBackend,
    letter: Letter,
    photo: LetterPhoto,
) -> None:
    if is_frozen(letter, settings):
        raise ApiError(409, "LETTER_FROZEN", "La carta publicada no admite cambios.")
    await db.delete(photo)
    await db.commit()
    await storage.delete(photo.storage_key)
