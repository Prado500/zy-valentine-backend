"""Contador de cupos: lectura pública y el descuento al confirmarse un pago.

Todas las comprobaciones son **relativas** (leer, actuar, releer). El contador es una
fila única que la migración siembra y que el `conftest` rebobina entre casos; atarse a la
cifra absoluta convertiría cualquier ajuste de la semilla en una prueba rota, cuando lo
que se está verificando es el movimiento, no el punto de partida.
"""

import asyncio

from sqlalchemy import select, text

from app.models.commerce import SLOTS_SEED_SOLD, SLOTS_TOTAL, CampaignSlots
from tests.conftest import new_purchase, pay
from tests.test_commerce import send_webhook


async def read_slots(client):
    response = await client.get("/api/v1/public/slots")
    assert response.status_code == 200, response.text
    return response.json()


async def sold(app) -> int:
    async with app.state.sessions() as db:
        return await db.scalar(select(CampaignSlots.sold).where(CampaignSlots.id == 1))


# --- Lectura pública ------------------------------------------------------------------


async def test_slots_are_public_and_need_no_session(client):
    """La landing pide esta cifra antes de que nadie inicie sesión."""
    body = await read_slots(client)
    assert body == {
        "total": SLOTS_TOTAL,
        "taken": SLOTS_SEED_SOLD,
        "remaining": SLOTS_TOTAL - SLOTS_SEED_SOLD,
    }


async def test_seed_keeps_the_number_the_landing_already_showed(client):
    """La semilla existe para que el número público no salte el día del despliegue."""
    assert (await read_slots(client))["remaining"] == 8364


# --- El evento: una compra pagada resta un cupo ---------------------------------------


async def test_paid_purchase_consumes_exactly_one_slot(client, gateway, buyer, app):
    before = await sold(app)
    purchase = await new_purchase(client)
    await pay(client, gateway, purchase)
    assert await sold(app) == before + 1


async def test_verify_repeated_does_not_consume_twice(client, gateway, buyer, app):
    """Volver de Mercado Pago dos veces, o recargar la pestaña, no gasta dos cupos."""
    purchase = await new_purchase(client)
    await pay(client, gateway, purchase)
    after_first = await sold(app)
    await pay(client, gateway, purchase)
    assert await sold(app) == after_first


async def test_webhook_consumes_one_and_only_once(client, gateway, buyer, app):
    before = await sold(app)
    purchase = await new_purchase(client)
    gateway.approve("930001", purchase["externalReference"], purchase["amountCents"])
    assert (await send_webhook(client, 9301, "930001")).json()["result"] == "applied"
    assert await sold(app) == before + 1
    # La reentrega del mismo evento se corta antes de tocar la compra.
    assert (await send_webhook(client, 9301, "930001")).json()["result"] == "duplicate"
    assert await sold(app) == before + 1


async def test_webhook_after_verify_does_not_consume_a_second_slot(client, gateway, buyer, app):
    """Los dos caminos confirman el mismo pago: el cupo se gasta una vez, no dos.

    Es la prueba que falla si `lock_purchase` devuelve la compra obsoleta del mapa de
    identidad: el webhook volvería a ver `pending` y descontaría otra vez.
    """
    before = await sold(app)
    purchase = await new_purchase(client)
    await pay(client, gateway, purchase, payment_id="930002")
    assert (await send_webhook(client, 9302, "930002")).json()["result"] == "applied"
    assert await sold(app) == before + 1


async def test_out_of_order_webhook_does_not_move_the_counter(client, gateway, buyer, app):
    """Una notificación vieja no revierte el pago (ya se probaba) ni el contador."""
    purchase = await new_purchase(client)
    gateway.approve("930003", purchase["externalReference"], purchase["amountCents"])
    assert (await send_webhook(client, 9303, "930003")).json()["result"] == "applied"
    after_paid = await sold(app)
    gateway.set_status("930003", purchase["externalReference"], "pending", purchase["amountCents"])
    assert (await send_webhook(client, 9304, "930003")).json()["result"] == "applied"
    assert await sold(app) == after_paid


# --- Concurrencia ---------------------------------------------------------------------


