import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.schemas.auth import DocumentInput, Input

# Emojis, acentos y saltos de línea son contenido válido de una carta; solo se
# rechazan caracteres de control que romperían el visor o el correo.
FORBIDDEN_CONTROL = {chr(code) for code in range(32)} - {"\n", "\t"}


def _clean_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if any(character in FORBIDDEN_CONTROL for character in value):
        raise ValueError("Text contains control characters")
    return value


class IdentityDocumentInput(DocumentInput):
    """Documento: dato privado. Nunca viaja de vuelta ni aparece en enlaces públicos."""


class IdentityDocumentResponse(BaseModel):
    # Código DIAN; la etiqueta la pone el frontend con su copia del catálogo.
    documentType: int
    documentLast4: str
    createdAt: datetime


class PurchaseCreate(Input):
    idempotencyKey: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._\-]+$")


class PaymentVerifyInput(Input):
    """El navegador solo aporta el identificador devuelto por el proveedor."""

    paymentId: str | None = Field(default=None, max_length=64, pattern=r"^[0-9]+$")


class PurchaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    status: str
    amountCents: int
    currency: str
    externalReference: str
    checkoutUrl: str | None
    hasLetter: bool = False
    paidAt: datetime | None
    expiresAt: datetime
    createdAt: datetime


class PaymentResponse(BaseModel):
    status: str
    statusDetail: str | None
    providerPaymentId: str
    verifiedAt: datetime


class PurchaseVerification(BaseModel):
    purchase: PurchaseResponse
    payment: PaymentResponse | None
    canCreateLetter: bool


