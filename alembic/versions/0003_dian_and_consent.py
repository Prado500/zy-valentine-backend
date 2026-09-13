"""Tipo de documento como código DIAN, número cifrado y consentimientos.

El tipo pasa de sigla a código numérico oficial. Las filas que ya existieran se
traducen con el mismo mapa que usa ``app.core.dian``; su ``document_hash`` **no** se
toca, porque el token del HMAC se conserva (ver el docstring de ese módulo).

El ``downgrade`` solo sabe volver a las cuatro siglas que existían antes. Si hay filas
con alguno de los cinco códigos nuevos falla a propósito, con un mensaje claro, en vez
de reetiquetarlas en silencio: un registro civil convertido en "CC" tendría además una
huella calculada con otro token y, al volver a migrar, dejaría de coincidir.
"""

import sqlalchemy as sa

from alembic import op

revision = "0003_dian_and_consent"
down_revision = "0002_commerce"
branch_labels = None
depends_on = None

DIAN_CODES = "(11, 12, 13, 21, 22, 31, 41, 42, 91)"
LEGACY_CODES = "(13, 22, 41, 31)"
TO_CODE = (
    "CASE document_type "
    "WHEN 'CC' THEN 13 WHEN 'CE' THEN 22 WHEN 'PA' THEN 41 WHEN 'NIT' THEN 31 END"
)
TO_LEGACY_LABEL = (
    "CASE document_type "
    "WHEN 13 THEN 'CC' WHEN 22 THEN 'CE' WHEN 41 THEN 'PA' WHEN 31 THEN 'NIT' END"
)
NOT_REVERSIBLE = (
    "user_identity_documents tiene tipos DIAN sin sigla heredada: "
    "no se puede revertir 0003 sin perder datos"
)


def upgrade():
    op.drop_constraint("ck_document_type", "user_identity_documents", type_="check")
    op.alter_column(
        "user_identity_documents",
        "document_type",
        existing_type=sa.String(8),
        type_=sa.SmallInteger(),
        existing_nullable=False,
        postgresql_using=f"({TO_CODE})::smallint",
    )
    op.create_check_constraint(
        "ck_document_type", "user_identity_documents", f"document_type in {DIAN_CODES}"
    )
    op.add_column(
        "user_identity_documents", sa.Column("document_cipher", sa.LargeBinary(), nullable=True)
    )

    op.create_table(
        "user_consents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("document_version", sa.String(32), nullable=False),
        sa.Column("document_checksum", sa.String(64), nullable=False),
        sa.Column(
            "accepted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.CheckConstraint("kind in ('terms_and_privacy')", name="ck_consent_kind"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "kind", "document_version", name="uq_consent_user_kind_version"
        ),
    )
    op.create_index("ix_user_consents_user_id", "user_consents", ["user_id"])


def downgrade():
    # Ruidoso a propósito: ver el docstring del módulo. Va primero para que la
    # transacción no toque nada si la vuelta atrás no es posible.
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS ("
            f"SELECT 1 FROM user_identity_documents WHERE document_type NOT IN {LEGACY_CODES}"
            f") THEN RAISE EXCEPTION '{NOT_REVERSIBLE}'; END IF; END $$"
        )
    )
    op.drop_index("ix_user_consents_user_id", table_name="user_consents")
    op.drop_table("user_consents")
    op.drop_column("user_identity_documents", "document_cipher")
    op.drop_constraint("ck_document_type", "user_identity_documents", type_="check")
    op.alter_column(
        "user_identity_documents",
        "document_type",
        existing_type=sa.SmallInteger(),
        type_=sa.String(8),
        existing_nullable=False,
        postgresql_using=TO_LEGACY_LABEL,
    )
    op.create_check_constraint(
        "ck_document_type", "user_identity_documents", "document_type in ('CC','CE','PA','NIT')"
    )