async def test_concurrent_payments_consume_one_slot_each(client, gateway, buyer, app):
    """Tres compras distintas pagadas a la vez gastan exactamente tres cupos.

    El incremento va en SQL (`sold = sold + 1`), así que las tres transacciones se
    serializan en la fila del contador en vez de pisarse leyendo y reescribiendo.
    """
    before = await sold(app)
    purchases = [await new_purchase(client, key=f"idem-cupo-{index}") for index in range(3)]
    for index, purchase in enumerate(purchases):
        gateway.approve(f"9310{index}", purchase["externalReference"], purchase["amountCents"])
    await asyncio.gather(
        *[
            client.post(
                f"/api/v1/purchases/{purchase['id']}/verify",
                json={"paymentId": f"9310{index}"},
            )
            for index, purchase in enumerate(purchases)
        ]
    )
    assert await sold(app) == before + 3


async def test_same_payment_verified_concurrently_consumes_one(client, gateway, buyer, app):
    """Doble clic sobre la misma compra: el `FOR UPDATE` los serializa y solo uno cuenta."""
    before = await sold(app)
    purchase = await new_purchase(client)
    gateway.approve("930200", purchase["externalReference"], purchase["amountCents"])
    await asyncio.gather(
        *[
            client.post(
                f"/api/v1/purchases/{purchase['id']}/verify", json={"paymentId": "930200"}
            )
            for _ in range(3)
        ]
    )
    assert await sold(app) == before + 1


# --- Reembolsos -----------------------------------------------------------------------


async def test_refund_returns_the_slot_once(client, gateway, buyer, app):
    before = await sold(app)
    purchase = await new_purchase(client)
    gateway.approve("930400", purchase["externalReference"], purchase["amountCents"])
    assert (await send_webhook(client, 9340, "930400")).json()["result"] == "applied"
    assert await sold(app) == before + 1

    gateway.set_status(
        "930400", purchase["externalReference"], "refunded", purchase["amountCents"]
    )
    assert (await send_webhook(client, 9341, "930400")).json()["result"] == "applied"
    assert await sold(app) == before

    # El segundo reembolso encuentra la compra ya `cancelled`: no regala otro cupo.
    assert (await send_webhook(client, 9342, "930400")).json()["result"] == "applied"
    assert await sold(app) == before


# --- Estados que no mueven el contador ------------------------------------------------


async def test_pending_purchase_does_not_consume(client, gateway, buyer, app):
    before = await sold(app)
    await new_purchase(client)
    assert await sold(app) == before


async def test_rejected_payment_does_not_consume(client, gateway, buyer, app):
    before = await sold(app)
    purchase = await new_purchase(client)
    gateway.set_status(
        "930500", purchase["externalReference"], "rejected", purchase["amountCents"]
    )
    response = await client.post(
        f"/api/v1/purchases/{purchase['id']}/verify", json={"paymentId": "930500"}
    )
    assert response.status_code == 200
    assert response.json()["purchase"]["status"] == "pending"
    assert await sold(app) == before


async def test_amount_mismatch_rolls_back_the_decrement(client, gateway, buyer, app):
    """Un pago por menos de lo debido aborta con 409 y no deja el contador tocado."""
    before = await sold(app)
    purchase = await new_purchase(client)
    gateway.approve("930600", purchase["externalReference"], purchase["amountCents"] - 1)
    response = await client.post(
        f"/api/v1/purchases/{purchase['id']}/verify", json={"paymentId": "930600"}
    )
    assert response.status_code == 409
    assert await sold(app) == before


# --- Sobreventa -----------------------------------------------------------------------


async def test_oversold_counter_reads_zero_and_never_negative(client, app):
    """El contador informa, no frena: si se pasa de la raya, la landing enseña 0.

    Un `CHECK (sold <= total)` habría hecho fallar el pago que provocara la sobreventa,
    que es exactamente lo que no puede pasar: el proveedor ya cobró.
    """
    async with app.state.engine.begin() as conn:
        await conn.execute(text("UPDATE campaign_slots SET sold = total + 5 WHERE id = 1"))
    body = await read_slots(client)
    assert body["remaining"] == 0
    assert body["taken"] == body["total"] + 5
