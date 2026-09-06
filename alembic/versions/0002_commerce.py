"""Identidad privada, compras, pagos, cartas, fotos y entregas.

Las reglas de negocio críticas viven como restricciones de base de datos:
una carta por compra (UNIQUE en letters.purchase_id), una compra por clave de
idempotencia y un webhook procesado una sola vez.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002_commerce"
down_revision = "0001_base_auth"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_identity_documents",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("document_type", sa.String(8), nullable=False),
        sa.Column("document_hash", sa.String(64), nullable=False),
        sa.Column("document_last4", sa.String(4), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("document_type in ('CC','CE','PA','NIT')", name="ck_document_type"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("document_hash"),
    )
    op.create_table(
        "purchases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("external_reference", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), server_default="none", nullable=False),
        sa.Column("provider_preference_id", sa.String(128), nullable=True),
        sa.Column("checkout_url", sa.String(512), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("amount_cents >= 0", name="ck_purchase_amount"),
        sa.CheckConstraint(
            "status in ('pending','paid','cancelled','expired')", name="ck_purchase_status"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_reference"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_purchases_user_idempotency"),
    )
    op.create_index("ix_purchases_user_id", "purchases", ["user_id"])
    op.create_index("ix_purchases_expires_at", "purchases", ["expires_at"])
    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purchase_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_payment_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_detail", sa.String(64), nullable=True),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column(
            "verified_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status in ('pending','in_process','approved','rejected','cancelled',"
            "'refunded','charged_back')",
            name="ck_payment_status",
        ),
        sa.ForeignKeyConstraint(["purchase_id"], ["purchases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "provider_payment_id", name="uq_payments_provider_payment"),
    )
    op.create_index("ix_payments_purchase_id", "payments", ["purchase_id"])
    op.create_table(
        "payment_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("provider_payment_id", sa.String(64), nullable=True),
        sa.Column("action", sa.String(64), nullable=True),
        sa.Column("applied", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "event_id", name="uq_payment_events_provider_event"),
    )
    op.create_index(
        "ix_payment_events_provider_payment_id", "payment_events", ["provider_payment_id"]
    )
    op.create_table(
        "letters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purchase_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("public_slug", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="draft", nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("recipient_name", sa.String(120), nullable=False),
        sa.Column("recipient_email", sa.String(320), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("theme", sa.String(32), server_default="classic", nullable=False),
        sa.Column("published_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status in ('draft','published')", name="ck_letter_status"),
        sa.ForeignKeyConstraint(["purchase_id"], ["purchases.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Una compra pagada habilita exactamente una carta.
        sa.UniqueConstraint("purchase_id"),
        sa.UniqueConstraint("public_slug"),
    )
    op.create_index("ix_letters_user_id", "letters", ["user_id"])
    op.create_table(
        "letter_photos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("letter_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("caption", sa.String(200), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("position >= 0", name="ck_photo_position"),
        sa.ForeignKeyConstraint(["letter_id"], ["letters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("letter_id", "position", name="uq_letter_photos_position"),
    )
    op.create_index("ix_letter_photos_letter_id", "letter_photos", ["letter_id"])
    op.create_table(
        "letter_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("letter_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_email", sa.String(320), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("letter_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(200), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status in ('pending','sent','failed')", name="ck_delivery_status"),
        sa.ForeignKeyConstraint(["letter_id"], ["letters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_letter_deliveries_letter_id", "letter_deliveries", ["letter_id"])


def downgrade():
    op.drop_table("letter_deliveries")
    op.drop_table("letter_photos")
    op.drop_table("letters")
    op.drop_table("payment_events")
    op.drop_table("payments")
    op.drop_table("purchases")
    op.drop_table("user_identity_documents")
