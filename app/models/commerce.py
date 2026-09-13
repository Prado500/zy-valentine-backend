"""Dominio comercial: identidad privada, compras, pagos, cartas y entregas.

Estados separados a propósito (una tabla no reutiliza el estado de otra):

- ``users.is_active`` / ``email_verified``  -> estado de la identidad.
- ``purchases.status``                      -> estado comercial de la compra.
- ``payments.status``                       -> estado reportado por el proveedor.
- ``letters.status``                        -> borrador o publicada.
- ``letter_deliveries.status``              -> estado del envío de correo.

Reglas garantizadas por la base de datos, no solo por el servicio:

- ``uq_purchases_user_idempotency``: una compra por clave de idempotencia y usuario.
- ``uq_payments_provider_payment``: un pago por identificador del proveedor.
- ``uq_payment_events_provider_event``: un webhook procesado una sola vez.
- ``letters.purchase_id`` UNIQUE: una compra pagada habilita exactamente una carta.
- ``uq_letter_photos_position``: orden de fotos estable y sin duplicados.
- ``ck_document_type``: el tipo de documento es uno de los nueve códigos DIAN.
- ``uq_consent_user_kind_version``: una sola aceptación por persona, documento y versión.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.dian import CODES as DIAN_CODES
from app.models.base import Base

PURCHASE_STATUSES = ("pending", "paid", "cancelled", "expired")
PAYMENT_STATUSES = (
    "pending",
    "in_process",
    "approved",
    "rejected",
    "cancelled",
    "refunded",
    "charged_back",
)
# Orden de aplicación para webhooks fuera de orden: nunca se retrocede de rango.
PAYMENT_STATUS_RANK = {
    "pending": 1,
    "in_process": 1,
    "rejected": 2,
    "cancelled": 2,
    "approved": 3,
    "refunded": 4,
    "charged_back": 4,
}
LETTER_STATUSES = ("draft", "published")
DELIVERY_STATUSES = ("pending", "sent", "failed")
CONSENT_KINDS = ("terms_and_privacy",)


class UserIdentityDocument(Base):
    """Documento del comprador: dato privado, nunca credencial ni identificador público.

    Cuatro representaciones, cada una con un cometido:

    - ``document_type``: el código oficial DIAN (``app.core.dian``), no una sigla.
    - ``document_hash``: HMAC con clave dedicada. Índice ciego para la unicidad
      entre cuentas sin exponer el número.
    - ``document_last4``: para que la persona reconozca su documento.
    - ``document_cipher``: sobre AES-256-GCM atado al usuario (``app.core.crypto``).
      Es lo único de lo que se recupera el número real, que exigen la factura
      electrónica de la DIAN y el pagador de Mercado Pago.

    El número en claro no se persiste ni se devuelve en ninguna respuesta.
    """

    __tablename__ = "user_identity_documents"
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    document_type: Mapped[int] = mapped_column(SmallInteger)
    document_hash: Mapped[str] = mapped_column(String(64), unique=True)
    document_last4: Mapped[str] = mapped_column(String(4))
    document_cipher: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        CheckConstraint(
            f"document_type in ({', '.join(str(code) for code in DIAN_CODES)})",
            name="ck_document_type",
        ),
    )


class UserConsent(Base):
    """Prueba de la autorización de tratamiento de datos (Decreto 1377 de 2013, art. 7).

    **Append-only.** Nunca se actualiza una fila: cuando cambia la política sube su
    versión y se inserta otra. Eso es lo que hace que el registro sea auditable;
    un UPDATE borraría la prueba de lo que la persona aceptó aquel día.

    ``document_checksum`` es el SHA-256 del texto exacto que se mostró. No es una
    promesa de qué decía la política: es la prueba.
    """

    __tablename__ = "user_consents"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    document_version: Mapped[str] = mapped_column(String(32))
    document_checksum: Mapped[str] = mapped_column(String(64))
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # La IP que se guarda hoy es la del ingress de Azure: uvicorn corre con
    # --no-proxy-headers. Queda pendiente el PR de X-Forwarded-For.
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    __table_args__ = (
        UniqueConstraint(
            "user_id", "kind", "document_version", name="uq_consent_user_kind_version"
        ),
        CheckConstraint("kind in ('terms_and_privacy')", name="ck_consent_kind"),
    )


class Purchase(Base):
    __tablename__ = "purchases"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    external_reference: Mapped[str] = mapped_column(String(64), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32), default="none", server_default="none")
    provider_preference_id: Mapped[str | None] = mapped_column(String(128))
    checkout_url: Mapped[str | None] = mapped_column(String(512))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_purchases_user_idempotency"),
        CheckConstraint(
            "status in ('pending','paid','cancelled','expired')", name="ck_purchase_status"
        ),
        CheckConstraint("amount_cents >= 0", name="ck_purchase_amount"),
    )


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    purchase_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchases.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    provider_payment_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    status_detail: Mapped[str | None] = mapped_column(String(64))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        UniqueConstraint("provider", "provider_payment_id", name="uq_payments_provider_payment"),
        CheckConstraint(
            "status in ('pending','in_process','approved','rejected','cancelled',"
            "'refunded','charged_back')",
            name="ck_payment_status",
        ),
    )


class PaymentEvent(Base):
    """Bitácora de webhooks. La unicidad evita reprocesar notificaciones repetidas."""

    __tablename__ = "payment_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(32))
    event_id: Mapped[str] = mapped_column(String(128))
    provider_payment_id: Mapped[str | None] = mapped_column(String(64), index=True)
    action: Mapped[str | None] = mapped_column(String(64))
    applied: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    detail: Mapped[dict | None] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_payment_events_provider_event"),
    )


class Letter(Base):
    __tablename__ = "letters"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # UNIQUE: una compra pagada habilita exactamente una carta.
    purchase_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchases.id", ondelete="RESTRICT"), unique=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    public_slug: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")
    title: Mapped[str] = mapped_column(String(120))
    recipient_name: Mapped[str] = mapped_column(String(120))
    recipient_email: Mapped[str | None] = mapped_column(String(320))
    body: Mapped[str] = mapped_column(Text)
    theme: Mapped[str] = mapped_column(String(32), default="classic", server_default="classic")
    published_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (CheckConstraint("status in ('draft','published')", name="ck_letter_status"),)


class LetterPhoto(Base):
    __tablename__ = "letter_photos"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    letter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("letters.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer)
    caption: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("letter_id", "position", name="uq_letter_photos_position"),
        CheckConstraint("position >= 0", name="ck_photo_position"),
    )


class LetterDelivery(Base):
    """Un envío de correo. Reenviar crea otra fila, nunca otra carta ni otra compra."""

    __tablename__ = "letter_deliveries"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    letter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("letters.id", ondelete="CASCADE"), index=True
    )
    recipient_email: Mapped[str] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    letter_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(200))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        CheckConstraint("status in ('pending','sent','failed')", name="ck_delivery_status"),
    )
