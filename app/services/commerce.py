"""Servicio de aplicación del dominio comercial.

Es la única capa que orquesta: llama a los repositorios, decide si una carta se
encola o se escribe, traslada las fotos y arma los esquemas Pydantic de respuesta.
Por encima, los routers solo traducen HTTP; por debajo, los servicios de dominio
(``letters``, ``purchases``, ``deliveries``, ``identity``, ``webhooks``) mantienen
las reglas de negocio y los repositorios hablan SQL.

Por qué existe este archivo: antes cada endpoint recibía la ``AsyncSession`` y
llamaba a ``repo.*`` por su cuenta, así que la capa HTTP conocía el esquema de la
base de datos. Bastaba con cambiar una consulta para tener que tocar el router, y
la misma orquestación estaba escrita dos veces —una en el router y otra en el
worker— con el riesgo de que divergieran. De hecho divergieron: el camino síncrono
se olvidaba de trasladar las fotos del *eager upload*.

Este objeto vive **una petición** (o un mensaje de la cola). Su sesión es la de esa
unidad de trabajo, y los puertos (almacenamiento, correo, pagos, cola) se le
inyectan ya construidos: no los crea ni los cierra.

Sobre los errores: se usa ``ApiError`` en todas las capas, que es la convención ya
establecida en este código. Lleva un código estable y un mensaje para la persona,
y son esos dos datos —no la clase— los que consume tanto el manejador HTTP como el
worker, que traduce el estado a "reintentar" o "a la dead-letter queue".
"""

import uuid
from dataclasses import dataclass
from functools import partial
from typing import Any, NamedTuple

from anyio import to_thread
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.models.commerce import Letter, LetterDelivery, LetterPhoto, Purchase
from app.models.user import User
from app.repositories import commerce as repo
from app.schemas.commerce import (
    CommerceHealth,
    DedicationResponse,
    DeliveryResponse,
    EagerPhotoResponse,
    IdentityDocumentInput,
    IdentityDocumentResponse,
    LetterCreate,
    LetterQueued,
    LetterResponse,
    LetterUpdate,
    PaymentResponse,
    PaymentVerifyInput,
    PhotoResponse,
    PublicLetterResponse,
    PublicPhoto,
    PurchaseCreate,
    PurchaseResponse,
    PurchaseVerification,
    ResendInput,
    TempPhotoRef,
)
from app.services import dedications, deliveries, identity, letters, purchases, webhooks
from app.services.cards import CardContent, card_name, render_card_pdf
from app.services.first_phrase import first_phrase
from app.services.letter_body import parse_body
from app.services.mailer import Mailer
from app.services.payments import PaymentGateway
from app.services.postcard import PostcardContent, render_postcard, render_qr_tile
from app.services.service_bus import (
    LETTER_MESSAGE_TYPE,
    LETTER_MESSAGE_VERSION,
    LetterPublisher,
)
from app.services.storage import StorageBackend


class Outcome(NamedTuple):
    """Respuesta cuyo código HTTP depende de lo que ocurrió (201 nueva, 200 existente…)."""

    body: Any
    status: int


class BinaryContent(NamedTuple):
    """Bytes servidos tal cual: una foto, el PNG del QR o el PDF de la tarjeta.

    Con ``filename`` la respuesta lleva ``Content-Disposition: attachment``: el botón
    "Descargar" del frontend usa ``<a download>`` sobre otro origen, que el navegador
    ignora, así que la descarga la tiene que forzar el servidor. ``cache_control`` va
    en las rutas públicas de contenido congelado, que se regenera en cada petición.
    """

    content: bytes
    media_type: str
    filename: str | None = None
    cache_control: str | None = None


# Una carta publicada está congelada: su QR y su tarjeta son estables por versión.
FROZEN_CACHE = "public, max-age=86400"


