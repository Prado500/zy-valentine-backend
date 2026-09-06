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
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

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


class UserIdentityDocument(Base):
    """Cédula del comprador: dato privado, nunca credencial ni identificador público.

    Se guarda solo el HMAC del número (clave dedicada) y los últimos dígitos para
    que el usuario reconozca su documento. El número en claro no se persiste ni
    se devuelve en ninguna respuesta.
    """

    __tablename__ = "user_identity_documents"
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    document_type: Mapped[str] = mapped_column(String(8))
    document_hash: Mapped[str] = mapped_column(String(64), unique=True)
    document_last4: Mapped[str] = mapped_column(String(4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        CheckConstraint("document_type in ('CC','CE','PA','NIT')", name="ck_document_type"),
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
