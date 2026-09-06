"""Webhooks de pago: firmados, idempotentes y tolerantes al desorden.

- La firma se valida antes de tocar la base de datos.
- ``uq_payment_events_provider_event`` garantiza que una notificación repetida se
  registre una sola vez; la repetición responde 200 sin volver a aplicar nada.
- El estado se aplica con :func:`app.services.purchases.apply_snapshot`, que nunca
  retrocede de rango, de modo que un webhook viejo no "despaga" una compra.
"""

import json

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models.commerce import PaymentEvent
from app.repositories import commerce
from app.services import purchases
from app.services.payments import PaymentGateway

PROVIDER = "mercadopago"


def decode(body: bytes) -> dict:
    """Interpreta el cuerpo del webhook. Debe llamarse **después** de validar la firma.

    Un cuerpo que no sea un objeto JSON se rechaza aquí y no llega a la base: lo que
    manda un tercero por la red es una entrada, no un dato de confianza.
    """
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise ApiError(422, "INVALID_WEBHOOK", "Cuerpo de notificación inválido.") from None
    if not isinstance(payload, dict):
        raise ApiError(422, "INVALID_WEBHOOK", "Cuerpo de notificación inválido.")
    return payload


def extract_event(payload: dict, query_id: str | None) -> tuple[str, str | None, str | None]:
    """Devuelve (event_id, provider_payment_id, action) del cuerpo de Mercado Pago."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    payment_id = data.get("id") or payload.get("id") or query_id
    payment_id = str(payment_id)[:64] if payment_id is not None else None
    action = str(payload.get("action") or payload.get("type") or "")[:64] or None
    # Mercado Pago reenvía la misma notificación; su "id" superior identifica el evento.
    event_id = str(payload.get("id") or f"{action}:{payment_id}")[:128]
    return event_id, payment_id, action


async def handle(
    db: AsyncSession, gateway: PaymentGateway, payload: dict, query_id: str | None
) -> str:
    """Devuelve un resultado textual: duplicate | ignored | applied | unknown_purchase."""
    event_id, payment_id, action = extract_event(payload, query_id)
    if await commerce.event_exists(db, PROVIDER, event_id):
        return "duplicate"
    event = PaymentEvent(
        provider=PROVIDER,
        event_id=event_id,
        provider_payment_id=payment_id,
        action=action,
        detail={"action": action, "payment_id": payment_id},
    )
    db.add(event)
    try:
        await db.commit()
    except IntegrityError:
        # Dos entregas simultáneas del mismo webhook: solo una continúa.
        await db.rollback()
        return "duplicate"

    if not payment_id or (action and not action.startswith("payment")):
        return "ignored"
    snapshot = await gateway.fetch_payment(payment_id)
    if not snapshot.external_reference:
        return "ignored"
    purchase = await commerce.purchase_by_reference(db, snapshot.external_reference)
    if purchase is None:
        return "unknown_purchase"
    try:
        await purchases.apply_snapshot(db, purchase, snapshot)
    except ApiError:
        # Un pago que no cuadra queda registrado en el evento y no altera la compra.
        return "ignored"
    event.applied = True
    await db.commit()
    return "applied"