@dataclass(frozen=True, slots=True)
class CommerceService:
    db: AsyncSession
    settings: Settings
    storage: StorageBackend
    mailer: Mailer
    payments: PaymentGateway
    queue: LetterPublisher

    # --- Identidad privada -----------------------------------------------------------

    async def set_identity_document(
        self, user: User, payload: IdentityDocumentInput
    ) -> IdentityDocumentResponse:
        record = await identity.set_document(self.db, self.settings, user, payload)
        return IdentityDocumentResponse(
            documentType=record.document_type,
            documentLast4=record.document_last4,
            createdAt=record.created_at,
        )

    async def identity_document(self, user: User) -> IdentityDocumentResponse:
        record = await repo.identity_by_user(self.db, user.id)
        if record is None:
            raise ApiError(404, "DOCUMENT_NOT_FOUND", "No hay documento registrado.")
        return IdentityDocumentResponse(
            documentType=record.document_type,
            documentLast4=record.document_last4,
            createdAt=record.created_at,
        )

    # --- Compras y pagos -------------------------------------------------------------

    async def create_purchase(self, user: User, payload: PurchaseCreate) -> Outcome:
        purchase, created = await purchases.create_intent(
            self.db, self.settings, self.payments, user, payload
        )
        return Outcome(await self._purchase_payload(purchase), 201 if created else 200)

    async def list_purchases(self, user: User) -> list[PurchaseResponse]:
        items = await repo.purchases_of_user(self.db, user.id)
        with_letter = await repo.letters_with_purchase(self.db, [item.id for item in items])
        return [self._purchase_schema(item, item.id in with_letter) for item in items]

    async def get_purchase(self, user: User, purchase_id: uuid.UUID) -> PurchaseResponse:
        return await self._purchase_payload(await self._owned_purchase(user, purchase_id))

    async def verify_purchase(
        self, user: User, purchase_id: uuid.UUID, payload: PaymentVerifyInput
    ) -> PurchaseVerification:
        """IOP #3: el navegador solo aporta el identificador; el estado lo confirma el servidor."""
        purchase = await self._owned_purchase(user, purchase_id)
        if payload.paymentId:
            await purchases.verify(self.db, self.payments, purchase, payload.paymentId)
        payment = await repo.payment_of_purchase(self.db, purchase.id)
        letter = await repo.letter_of_purchase(self.db, purchase.id)
        return PurchaseVerification(
            purchase=self._purchase_schema(purchase, letter is not None),
            payment=(
                PaymentResponse(
                    status=payment.status,
                    statusDetail=payment.status_detail,
                    providerPaymentId=payment.provider_payment_id,
                    verifiedAt=payment.verified_at,
                )
                if payment
                else None
            ),
            canCreateLetter=purchase.status == "paid" and letter is None,
        )

    async def handle_webhook(
        self, body: bytes, headers: dict[str, str], query_id: str | None
    ) -> str:
        """Verifica la firma **antes** de tocar la base de datos y aplica el evento."""
        if len(body) > 64_000:
            raise ApiError(413, "PAYLOAD_TOO_LARGE", "Notificación demasiado grande.")
        self.payments.verify_webhook(body, headers)
        payload = webhooks.decode(body)
        return await webhooks.handle(self.db, self.payments, payload, query_id)

    # --- Cartas ----------------------------------------------------------------------

    async def create_letter(self, user: User, payload: LetterCreate) -> Outcome:
        """IOP #4 a #7: valida la compra, encola si se puede y escribe (y envía) si no.

        Con la cola disponible se responde 202 sin escribir nada. Sin ella —o si el
        transporte falla— se conserva el camino síncrono: se escribe la carta, se
        trasladan las fotos del contenedor efímero y se **despacha** igual que lo
        haría el worker (publicar y enviar el correo). Antes este camino se detenía
        en el borrador y el comprador esperaba un correo que nadie mandaba.
        """
        if self.queue.enabled and await self._try_enqueue(user, payload):
            return Outcome(LetterQueued(purchaseId=payload.purchaseId), 202)
        letter, created = await letters.create(self.db, self.settings, user, payload)
        if created or letter.status == "draft":
            # Carta nueva, o borrador retomado desde "Mis dedicatorias": en los dos casos
            # las fotos son las del payload de ahora. Una carta publicada no se toca.
            await letters.attach_temp_photos(
                self.db,
                self.settings,
                self.storage,
                letter,
                payload.temp_photos,
                replace=not created,
            )
        # También sobre la carta ya existente (200): si un intento anterior murió entre
        # crear y enviar, o el comprador retomó el borrador, el despacho lo completa sin
        # duplicar el correo de una versión ya enviada.
        await self._dispatch(letter, payload.autoPublish)
        return Outcome(await self._letter_payload(letter), 201 if created else 200)

    async def _try_enqueue(self, user: User, payload: LetterCreate) -> bool:
        """Publica en la cola. Devuelve False si hay que caer al camino síncrono.

        Los errores de negocio (compra ajena, sin pagar, con carta) se propagan: son
        la respuesta correcta. Solo los fallos de transporte degradan, y degradan en
        silencio porque el comprador no tiene nada que hacer con ellos.
        """
        try:
            return await letters.enqueue(self.db, self.settings, self.queue, user, payload)
        except ApiError:
            raise
        except Exception:  # noqa: BLE001 - un fallo de transporte no pierde la carta
            return False

    async def list_letters(self, user: User) -> list[LetterResponse]:
        return [
            await self._letter_payload(letter)
            for letter in await repo.letters_of_user(self.db, user.id)
        ]

    async def get_letter(self, user: User, letter_id: uuid.UUID) -> LetterResponse:
        return await self._letter_payload(await self._owned_letter(user, letter_id))

    async def list_dedications(self, user: User) -> list[DedicationResponse]:
        """Panel posventa: una fila por compra pagada, con su carta si existe.

        Una sola consulta (``LEFT JOIN``) y sin cuerpo, fotos ni entregas: el panel
        enseña estado y enlace, y el detalle sigue en ``get_letter``. Así listar no
        cuesta 1 + 2N consultas, como ``list_letters``.
        """
        rows = await repo.dedications_of_user(self.db, user.id)
        items: list[DedicationResponse] = []
        for purchase, letter in rows:
            state = dedications.state_of(purchase, letter)
            if state is not None:
                items.append(self._dedication_schema(purchase, letter, state))
        return items

    async def update_letter(
        self, user: User, letter_id: uuid.UUID, payload: LetterUpdate
    ) -> LetterResponse:
        letter = await self._owned_letter(user, letter_id)
        await letters.update(self.db, self.settings, letter, payload)
        return await self._letter_payload(letter)

    async def publish_letter(self, user: User, letter_id: uuid.UUID) -> LetterResponse:
        """IOP #7: publica, genera enlace y QR, y envía el correo con estado persistido."""
        letter = await self._owned_letter(user, letter_id)
        await letters.publish(self.db, self.settings, letter)
        await deliveries.deliver(self.db, self.settings, self.mailer, letter)
        return await self._letter_payload(letter)

    async def resend_letter(
        self, user: User, letter_id: uuid.UUID, payload: ResendInput
    ) -> DeliveryResponse:
        """Reenviar no consume otra compra ni crea otra carta: solo otro intento de envío."""
        letter = await self._owned_letter(user, letter_id)
        recipient = str(payload.recipientEmail) if payload.recipientEmail else None
        delivery = await deliveries.deliver(self.db, self.settings, self.mailer, letter, recipient)
        return self._delivery_schema(delivery)

    async def letter_qr(self, user: User, letter_id: uuid.UUID) -> BinaryContent:
        letter = await self._owned_letter(user, letter_id)
        return await self._tile(letter, self._published_url(letter, "su QR"))

    async def letter_card(self, user: User, letter_id: uuid.UUID) -> BinaryContent:
        """Tarjeta QR en PDF de la carta propia (la misma que viaja en el correo)."""
        letter = await self._owned_letter(user, letter_id)
        return await self._card(letter, self._published_url(letter, "su tarjeta"))

    async def letter_postcard(self, user: User, letter_id: uuid.UUID) -> BinaryContent:
        """La postal en PNG: lo mismo que va dentro del PDF, sin la hoja alrededor."""
        letter = await self._owned_letter(user, letter_id)
        return await self._postcard(letter, self._published_url(letter, "su postal"))

    def _published_url(self, letter: Letter, what: str) -> str:
        url = letters.public_url(self.settings, letter)
        if not url:
            raise ApiError(409, "LETTER_NOT_PUBLISHED", f"Publica la carta para obtener {what}.")
        return url

    def _postcard_content(self, letter: Letter, url: str) -> PostcardContent:
        """Lo que la postal necesita. El remitente y la primera frase salen del cuerpo."""
        parsed = parse_body(letter.body)
        return PostcardContent(
            recipient_name=letter.recipient_name,
            sender_name=parsed.sender,
            note=first_phrase(parsed.message),
            public_url=url,
            theme=letter.theme,
        )

    async def _tile(self, letter: Letter, url: str) -> BinaryContent:
        """El código estilizado del tema. Es lo que enseña el correo y lo que pide el front."""
        png = await to_thread.run_sync(
            partial(render_qr_tile, letter.theme, url, deliveries.EMAIL_QR_WIDTH),
            limiter=deliveries.card_limiter(),
        )
        return BinaryContent(png, "image/png", f"qr-{letter.public_slug}.png")

    async def _postcard(self, letter: Letter, url: str) -> BinaryContent:
        if not self.settings.letter_card_enabled:
            raise ApiError(503, "LETTER_CARD_DISABLED", "La tarjeta QR está desactivada.")
        png = await to_thread.run_sync(
            partial(render_postcard, self._postcard_content(letter, url)),
            limiter=deliveries.card_limiter(),
        )
        return BinaryContent(png, "image/png", f"postal-{letter.public_slug}.png")

    async def _card(self, letter: Letter, url: str) -> BinaryContent:
        if not self.settings.letter_card_enabled:
            raise ApiError(503, "LETTER_CARD_DISABLED", "La tarjeta QR está desactivada.")
        postcard = self._postcard_content(letter, url)
        content = CardContent(
            title=letter.title,
            recipient_name=postcard.recipient_name,
            sender_name=postcard.sender_name,
            note=postcard.note,
            public_url=url,
            theme=letter.theme,
            letter_id=str(letter.id),
            version=letter.published_version,
        )
        # Mismo hilo y mismo límite que la entrega por correo: un render a la vez.
        pdf = await to_thread.run_sync(
            partial(render_card_pdf, content), limiter=deliveries.card_limiter()
        )
        return BinaryContent(pdf, "application/pdf", card_name(letter))

    # --- Fotos -----------------------------------------------------------------------

    async def eager_photo(
        self, user: User, data: bytes, declared_type: str | None, file_name: str | None
    ) -> EagerPhotoResponse:
        """Sube al contenedor efímero. No toca PostgreSQL: cero escrituras."""
        record = await letters.eager_upload(
            self.settings, self.storage, user, data, declared_type, file_name
        )
        return EagerPhotoResponse(**record)

    async def add_photo(
        self,
        user: User,
        letter_id: uuid.UUID,
        data: bytes,
        declared_type: str | None,
        caption: str | None,
        position: int | None,
    ) -> PhotoResponse:
        letter = await self._owned_letter(user, letter_id)
        photo = await letters.add_photo(
            self.db, self.settings, self.storage, letter, data, declared_type, caption, position
        )
        return self._photo_schema(letter, photo)

    async def photo_content(
        self, user: User, letter_id: uuid.UUID, photo_id: uuid.UUID
    ) -> BinaryContent:
        letter = await self._owned_letter(user, letter_id)
        photo = await repo.photo_of_letter(self.db, letter.id, photo_id)
        if photo is None:
            raise ApiError(404, "PHOTO_NOT_FOUND", "La foto no existe.")
        return BinaryContent(await self.storage.get(photo.storage_key), photo.content_type)

    async def delete_photo(self, user: User, letter_id: uuid.UUID, photo_id: uuid.UUID) -> None:
        letter = await self._owned_letter(user, letter_id)
        photo = await repo.photo_of_letter(self.db, letter.id, photo_id)
        if photo is None:
            raise ApiError(404, "PHOTO_NOT_FOUND", "La foto no existe.")
        await letters.remove_photo(self.db, self.settings, self.storage, letter, photo)

    # --- Visor público (sin sesión, sin datos personales del comprador) ---------------

    async def public_letter(self, slug: str) -> PublicLetterResponse:
        letter = await self._published_letter(slug)
        photos = await repo.photos_of_letter(self.db, letter.id)
        return PublicLetterResponse(
            letterId=letter.id,
            publishedVersion=letter.published_version,
            title=letter.title,
            recipientName=letter.recipient_name,
            body=letter.body,
            theme=letter.theme,
            photos=[
                PublicPhoto(
                    position=photo.position,
                    caption=photo.caption,
                    url=f"/api/v1/public/letters/{slug}/photos/{photo.position}",
                )
                for photo in photos
            ],
            publishedAt=letter.published_at,
        )

    async def public_photo(self, slug: str, position: int) -> BinaryContent:
        letter = await self._published_letter(slug)
        photo = await repo.photo_at_position(self.db, letter.id, position)
        if photo is None:
            raise ApiError(404, "PHOTO_NOT_FOUND", "La foto no existe.")
        return BinaryContent(await self.storage.get(photo.storage_key), photo.content_type)

    async def public_qr(self, slug: str) -> BinaryContent:
        """El código estilizado, sin sesión. Lo usan el correo y el modal del frontend."""
        letter = await self._published_letter(slug)
        tile = await self._tile(letter, letters.public_url(self.settings, letter))
        return tile._replace(cache_control=FROZEN_CACHE)

    async def public_postcard(self, slug: str) -> BinaryContent:
        letter = await self._published_letter(slug)
        postal = await self._postcard(letter, letters.public_url(self.settings, letter))
        return postal._replace(cache_control=FROZEN_CACHE)

    async def public_card(self, slug: str) -> BinaryContent:
        """Tarjeta QR en PDF, sin sesión. Cuesta CPU: se declara cacheable."""
        letter = await self._published_letter(slug)
        content = await self._card(letter, letters.public_url(self.settings, letter))
        return content._replace(cache_control=FROZEN_CACHE)

    def health(self) -> CommerceHealth:
        """Diagnóstico sin secretos: qué integraciones están **activas**.

        Activas, no configuradas. La diferencia importa en la cola: una cadena
        inválida deja las variables puestas y el publicador degradado, y decir
        entonces "service-bus" certificaría salud sobre un sistema que escribe de
        forma síncrona.
        """
        return CommerceHealth(
            paymentProvider=self.settings.payment_provider,
            storageBackend=self.settings.storage_backend,
            mailBackend=self.settings.mail_backend,
            # `self.queue.enabled` y no `settings.service_bus_enabled`: las variables
            # pueden estar puestas y aun así haber degradado a `MockPublisher` por una
            # cadena inválida o por falta del paquete. El diagnóstico refleja lo que la
            # aplicación **hace**, no lo que se le pidió que hiciera.
            letterQueue="service-bus" if self.queue.enabled else "sync",
            freezeAfterPublish=self.settings.freeze_letter_after_publish,
        )

    # --- Consumo de la cola ------------------------------------------------------------

    async def fulfil_queued_letter(self, message: dict) -> uuid.UUID:
        """Ejecuta un encargo de la cola: escribe la carta, mueve las fotos y envía.

        Es la misma orquestación del camino síncrono, en el mismo sitio, para que no
        vuelvan a divergir. El worker solo aporta el mensaje y decide, según el error,
        si reintenta o manda a la dead-letter queue.
        """
        if message.get("type") != LETTER_MESSAGE_TYPE:
            raise ApiError(422, "UNKNOWN_MESSAGE_TYPE", "Tipo de mensaje no soportado.")
        if message.get("version") != LETTER_MESSAGE_VERSION:
            raise ApiError(422, "UNKNOWN_MESSAGE_VERSION", "Versión de mensaje no soportada.")
        try:
            user_id = uuid.UUID(str(message["userId"]))
            payload = LetterCreate(purchaseId=message["purchaseId"], **message["letter"])
            refs = [TempPhotoRef(**item) for item in message.get("tempPhotos") or []]
        except Exception:  # noqa: BLE001 - cualquier forma inesperada es irrecuperable
            raise ApiError(
                422, "INVALID_MESSAGE", "El mensaje no tiene la forma esperada."
            ) from None

        user = await self.db.get(User, user_id)
        if user is None or not user.is_active:
            raise ApiError(404, "USER_UNAVAILABLE", "El comprador no existe o está inactivo.")

        letter, created = await letters.create(self.db, self.settings, user, payload)
        if created or letter.status == "draft":
            await letters.attach_temp_photos(
                self.db, self.settings, self.storage, letter, refs, replace=not created
            )
        await self._dispatch(letter, bool(message.get("autoPublish", True)))
        return letter.id

    async def _dispatch(self, letter: Letter, auto_publish: bool) -> None:
        """IOP #7 para cualquier camino: publica si hace falta y envía una vez por versión.

        Es el único sitio donde una carta recién escrita pasa a ``published`` y se
        manda el correo, para que el camino síncrono y el worker no vuelvan a divergir.
        Sin correo de destino no hay nada que despachar y la carta queda en borrador.
        Un fallo de SMTP no rompe la petición: ``deliver`` deja la entrega en ``failed``
        y el comprador puede reintentar con ``POST /letters/{id}/deliveries``.
        """
        if not (auto_publish and letter.recipient_email):
            return
        if letter.status != "published":
            await letters.publish(self.db, self.settings, letter)
        if await self._already_delivered(letter):
            return
        await deliveries.deliver(self.db, self.settings, self.mailer, letter)

    async def _already_delivered(self, letter: Letter) -> bool:
        """Una reentrega no vuelve a enviar el correo de la misma versión publicada."""
        sent = await repo.deliveries_of_letter(self.db, letter.id)
        return any(
            item.status == "sent" and item.letter_version == letter.published_version
            for item in sent
        )

    # --- Acceso y armado de respuestas -------------------------------------------------

    async def _owned_purchase(self, user: User, purchase_id: uuid.UUID) -> Purchase:
        purchase = await repo.owned_purchase(self.db, purchase_id, user.id)
        if purchase is None:
            raise ApiError(404, "PURCHASE_NOT_FOUND", "La compra no existe para esta cuenta.")
        return purchase

    async def _owned_letter(self, user: User, letter_id: uuid.UUID) -> Letter:
        letter = await repo.owned_letter(self.db, letter_id, user.id)
        if letter is None:
            raise ApiError(404, "LETTER_NOT_FOUND", "La carta no existe para esta cuenta.")
        return letter

    async def _published_letter(self, slug: str) -> Letter:
        letter = await repo.letter_by_slug(self.db, slug)
        if letter is None or letter.status != "published":
            raise ApiError(404, "LETTER_NOT_FOUND", "La carta no está disponible.")
        return letter

    async def _purchase_payload(self, purchase: Purchase) -> PurchaseResponse:
        letter = await repo.letter_of_purchase(self.db, purchase.id)
        return self._purchase_schema(purchase, letter is not None)

    @staticmethod
    def _purchase_schema(purchase: Purchase, has_letter: bool) -> PurchaseResponse:
        return PurchaseResponse(
            id=purchase.id,
            status=purchase.status,
            amountCents=purchase.amount_cents,
            currency=purchase.currency,
            externalReference=purchase.external_reference,
            checkoutUrl=purchase.checkout_url,
            hasLetter=has_letter,
            paidAt=purchase.paid_at,
            expiresAt=purchase.expires_at,
            createdAt=purchase.created_at,
        )

    def _dedication_schema(
        self, purchase: Purchase, letter: Letter | None, state: str
    ) -> DedicationResponse:
        published = letter is not None and letter.status == "published"
        return DedicationResponse(
            purchaseId=purchase.id,
            letterId=letter.id if letter else None,
            state=state,
            title=letter.title if letter else None,
            recipientName=letter.recipient_name if letter else None,
            theme=letter.theme if letter else None,
            publicSlug=letter.public_slug if published else None,
            publicUrl=letters.public_url(self.settings, letter) if letter else None,
            paidAt=purchase.paid_at,
            publishedAt=letter.published_at if letter else None,
            updatedAt=letter.updated_at if letter else purchase.updated_at,
        )

    @staticmethod
    def _photo_schema(letter: Letter, photo: LetterPhoto) -> PhotoResponse:
        return PhotoResponse(
            id=photo.id,
            position=photo.position,
            caption=photo.caption,
            contentType=photo.content_type,
            byteSize=photo.byte_size,
            url=f"/api/v1/letters/{letter.id}/photos/{photo.id}/content",
        )

    @staticmethod
    def _delivery_schema(delivery: LetterDelivery) -> DeliveryResponse:
        return DeliveryResponse(
            id=delivery.id,
            recipientEmail=delivery.recipient_email,
            status=delivery.status,
            attempts=delivery.attempts,
            letterVersion=delivery.letter_version,
            lastError=delivery.last_error,
            sentAt=delivery.sent_at,
            createdAt=delivery.created_at,
        )

    async def _letter_payload(self, letter: Letter) -> LetterResponse:
        photos = await repo.photos_of_letter(self.db, letter.id)
        sent = await repo.deliveries_of_letter(self.db, letter.id)
        url = letters.public_url(self.settings, letter)
        base = self.settings.public_base_url
        return LetterResponse(
            id=letter.id,
            purchaseId=letter.purchase_id,
            status=letter.status,
            title=letter.title,
            recipientName=letter.recipient_name,
            recipientEmail=letter.recipient_email,
            body=letter.body,
            theme=letter.theme,
            publicSlug=letter.public_slug if letter.status == "published" else None,
            publicUrl=url,
            qrUrl=f"{base}/api/v1/letters/{letter.id}/qr.png" if url else None,
            cardUrl=f"{base}/api/v1/letters/{letter.id}/card.pdf" if url else None,
            publishedVersion=letter.published_version,
            publishedAt=letter.published_at,
            frozen=letters.is_frozen(letter, self.settings),
            photos=[self._photo_schema(letter, photo) for photo in photos],
            deliveries=[self._delivery_schema(item) for item in sent],
            createdAt=letter.created_at,
            updatedAt=letter.updated_at,
        )
