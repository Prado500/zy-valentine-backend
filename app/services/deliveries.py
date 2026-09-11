"""Entrega por correo: estado propio, reintentos y reenvíos sin duplicar cartas.

Cada intento crea una fila en ``letter_deliveries`` con su propio estado. Ningún
camino de este módulo crea compras ni cartas, de modo que reenviar una carta no
consume otra compra.

El correo va al comprador (IOP #7): agradecimiento, enlace, el código estilizado del
tema, consejos para acompañar la carta y un único adjunto, la **tarjeta QR en PDF** con
la postal completa y la dedicatoria «Para Y / De X». Dibujar la postal cuesta CPU, así
que se hace fuera del bucle de eventos con ``anyio.to_thread.run_sync`` y un
``CapacityLimiter`` de uno, porque la B1ms tiene un núcleo. La tarjeta no es
imprescindible: si falla, se anota y el correo sale con el enlace y el código.

El código del cuerpo tiene dos vías. Con ``API_PUBLIC_URL`` va como imagen remota
(``/api/v1/public/letters/{slug}/qr.png``), que todos los clientes muestran; sin ella,
incrustado por Content-ID (``cid:``), que algunos clientes no pintan.

El remitente y la canción no son columnas: viajan al final de ``letters.body`` y se
separan aquí una sola vez (:func:`app.services.letter_body.parse_body`). Solo la
postal usa el remitente y la primera frase; el correo no lleva la dedicatoria ni la
canción.
"""

import logging
from datetime import UTC, datetime
from email.utils import make_msgid
from functools import partial

from anyio import CapacityLimiter, to_thread
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.models.commerce import Letter, LetterDelivery
from app.services.cards import CardContent, card_name, render_card_pdf
from app.services.first_phrase import first_phrase
from app.services.letter_body import ParsedBody, parse_body
from app.services.letters import public_url
from app.services.mailer import Attachment, Mailer, render_letter_email
from app.services.postcard import render_qr_tile

LOG = logging.getLogger("app.deliveries")

# Dominio del Content-ID. Es un identificador opaco que ningún cliente resuelve, así que se
# fija en vez de dejar que `make_msgid` llame a `socket.getfqdn()`: esa llamada hace una
# consulta DNS inversa bloqueante dentro del bucle de eventos y, de paso, publicaría el
# nombre del contenedor en cada correo. `.invalid` está reservado por el RFC 2606.
CID_DOMAIN = "zy-valentine.invalid"

# Ancho del código en el correo: el doble de su tamaño de diseño (232 px), para que no se
# vea borroso en pantallas densas. El HTML lo muestra a la mitad.
EMAIL_QR_WIDTH = 464

_card_limiter: CapacityLimiter | None = None


def card_limiter() -> CapacityLimiter:
    """Un render de tarjeta a la vez por proceso (mismo patrón que ``hash_limiter``).

    Se crea en el primer uso y no al importar: ``CapacityLimiter`` necesita un bucle de
    eventos activo, y este módulo lo importan la API y el worker antes de tener uno.
    """
    global _card_limiter
    if _card_limiter is None:
        _card_limiter = CapacityLimiter(1)
    return _card_limiter


def qr_part(theme: str | None, url: str) -> tuple[str, Attachment]:
    """Código del cuerpo del correo: (``src`` del ``<img>``, parte incrustada).

    Un único sitio construye las dos mitades para que no puedan divergir: la cabecera
    ``Content-ID`` lleva los ``<>`` y el ``src`` los lleva pelados, y un identificador que
    no case deja la imagen rota otra vez. ``build_mime`` la marca ``inline`` y con nombre
    de archivo, que Outlook y el correo de iOS exigen para pintarla.
    """
    cid = make_msgid(idstring="qr", domain=CID_DOMAIN)
    part = Attachment(
        filename="qr.png",
        content=render_qr_tile(theme, url, EMAIL_QR_WIDTH),
        maintype="image",
        subtype="png",
        cid=cid,
    )
    return f"cid:{cid[1:-1]}", part


async def build_qr(
    settings: Settings, letter: Letter, url: str
) -> tuple[str, tuple[Attachment, ...]]:
    """(``src`` del ``<img>``, partes incrustadas) según haya o no origen público de la API.

    Con ``API_PUBLIC_URL`` el código es una imagen remota servida por
    ``GET /api/v1/public/letters/{slug}/qr.png`` (pública y cacheable): es la vía que
    todos los clientes de correo muestran. Sin ella se incrusta por Content-ID.
    """
    if settings.api_public_url:
        return f"{settings.api_public_url}/api/v1/public/letters/{letter.public_slug}/qr.png", ()
    source, part = await to_thread.run_sync(
        partial(qr_part, letter.theme, url), limiter=card_limiter()
    )
    return source, (part,)


async def build_card(
    settings: Settings, letter: Letter, parsed: ParsedBody, url: str
) -> Attachment | None:
    """Tarjeta QR en PDF con el diseño del tema. Nunca rompe la entrega.

    Se puede apagar con ``LETTER_CARD_ENABLED=false`` sin desplegar. El render corre en
    un hilo y de uno en uno; si falla o pesa más de la cuenta, se registra y el correo
    sale sin ella: la tarjeta es un extra, no la carta.
    """
    if not settings.letter_card_enabled:
        return None
    content = CardContent(
        title=letter.title,
        recipient_name=letter.recipient_name,
        sender_name=parsed.sender,
        note=first_phrase(parsed.message),
        public_url=url,
        theme=letter.theme,
        letter_id=str(letter.id),
        version=letter.published_version,
    )
    try:
        pdf = await to_thread.run_sync(partial(render_card_pdf, content), limiter=card_limiter())
    except Exception as error:  # noqa: BLE001 - la tarjeta es opcional
        LOG.warning(
            "No se pudo generar la tarjeta QR de la carta %s (%s)", letter.id, type(error).__name__
        )
        return None
    if len(pdf) > settings.max_letter_card_bytes:
        LOG.warning("Tarjeta QR de la carta %s descartada por tamaño (%s bytes)", letter.id, len(pdf))
        return None
    return Attachment(
        filename=card_name(letter), content=pdf, maintype="application", subtype="pdf"
    )


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
    parsed = parse_body(letter.body)
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

    card = await build_card(settings, letter, parsed, url)
    qr_source, inline = await build_qr(settings, letter, url)
    message = render_letter_email(
        to=address,
        title=letter.title,
        public_url=url,
        qr_source=qr_source,
        letter_id=str(letter.id),
        version=letter.published_version,
        attachments=(card,) if card else (),
        inline=inline,
        theme=letter.theme,
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