class LetterInput(Input):
    title: str = Field(min_length=1, max_length=120)
    recipientName: str = Field(min_length=1, max_length=120)
    recipientEmail: EmailStr | None = None
    body: str = Field(min_length=1, max_length=5000)
    theme: str = Field(default="classic", max_length=32, pattern=r"^[a-z0-9\-]+$")

    @field_validator("title", "recipientName")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        # `LetterUpdate` hereda este validador con campos opcionales: un `null` explícito
        # significa "no tocar", igual que omitir el campo, y no debe llegar a `.replace`
        # (antes reventaba con AttributeError y la petición acababa en un 500).
        if value is None:
            return None
        value = _clean_text(value).strip()
        if not value:
            raise ValueError("Field cannot be blank")
        return value

    @field_validator("body")
    @classmethod
    def clean_body(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = _clean_text(value)
        if not value.strip():
            raise ValueError("Body cannot be blank")
        return value


class TempPhotoRef(Input):
    """Foto ya subida al contenedor efímero, pendiente de trasladar (eager upload).

    ``tempId`` es la clave que devolvió ``POST /api/v1/letters/photos/eager``, no un
    nombre libre: el patrón la ata a la forma ``temporal/<usuario>/<uuid>.<ext>``. Sin
    esa restricción, un cliente podría pedir el traslado de un blob ajeno o escaparse
    del contenedor con una ruta relativa.
    """

    tempId: str = Field(
        min_length=16,
        max_length=200,
        pattern=r"^temporal/[0-9a-f\-]{36}/[0-9a-f]{32}\.(jpg|png|webp)$",
    )
    fileName: str = Field(min_length=1, max_length=200)
    position: int | None = Field(default=None, ge=0, le=99)
    # El frontend devuelve el mismo objeto que le entregó el eager upload, así que
    # estos dos campos se aceptan para no obligarle a recortarlo. **No se usan**: el
    # tipo se deduce de la extensión que puso el servidor y el tamaño lo devuelve el
    # traslado del blob. Un cliente no decide qué hay dentro de un archivo.
    contentType: str | None = Field(default=None, max_length=64)
    byteSize: int | None = Field(default=None, ge=0)

    @field_validator("fileName")
    @classmethod
    def clean_name(cls, value: str) -> str:
        # El nombre original acaba en `caption`, que se muestra en el visor y en el
        # correo: se limpia igual que cualquier otro texto de la carta.
        value = _clean_text(value).strip()
        if not value:
            raise ValueError("File name cannot be blank")
        return value


class EagerPhotoResponse(BaseModel):
    """Acuse del eager upload. `tempId` es lo que el frontend adjunta a la carta."""

    tempId: str
    fileName: str
    contentType: str
    byteSize: int


class LetterCreate(LetterInput):
    purchaseId: uuid.UUID
    # Publicar y enviar el correo en el mismo acto (IOP #7). Es lo que espera el
    # frontend, que no tiene pantalla de borradores: tras pagar, escribe la carta y
    # espera el correo. Con False la carta queda en borrador para editarla y
    # publicarla después con POST /letters/{id}/publish. Se aplica igual en el camino
    # síncrono y en el de la cola: es el mismo campo que viaja en el sobre del worker.
    autoPublish: bool = True
    # Fotos subidas en caliente antes de crear la carta. El worker las traslada al
    # contenedor permanente (IOP #6); aquí solo viajan sus referencias.
    temp_photos: list[TempPhotoRef] = Field(default_factory=list, max_length=20)


class LetterUpdate(LetterInput):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    recipientName: str | None = Field(default=None, min_length=1, max_length=120)
    body: str | None = Field(default=None, min_length=1, max_length=5000)
    theme: str | None = Field(default=None, max_length=32, pattern=r"^[a-z0-9\-]+$")


class LetterQueued(BaseModel):
    """Respuesta 202 del camino por eventos: la carta aún no existe en la base.

    El frontend debe consultar ``GET /api/v1/letters`` (o la compra) para ver la carta
    cuando el worker la haya escrito; ``hasLetter`` de la compra es la señal.
    """

    status: str = "queued"
    purchaseId: uuid.UUID
    message: str = "Tu carta está en proceso; te avisaremos por correo al terminar."


class PhotoResponse(BaseModel):
    id: uuid.UUID
    position: int
    caption: str | None
    contentType: str
    byteSize: int
    url: str


class DeliveryResponse(BaseModel):
    id: uuid.UUID
    recipientEmail: str
    status: str
    attempts: int
    letterVersion: int
    lastError: str | None
    sentAt: datetime | None
    createdAt: datetime


class LetterResponse(BaseModel):
    id: uuid.UUID
    purchaseId: uuid.UUID
    status: str
    title: str
    recipientName: str
    recipientEmail: str | None
    body: str
    theme: str
    publicSlug: str | None
    publicUrl: str | None
    qrUrl: str | None
    cardUrl: str | None
    publishedVersion: int
    publishedAt: datetime | None
    frozen: bool
    photos: list[PhotoResponse]
    deliveries: list[DeliveryResponse]
    createdAt: datetime
    updatedAt: datetime


class DedicationResponse(BaseModel):
    """Una fila del panel "Mis dedicatorias".

    Ligera a propósito: sin cuerpo, fotos ni entregas, que siguen en ``GET /letters/{id}``.
    ``state`` se deriva al leer (ver ``app.services.dedications``): ``draft`` es una compra
    pagada sin carta o una carta en borrador; ``published``, una carta ya publicada. Con
    ``letterId`` nulo el frontend abre el editor con ``purchaseId``; si hay carta en
    borrador, el mismo ``POST /letters`` la retoma desde cero.
    """

    purchaseId: uuid.UUID
    letterId: uuid.UUID | None
    state: Literal["draft", "published"]
    title: str | None
    recipientName: str | None
    theme: str | None
    publicSlug: str | None
    publicUrl: str | None
    paidAt: datetime | None
    publishedAt: datetime | None
    updatedAt: datetime


class PublicPhoto(BaseModel):
    position: int
    caption: str | None
    url: str


class PublicLetterResponse(BaseModel):
    """Vista pública: sin usuario, sin correo del comprador y sin cédula."""

    letterId: uuid.UUID
    publishedVersion: int
    title: str
    recipientName: str
    body: str
    theme: str
    photos: list[PublicPhoto]
    publishedAt: datetime


class CommerceHealth(BaseModel):
    """Diagnóstico de integraciones. Nombres de modo, nunca credenciales ni URLs."""

    paymentProvider: str
    storageBackend: str
    mailBackend: str
    letterQueue: str
    freezeAfterPublish: bool


class ResendInput(Input):
    recipientEmail: EmailStr | None = None
