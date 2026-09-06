"""Entrega por correo: estado propio, reintentos y reenvíos sin duplicar cartas.

Cada intento crea una fila en ``letter_deliveries`` con su propio estado. Ningún
camino de este módulo crea compras ni cartas, de modo que reenviar una carta no
consume otra compra.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.models.commerce import Letter, LetterDelivery
from app.services.letters import public_url
from app.services.mailer import Mailer, render_letter_email
from app.services.qr import qr_data_uri


async def deliver(
    db: AsyncSession,
    settings: Settings,
    mailer: Mailer,
    letter: Letter,
    recipient: str | None = None,
) -> LetterDelivery:
    if letter.status != "published":
        raise ApiError(409, "LETTER_NOT_PUBLISHED", "Publica la carta antes de enviarla.")
    address = (recipient or letter.recipient_email or "").casefold()
    if not address:
        raise ApiError(422, "RECIPIENT_EMAIL_REQUIRED", "Falta el correo de destino.")
    url = public_url(settings, letter)
    delivery = LetterDelivery(
        letter_id=letter.id,
        recipient_email=address,
        status="pending",
        attempts=0,
        letter_version=letter.published_version,
    )
    db.add(delivery)
    await db.commit()
    await db.refresh(delivery)

    message = render_letter_email(
        to=address,
        recipient_name=letter.recipient_name,
        title=letter.title,
        public_url=url,
        qr_source=qr_data_uri(url),
        letter_id=str(letter.id),
        version=letter.published_version,
    )
    delivery.attempts += 1
    try:
        await mailer.send(message)
    except Exception as error:  # noqa: BLE001 - el fallo de correo no rompe la carta
        delivery.status = "failed"
        delivery.last_error = f"{type(error).__name__}"[:200]
    else:
        delivery.status = "sent"
        delivery.sent_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(delivery)
    return delivery
