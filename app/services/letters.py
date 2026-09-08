"""Cartas: una compra pagada habilita exactamente una carta.

Concurrencia y idempotencia:

- ``lock_purchase`` toma ``SELECT ... FOR UPDATE`` sobre la compra: doble clic,
  varias pestañas o reintentos se serializan en la base de datos.
- ``letters.purchase_id`` es UNIQUE: aunque dos transacciones pasen la validación,
  la segunda falla en la base y devuelve la carta existente con 200.
- Consultar, republicar o reenviar **no** consume otra compra: no se crea ninguna
  compra desde este módulo.
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.html import sanitize
from app.models.commerce import Letter, LetterPhoto
from app.models.user import User
from app.repositories import commerce
from app.schemas.commerce import LetterCreate, LetterInput, LetterUpdate, TempPhotoRef
from app.services.service_bus import LetterPublisher, build_letter_message
from app.services.storage import (
    ALLOWED_CONTENT_TYPES,
    StorageBackend,
    content_type_of,
    sniff_content_type,
    temp_key,
    temp_key_owner,
)

LOG = logging.getLogger("app.letters")


def is_frozen(letter: Letter, settings: Settings) -> bool:
    """Decisión documentada: el contenido se congela al publicarse."""
    return letter.status == "published" and settings.freeze_letter_after_publish


def public_url(settings: Settings, letter: Letter) -> str | None:
    if letter.status != "published":
        return None
    return f"{settings.public_base_url}/carta/{letter.public_slug}"


def content_values(payload: LetterInput) -> dict:
    """Columnas de contenido tal como llegan del formulario.

    Una sola traducción para crear la carta y para sobrescribir un borrador retomado,
    de modo que las dos escrituras no puedan divergir.
    """
    return {
        "title": payload.title,
        "recipient_name": payload.recipientName,
        "recipient_email": (
            str(payload.recipientEmail).casefold() if payload.recipientEmail else None
        ),
        "body": payload.body,
        "theme": payload.theme,
    }


async def create(
    db: AsyncSession, settings: Settings, user: User, payload: LetterCreate
) -> tuple[Letter, bool]:
    """Devuelve (carta, creada). Nunca hay una segunda carta para la misma compra.

    Si la compra ya tiene carta en borrador, se sobrescribe con este payload (retomar
    desde "Mis dedicatorias"); si ya está publicada, se devuelve tal cual.
    """
    purchase = await commerce.lock_purchase(db, payload.purchaseId)
    if purchase is None or purchase.user_id != user.id:
        raise ApiError(404, "PURCHASE_NOT_FOUND", "La compra no existe para esta cuenta.")
    if purchase.status != "paid":
        raise ApiError(409, "PURCHASE_NOT_PAID", "La compra aún no está confirmada por el pago.")
    existing = await commerce.letter_of_purchase(db, purchase.id)
    if existing:
        if existing.status == "published":
            # Publicada: su contenido está fijo. El reintento devuelve la misma carta y
            # el despacho no vuelve a enviar el correo de esa versión.
            return existing, False
        # Borrador retomado. No hay autoguardado, así que lo que llega ahora es la carta
        # entera y sustituye lo que quedó a medias, bajo el mismo bloqueo de la compra.
        # Sigue siendo una carta por compra: cambia el contenido, no la fila ni el slug.
        for column, value in content_values(payload).items():
            setattr(existing, column, value)
        await db.commit()
        await db.refresh(existing)
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
            **content_values(payload),
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


async def enqueue(
    db: AsyncSession,
    settings: Settings,
    queue: LetterPublisher,
    user: User,
    payload: LetterCreate,
) -> bool:
    """IOP #5: valida en caliente y publica en la cola. Devuelve si quedó encolada.

    Antes de encolar nada se consulta la base de forma síncrona y se aborta si la
    compra no existe, no es de esta cuenta, no está pagada o **ya tiene una carta
    publicada**: los tres casos responden 4xx sin escribir nada. Son dos SELECT por
    índice, el precio mínimo para que la cola no acepte órdenes fraudulentas.

    ``False`` significa que el transporte no aceptó el mensaje; el llamador cae al
    camino síncrono en vez de perder la carta del comprador. No se toma ``FOR UPDATE``:
    la unicidad de ``letters.purchase_id`` sigue siendo la garantía final y el bloqueo
    lo toma el worker al insertar.
    """
    purchase = await commerce.owned_purchase(db, payload.purchaseId, user.id)
    if purchase is None:
        raise ApiError(404, "PURCHASE_NOT_FOUND", "La compra no existe para esta cuenta.")
    if purchase.status != "paid":
        raise ApiError(409, "PURCHASE_NOT_PAID", "La compra aún no está confirmada por el pago.")
    existing = await commerce.letter_of_purchase(db, purchase.id)
    if existing is not None and existing.status == "published":
        # Antitrampa: volver atrás en el navegador y reenviar el formulario no habilita
        # una segunda carta. Se corta aquí, antes de encolar: si la orden entrase en la
        # cola, el fraude se detectaría tarde y habría gastado IOPS del worker. Un
        # borrador sí pasa: es el retome desde "Mis dedicatorias", y el worker lo
        # sobrescribe en ``create`` en vez de crear otra carta.
        raise ApiError(409, "LETTER_ALREADY_EXISTS", "Esta compra ya tiene su carta.")
    if len(payload.temp_photos) > settings.max_photos_per_letter:
        raise ApiError(409, "PHOTO_LIMIT_REACHED", "Se alcanzó el máximo de fotos.")
    for photo in payload.temp_photos:
        # Una clave efímera solo la puede reclamar quien la subió.
        if temp_key_owner(photo.tempId) != str(user.id):
            raise ApiError(403, "TEMP_PHOTO_FORBIDDEN", "Esa foto temporal no es de esta cuenta.")
    message = build_letter_message(user.id, payload, auto_publish=payload.autoPublish)
    # message_id derivado de la compra: con detección de duplicados activa en la cola,
    # dos pestañas encolan una sola carta. Al retomar un borrador el id lleva un sufijo
    # único: si repitiera el de la creación, Service Bus descartaría el retome como
    # duplicado dentro de su ventana y el comprador esperaría un correo que no saldría.
    message_id = f"letter-{payload.purchaseId}"
    if existing is not None:
        message_id = f"{message_id}-{secrets.token_hex(4)}"
    return await queue.publish(message, message_id=message_id)


async def eager_upload(
    settings: Settings,
    storage: StorageBackend,
    user: User,
    data: bytes,
    declared_type: str | None,
    file_name: str | None,
) -> dict:
    """Sube la foto al espacio efímero antes de que la carta exista (eager upload).

    Se valida lo mismo que en el camino clásico —tamaño y firma binaria real, no el
    header del cliente— porque esto ocurre sin carta y sin compra verificada: el
    contenedor temporal no puede convertirse en un almacén de archivos arbitrarios.
    """
    if not data:
        raise ApiError(422, "EMPTY_PHOTO", "El archivo está vacío.")
    if len(data) > settings.max_photo_bytes:
        raise ApiError(413, "PHOTO_TOO_LARGE", "La foto supera el tamaño permitido.")
    content_type = sniff_content_type(data, declared_type)
    key = temp_key(user.id, content_type)
    await storage.put_temp(key, data, content_type)
    return {
        "tempId": key,
        # El nombre lo elige quien sube el archivo y acaba en `caption`, en el visor
        # y en el correo: se limpia aquí de controles y separadores, igual que hace
        # `TempPhotoRef` al recibirlo de vuelta.
        "fileName": sanitize(file_name or "").strip()[:200] or "foto",
        "contentType": content_type,
        "byteSize": len(data),
    }


def permanent_key(letter: Letter, ref: TempPhotoRef) -> str:
    """Destino definitivo de una foto temporal. **Determinista a propósito.**

    Deriva del identificador que ya lleva la clave efímera, no de un UUID nuevo. Así
    dos entregas del mismo mensaje calculan la misma ruta, y el traslado se puede
    reintentar sin duplicar la foto ni perderla: quien reintenta reconoce lo que ya
    hizo el intento anterior.
    """
    stem = Path(ref.tempId).stem
    return f"letters/{letter.id}/{stem}{ALLOWED_CONTENT_TYPES[content_type_of(ref.tempId)]}"


def ordered_refs(refs: list[TempPhotoRef]) -> list[TempPhotoRef]:
    """Ordena por la posición pedida y, a falta de ella, por el orden de llegada.

    La posición final la asigna el servidor con este orden. Si se copiara el número
    que manda el cliente, dos fotos podrían reclamar la misma posición y romper
    ``uq_letter_photos_position`` con un 409 que el comprador no puede arreglar.
    """
    return [
        ref
        for _, ref in sorted(
            enumerate(refs),
            key=lambda pair: pair[1].position if pair[1].position is not None else pair[0],
        )
    ]


async def discard_stale_photos(
    db: AsyncSession,
    storage: StorageBackend,
    letter: Letter,
    existing: list[LetterPhoto],
    refs: list[TempPhotoRef],
) -> list[LetterPhoto]:
    """Al retomar un borrador, las fotos pasan a ser las del payload de ahora.

    Se conservan las que este payload vuelve a traer: su destino determinista
    (:func:`permanent_key`) coincide con una fila existente, que es justo lo que ocurre
    cuando un mensaje de la cola se reentrega después de haber movido los blobs.
    Borrarlas las perdería para siempre, porque el temporal ya está vacío.

    Las filas se sueltan primero y en una sola transacción; el blob se borra después y
    su fallo solo se anota: un archivo huérfano en el contenedor es barato, una carta
    pagada que no sale no lo es.
    """
    keep = {permanent_key(letter, ref) for ref in refs}
    stale = [photo for photo in existing if photo.storage_key not in keep]
    if not stale:
        return existing
    for photo in stale:
        await db.delete(photo)
    await db.commit()
    for photo in stale:
        try:
            await storage.delete(photo.storage_key)
        except Exception:  # noqa: BLE001 - un blob huérfano no vale una carta pagada
            LOG.warning("No se pudo borrar el blob %s de la carta %s", photo.storage_key, letter.id)
    return [photo for photo in existing if photo.storage_key in keep]


async def attach_temp_photos(
    db: AsyncSession,
    settings: Settings,
    storage: StorageBackend,
    letter: Letter,
    refs: list[TempPhotoRef],
    *,
    replace: bool = False,
) -> list[LetterPhoto]:
    """IOP #6: traslada del contenedor efímero al permanente y registra las filas.

    Reentrante frente a una reentrega del mensaje. Cada foto tiene un destino
    determinista (:func:`permanent_key`), así que un segundo intento:

    - salta las que ya tienen fila,
    - reconoce las que ya se movieron pero no llegaron a guardarse (el propio
      ``move_blob`` devuelve el tamaño del destino cuando el origen ya no está),
    - y **omite** las que se perdieron, en vez de tumbar el mensaje entero: una carta
      pagada debe salir aunque falte una foto, y volver a intentarlo no la traería.

    Con ``replace`` (borrador retomado) las fotos pasan a ser las de este payload: las
    que ya tenía la carta y no vuelven a llegar se descartan antes de trasladar nada.

    El nombre original del archivo se guarda en ``caption`` para que el comprador
    reconozca su foto en el visor y en el correo.
    """
    if not refs and not replace:
        return []
    pending = ordered_refs(refs)[: settings.max_photos_per_letter]
    for ref in pending:
        # Una clave efímera solo la puede reclamar quien la subió. Se comprueba todo
        # antes de tocar nada: ni se borra ni se mueve una foto por un payload ajeno.
        if temp_key_owner(ref.tempId) != str(letter.user_id):
            raise ApiError(403, "TEMP_PHOTO_FORBIDDEN", "Esa foto temporal no es de esta cuenta.")
    existing = await commerce.photos_of_letter(db, letter.id)
    if replace:
        existing = await discard_stale_photos(db, storage, letter, existing, pending)
    if not pending:
        return existing
    known = {photo.storage_key for photo in existing}
    taken = {photo.position for photo in existing}

    photos: list[LetterPhoto] = []
    position = 0
    for ref in pending:
        key = permanent_key(letter, ref)
        if key in known:
            continue  # Ya trasladada y registrada en una entrega anterior.
        while position in taken:
            position += 1
        try:
            size = await storage.move_blob(ref.tempId, key)
        except ApiError as error:
            if error.status_code != 404:
                raise
            # La foto ya no está ni en el temporal ni en el permanente. Se registra y
            # se sigue: la carta y su correo valen más que la foto perdida.
            LOG.warning("Foto temporal ausente para la carta %s; se omite", letter.id)
            continue
        photos.append(
            LetterPhoto(
                letter_id=letter.id,
                position=position,
                storage_key=key,
                content_type=content_type_of(ref.tempId),
                byte_size=size,
                caption=ref.fileName[:200],
            )
        )
        taken.add(position)
    if not photos:
        return existing
    db.add_all(photos)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ApiError(409, "PHOTO_POSITION_TAKEN", "Dos fotos piden la misma posición.") from None
    for photo in photos:
        await db.refresh(photo)
    return existing + photos


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
