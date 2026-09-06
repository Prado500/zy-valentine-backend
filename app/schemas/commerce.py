import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.schemas.auth import Input

# Emojis, acentos y saltos de línea son contenido válido de una carta; solo se
# rechazan caracteres de control que romperían el visor o el correo.
FORBIDDEN_CONTROL = {chr(code) for code in range(32)} - {"\n", "\t"}


def _clean_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if any(character in FORBIDDEN_CONTROL for character in value):
        raise ValueError("Text contains control characters")
    return value


class IdentityDocumentInput(Input):
    """Cédula: dato privado. Nunca viaja de vuelta ni aparece en enlaces públicos."""

    documentType: str = Field(pattern="^(CC|CE|PA|NIT)$")
    documentNumber: str = Field(min_length=5, max_length=20, pattern=r"^[0-9A-Za-z\-]+$")


class IdentityDocumentResponse(BaseModel):
    documentType: str
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
    def nonblank(cls, value: str) -> str:
        value = _clean_text(value).strip()
        if not value:
            raise ValueError("Field cannot be blank")
        return value

    @field_validator("body")
    @classmethod
    def clean_body(cls, value: str) -> str:
        value = _clean_text(value)
        if not value.strip():
            raise ValueError("Body cannot be blank")
        return value


class LetterCreate(LetterInput):
    purchaseId: uuid.UUID


class LetterUpdate(LetterInput):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    recipientName: str | None = Field(default=None, min_length=1, max_length=120)
    body: str | None = Field(default=None, min_length=1, max_length=5000)
    theme: str | None = Field(default=None, max_length=32, pattern=r"^[a-z0-9\-]+$")


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
    publishedVersion: int
    publishedAt: datetime | None
    frozen: bool
    photos: list[PhotoResponse]
    deliveries: list[DeliveryResponse]
    createdAt: datetime
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


class ResendInput(Input):
    recipientEmail: EmailStr | None = None
