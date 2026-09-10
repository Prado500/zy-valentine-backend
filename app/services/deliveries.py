"""Entrega por correo: estado propio, reintentos y reenvíos sin duplicar cartas.

Cada intento crea una fila en ``letter_deliveries`` con su propio estado. Ningún
camino de este módulo crea compras ni cartas, de modo que reenviar una carta no
consume otra compra.

El correo lleva tres adjuntos (IOP #7): el documento HTML autónomo con las fotos en
Base64, la **tarjeta QR en PDF** con el diseño del tema y el código suelto como
``qr.png``. Los dos primeros cuestan CPU —descargar y codificar fotos, subconjuntar
fuentes— y se construyen fuera del bucle de eventos con ``anyio.to_thread.run_sync``;
la tarjeta además pasa por un ``CapacityLimiter`` de uno, porque la B1ms tiene un
núcleo. Ninguno de los tres es imprescindible: si falla, se anota y el correo sale con
el enlace y el QR del cuerpo.

El remitente y la canción no son columnas: viajan al final de ``letters.body`` y se
separan aquí una sola vez (:func:`app.services.letter_body.parse_body`) para el
correo, el documento y la tarjeta.
"""

import logging
from datetime import UTC, datetime
from email.utils import make_msgid
from functools import partial

from anyio import CapacityLimiter, to_thread
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.html import safe_file_name
from app.models.commerce import Letter, LetterDelivery, LetterPhoto
from app.repositories import commerce
from app.services.cards import CardContent, card_name, render_card_pdf
from app.services.letter_body import ParsedBody, parse_body
from app.services.letters import public_url
from app.services.mailer import Attachment, Mailer, render_letter_document, render_letter_email
from app.services.qr import qr_data_uri, qr_png
from app.services.storage import StorageBackend

LOG = logging.getLogger("app.deliveries")

# Dominio del Content-ID. Es un identificador opaco que ningún cliente resuelve, así que se
# fija en vez de dejar que `make_msgid` llame a `socket.getfqdn()`: esa llamada hace una
# consulta DNS inversa bloqueante dentro del bucle de eventos y, de paso, publicaría el
# nombre del contenedor en cada correo. `.invalid` está reservado por el RFC 2606.
CID_DOMAIN = "zy-valentine.invalid"

# Nombre del QR descargable. El cuerpo lleva el mismo PNG como parte relacionada; este
# es el que aparece en la bandeja para guardarlo o imprimirlo tal cual.
QR_DOWNLOAD_NAME = "qr.png"

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


def qr_part(url: str, png: bytes | None = None) -> tuple[str, Attachment]:
    """QR del cuerpo del correo: devuelve (``src`` del ``<img>``, parte incrustada).

    Un único sitio construye las dos mitades para que no puedan divergir: la cabecera
    ``Content-ID`` lleva los ``<>`` y el ``src`` los lleva pelados, y un identificador que
    no case deja la imagen rota otra vez. El ``data:`` URI se queda solo en el documento
    adjunto, que se abre en un navegador. ``png`` permite compartir los bytes con el
    adjunto descargable en vez de generar el código dos veces.
    """
    cid = make_msgid(idstring="qr", domain=CID_DOMAIN)
    part = Attachment(
        filename="qr.png",
        content=png if png is not None else qr_png(url),
        maintype="image",
        subtype="png",
        cid=cid,
    )
    return f"cid:{cid[1:-1]}", part


def qr_download(png: bytes) -> Attachment:
    """El mismo QR como archivo descargable: sin ``Content-ID``, aparece en la bandeja."""
    return Attachment(filename=QR_DOWNLOAD_NAME, content=png, maintype="image", subtype="png")


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


def document_name(letter: Letter) -> str:
    """Nombre del adjunto: reconocible en la bandeja de entrada y sin rutas.

    El título lo escribe el comprador y acaba en una cabecera MIME, así que pasa por
    la lista blanca de :func:`app.core.html.safe_file_name`: sin barras, sin comillas
    y sin saltos de línea que pudieran partir la cabecera.
    """
    return f"{safe_file_name(letter.title, fallback='carta')}.html"


async def build_document(
    settings: Settings,
    storage: StorageBackend | None,
    letter: Letter,
    photos: list[LetterPhoto],
    url: str,
    parsed: ParsedBody | None = None,
) -> Attachment | None:
    """Descarga las fotos y arma el documento autónomo. Nunca rompe la entrega.

    Si el almacenamiento no está disponible o una foto se perdió, se registra y el
    correo sale igual con enlace y QR: el adjunto es un extra, no la carta.
    """
    if storage is None:
        return None
    parsed = parsed or parse_body(letter.body)
    loaded: list[tuple[str, str, bytes]] = []
    for photo in photos:
        try:
            data = await storage.get(photo.storage_key)
        except Exception as error:  # noqa: BLE001 - una foto ausente no cancela el correo
            LOG.warning(
                "Foto %s no disponible para el adjunto (%s)", photo.id, type(error).__name__
            )
            continue
        loaded.append((photo.caption or "", photo.content_type, data))
    try:
        # El Base64 y el armado del HTML son CPU sobre bytes: van a un hilo aparte.
        html = await to_thread.run_sync(
            partial(
                render_letter_document,
                title=letter.title,
                recipient_name=letter.recipient_name,
                body=parsed.message,
                public_url=url,
                qr_source=qr_data_uri(url),
                photos=loaded,
                letter_id=str(letter.id),
                version=letter.published_version,
                max_bytes=settings.max_letter_document_bytes,
                sender_name=parsed.sender,
                song_url=parsed.song_url,
                theme=letter.theme,
            )
        )
    except Exception as error:  # noqa: BLE001 - idem: el adjunto es opcional
        LOG.warning("No se pudo generar el documento adjunto (%s)", type(error).__name__)
        return None
    content = html.encode("utf-8")
    if len(content) > settings.max_letter_document_bytes:  # pragma: no cover - red de seguridad
        LOG.warning("Documento de la carta %s descartado por tamaño", letter.id)
        return None
    return Attachment(filename=document_name(letter), content=content)


async def deliver(
    db: AsyncSession,
    settings: Settings,
    mailer: Mailer,
    letter: Letter,
    recipient: str | None = None,
    storage: StorageBackend | None = None,
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

    photos = await commerce.photos_of_letter(db, letter.id)
    document = await build_document(settings, storage, letter, photos, url, parsed)
    card = await build_card(settings, letter, parsed, url)
    png = qr_png(url)
    qr_src, qr_inline = qr_part(url, png)
    # Orden fijo: la carta, la tarjeta y el código. Lo que falló simplemente no va.
    attachments = tuple(item for item in (document, card, qr_download(png)) if item)
    message = render_letter_email(
        to=address,
        recipient_name=letter.recipient_name,
        title=letter.title,
        public_url=url,
        qr_source=qr_src,
        letter_id=str(letter.id),
        version=letter.published_version,
        attachments=attachments,
        inline=(qr_inline,),
        sender_name=parsed.sender,
        theme=letter.theme,
        song_url=parsed.song_url,
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
