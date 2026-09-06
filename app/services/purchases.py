"""Compras y verificación de pago en servidor.

Reglas:

- Un usuario puede tener muchas compras (IOP #5 se resuelve por carta, no por usuario).
- La misma clave de idempotencia devuelve **la misma** compra, nunca una nueva.
- El retorno del navegador no confirma nada: el estado se consulta al proveedor o
  llega por webhook firmado, y ambos pasan por :func:`apply_snapshot`.
- Un webhook fuera de orden o repetido nunca retrocede el estado del pago.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.models.commerce import PAYMENT_STATUS_RANK, Payment, Purchase
from app.models.user import User
from app.repositories import commerce
from app.schemas.commerce import PurchaseCreate
from app.services.payments import PaymentGateway, PaymentSnapshot


async def create_intent(
    db: AsyncSession,
    settings: Settings,
    gateway: PaymentGateway,
    user: User,
    payload: PurchaseCreate,
) -> tuple[Purchase, bool]:
    """Devuelve (compra, creada). Reintentos con la misma clave no duplican compras."""
    existing = await commerce.purchase_by_idempotency(db, user.id, payload.idempotencyKey)
    if existing:
        return existing, False
    reference = f"zv-{uuid.uuid4().hex}"
    preference_id, checkout_url = await gateway.create_preference(
        reference,
        settings.purchase_amount_cents,
        settings.purchase_currency,
        f"{settings.public_base_url}/pago/retorno?ref={reference}",
    )
    # ON CONFLICT DO NOTHING: dos pestañas simultáneas con la misma clave no producen
    # ni una segunda compra ni un error de integridad que obligue a deshacer la sesión.
    statement = (
        pg_insert(Purchase)
        .values(
            id=uuid.uuid4(),
            user_id=user.id,
            status="pending",
            amount_cents=settings.purchase_amount_cents,
            currency=settings.purchase_currency,
            external_reference=reference,
            idempotency_key=payload.idempotencyKey,
            provider=settings.payment_provider,
            provider_preference_id=preference_id,
            checkout_url=checkout_url,
            expires_at=datetime.now(UTC) + timedelta(minutes=settings.purchase_pending_minutes),
        )
        .on_conflict_do_nothing(constraint="uq_purchases_user_idempotency")
        .returning(Purchase.id)
    )
    created_id = await db.scalar(statement)
    await db.commit()
    if created_id is None:
        existing = await commerce.purchase_by_idempotency(db, user.id, payload.idempotencyKey)
        if not existing:
            raise ApiError(409, "PURCHASE_CONFLICT", "No se pudo registrar la compra.")
        return existing, False
    return await db.get(Purchase, created_id), True


async def verify(
    db: AsyncSession,
    gateway: PaymentGateway,
    purchase: Purchase,
    payment_id: str,
) -> Payment:
    """Verificación de servidor (IOP #3): consulta al proveedor y aplica el estado."""
    snapshot = await gateway.fetch_payment(payment_id)
    return await apply_snapshot(db, purchase, snapshot)


async def apply_snapshot(
    db: AsyncSession, purchase: Purchase, snapshot: PaymentSnapshot
) -> Payment:
    """Aplica un estado de pago con bloqueo y sin retroceder de rango."""
    locked = await commerce.lock_purchase(db, purchase.id)
    if locked is None:
        raise ApiError(404, "PURCHASE_NOT_FOUND", "La compra no existe.")
    if snapshot.external_reference and snapshot.external_reference != locked.external_reference:
        raise ApiError(409, "PAYMENT_MISMATCH", "El pago no corresponde a esta compra.")
    if locked.amount_cents and snapshot.status == "approved":
        if snapshot.amount_cents < locked.amount_cents or snapshot.currency != locked.currency:
            raise ApiError(409, "PAYMENT_AMOUNT_MISMATCH", "El monto pagado no coincide.")

    payment = await commerce.payment_by_provider_id(
        db, locked.provider, snapshot.provider_payment_id
    )
    if payment is None:
        payment = Payment(
            purchase_id=locked.id,
            provider=locked.provider,
            provider_payment_id=snapshot.provider_payment_id,
            status=snapshot.status,
            status_detail=snapshot.status_detail,
            amount_cents=snapshot.amount_cents,
            currency=snapshot.currency,
        )
        db.add(payment)
    elif PAYMENT_STATUS_RANK[snapshot.status] >= PAYMENT_STATUS_RANK[payment.status]:
        payment.status = snapshot.status
        payment.status_detail = snapshot.status_detail
        payment.amount_cents = snapshot.amount_cents
    # Un webhook antiguo (rank menor) se registra pero no revierte el estado vigente.

    if payment.status == "approved" and locked.status != "paid":
        locked.status = "paid"
        locked.paid_at = datetime.now(UTC)
    elif payment.status in ("refunded", "charged_back"):
        locked.status = "cancelled"
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ApiError(409, "PAYMENT_CONFLICT", "El pago ya se estaba procesando.") from None
    await db.refresh(payment)
    await db.refresh(locked)
    purchase.status = locked.status
    purchase.paid_at = locked.paid_at
    return payment
