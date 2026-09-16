"""Contador de cupos de la campaña.

Una sola fila (``id = 1``) con el total activado y los cupos ya tomados. Nace sembrada
con ``sold = 1636``, que es la cifra que la landing venía mostrando escrita a mano
(10000 - 8364): sembrar en 0 haría saltar el número público de 8.364 a 10.000 el día del
despliegue, justo lo que el mensaje de escasez no puede permitirse. Desde esa base solo
lo mueven ventas reales.

La semilla va aquí y no en el arranque de la aplicación: el arranque no escribe en la
base (ver ``app/main.py``), y una fila que se crea sola en cada despliegue es una fila
que se recrea después de que alguien la ajuste a mano.

Los literales están escritos a pelo a propósito. Una migración no importa ``app.models``:
debe seguir describiendo el esquema de su momento aunque el modelo cambie después.
"""

import sqlalchemy as sa

from alembic import op

revision = "0004_campaign_slots"
down_revision = "0003_dian_and_consent"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "campaign_slots",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("sold", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="ck_campaign_slots_singleton"),
        sa.CheckConstraint("sold >= 0 and total >= 0", name="ck_campaign_slots_counts"),
    )
    # ON CONFLICT DO NOTHING: aplicar la migración sobre una base que ya tuviera la fila
    # (una restauración, un entorno resembrado a mano) no puede reventar ni pisar la cifra.
    op.execute(
        "insert into campaign_slots (id, total, sold) values (1, 10000, 1636) "
        "on conflict (id) do nothing"
    )


def downgrade():
    op.drop_table("campaign_slots")
