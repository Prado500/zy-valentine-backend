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
from typing import Any, NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.models.commerce import Letter, LetterDelivery, LetterPhoto, Purchase
from app.models.user import User
from app.repositories import commerce as repo
from app.schemas.commerce import (
    CommerceHealth,
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
from app.services import deliveries, identity, letters, purchases, webhooks
from app.services.mailer import Mailer
from app.services.payments import PaymentGateway
from app.services.qr import qr_png
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
    """Bytes servidos tal cual: una foto o un PNG de QR."""

    content: bytes
    media_type: str


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
        """IOP #4 a #6: valida la compra, encola si se puede y escribe si no.

        Con la cola disponible se responde 202 sin escribir nada. Sin ella —o si el
        transporte falla— se conserva el camino síncrono de siempre, incluido el
        traslado de las fotos del contenedor efímero.
        """
        if self.queue.enabled and await self._try_enqueue(user, payload):
            return Outcome(LetterQueued(purchaseId=payload.purchaseId), 202)
        letter, created = await letters.create(self.db, self.settings, user, payload)
        if created:
            await letters.attach_temp_photos(
                self.db, self.settings, self.storage, letter, payload.temp_photos
            )
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
        await deliveries.deliver(self.db, self.settings, self.mailer, letter, storage=self.storage)
        return await self._letter_payload(letter)

    async def resend_letter(
        self, user: User, letter_id: uuid.UUID, payload: ResendInput
    ) -> DeliveryResponse:
        """Reenviar no consume otra compra ni crea otra carta: solo otro intento de envío."""
        letter = await self._owned_letter(user, letter_id)
        recipient = str(payload.recipientEmail) if payload.recipientEmail else None
        delivery = await deliveries.deliver(
            self.db, self.settings, self.mailer, letter, recipient, storage=self.storage
        )
        return self._delivery_schema(delivery)

    async def letter_qr(self, user: User, letter_id: uuid.UUID) -> BinaryContent:
        letter = await self._owned_letter(user, letter_id)
        url = letters.public_url(self.settings, letter)
        if not url:
            raise ApiError(409, "LETTER_NOT_PUBLISHED", "Publica la carta para obtener su QR.")
        return BinaryContent(qr_png(url), "image/png")

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
        letter = await self._published_letter(slug)
        return BinaryContent(qr_png(letters.public_url(self.settings, letter)), "image/png")

    def health(self) -> CommerceHealth:
        """Diagnóstico sin secretos: qué integraciones están configuradas."""
        return CommerceHealth(
            paymentProvider=self.settings.payment_provider,
            storageBackend=self.settings.storage_backend,
            mailBackend=self.settings.mail_backend,
            letterQueue="service-bus" if self.settings.service_bus_enabled else "sync",
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

        letter, _ = await letters.create(self.db, self.settings, user, payload)
        await letters.attach_temp_photos(self.db, self.settings, self.storage, letter, refs)

        if not (message.get("autoPublish", True) and letter.recipient_email):
            return letter.id
        if letter.status != "published":
            await letters.publish(self.db, self.settings, letter)
        if await self._already_delivered(letter):
            return letter.id
        await deliveries.deliver(self.db, self.settings, self.mailer, letter, storage=self.storage)
        return letter.id

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
            publishedVersion=letter.published_version,
            publishedAt=letter.published_at,
            frozen=letters.is_frozen(letter, self.settings),
            photos=[self._photo_schema(letter, photo) for photo in photos],
            deliveries=[self._delivery_schema(item) for item in sent],
            createdAt=letter.created_at,
            updatedAt=letter.updated_at,
        )
