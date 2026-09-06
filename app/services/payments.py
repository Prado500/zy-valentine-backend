"""Puerto de pagos. La verificación ocurre en el servidor, nunca en el navegador.

El retorno del navegador (IOP #3) solo aporta un identificador; el estado real se
consulta contra la API del proveedor o llega por webhook firmado. Ambos caminos
terminan en la misma verificación de servidor.
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass

import httpx

from app.core.config import Settings
from app.core.errors import ApiError
from app.models.commerce import PAYMENT_STATUSES

LOG = logging.getLogger("app.payments")


@dataclass(frozen=True)
class PaymentSnapshot:
    """Estado del pago tal como lo reporta el proveedor, ya normalizado."""

    provider_payment_id: str
    status: str
    status_detail: str | None
    amount_cents: int
    currency: str
    external_reference: str | None


class PaymentGateway:
    """Interfaz mínima; las pruebas inyectan una implementación determinista."""

    configured = False

    async def fetch_payment(self, payment_id: str) -> PaymentSnapshot:  # pragma: no cover - puerto
        raise NotImplementedError

    async def create_preference(
        self, reference: str, amount_cents: int, currency: str, return_url: str
    ) -> tuple[str | None, str | None]:
        """Devuelve (preference_id, checkout_url); sin proveedor devuelve (None, None)."""
        return None, None

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> None:  # pragma: no cover
        raise NotImplementedError

    async def aclose(self) -> None:  # pragma: no cover - puerto
        return None


class UnconfiguredGateway(PaymentGateway):
    """Sin proveedor configurado la API responde 503 explícito, nunca 'pagado'."""

    configured = False

    async def fetch_payment(self, payment_id: str) -> PaymentSnapshot:
        raise ApiError(503, "PAYMENTS_NOT_CONFIGURED", "El proveedor de pagos no está configurado.")

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> None:
        raise ApiError(503, "PAYMENTS_NOT_CONFIGURED", "El proveedor de pagos no está configurado.")


class MercadoPagoGateway(PaymentGateway):
    configured = True

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.mercadopago_api_base,
            timeout=httpx.Timeout(8.0),
            headers={
                "Authorization": f"Bearer {settings.mercadopago_access_token.get_secret_value()}"
            },
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def fetch_payment(self, payment_id: str) -> PaymentSnapshot:
        if not payment_id.isdigit() or len(payment_id) > 32:
            raise ApiError(422, "INVALID_PAYMENT_ID", "Identificador de pago inválido.")
        try:
            response = await self.client.get(f"/v1/payments/{payment_id}")
        except httpx.HTTPError:
            raise ApiError(
                503, "PAYMENTS_UNAVAILABLE", "El proveedor de pagos no respondió; reintenta."
            ) from None
        if response.status_code == 404:
            raise ApiError(404, "PAYMENT_NOT_FOUND", "El pago no existe para esta cuenta.")
        if response.status_code >= 400:
            raise ApiError(
                503, "PAYMENTS_UNAVAILABLE", "El proveedor de pagos no respondió; reintenta."
            )
        return normalize_payment(response.json())

    async def create_preference(
        self, reference: str, amount_cents: int, currency: str, return_url: str
    ) -> tuple[str | None, str | None]:
        body = {
            "external_reference": reference,
            "items": [
                {
                    "title": "Carta Zyvencore",
                    "quantity": 1,
                    "currency_id": currency,
                    "unit_price": amount_cents / 100,
                }
            ],
            "back_urls": {"success": return_url, "pending": return_url, "failure": return_url},
            "auto_return": "approved",
        }
        try:
            response = await self.client.post("/checkout/preferences", json=body)
        except httpx.HTTPError:
            raise ApiError(
                503, "PAYMENTS_UNAVAILABLE", "El proveedor de pagos no respondió; reintenta."
            ) from None
        if response.status_code >= 400:
            raise ApiError(
                503, "PAYMENTS_UNAVAILABLE", "El proveedor de pagos no respondió; reintenta."
            )
        data = response.json()
        return (
            str(data.get("id"))[:128] if data.get("id") else None,
            str(data.get("init_point"))[:512] if data.get("init_point") else None,
        )

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> None:
        """Valida la firma HMAC del webhook. Sin firma válida no se procesa nada."""
        secret = self.settings.mercadopago_webhook_secret.get_secret_value().encode()
        signature = headers.get("x-signature", "")
        request_id = headers.get("x-request-id", "")
        parts = dict(piece.strip().split("=", 1) for piece in signature.split(",") if "=" in piece)
        timestamp, received = parts.get("ts", ""), parts.get("v1", "")
        data_id = headers.get("x-data-id", "")
        if not timestamp or not received:
            raise ApiError(401, "WEBHOOK_SIGNATURE_INVALID", "Firma de webhook ausente.")
        manifest = f"id:{data_id};request-id:{request_id};ts:{timestamp};"
        expected = hmac.new(secret, manifest.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            raise ApiError(401, "WEBHOOK_SIGNATURE_INVALID", "Firma de webhook inválida.")


def normalize_payment(payload: dict) -> PaymentSnapshot:
    """Traduce la respuesta del proveedor a un estado interno conocido."""
    status = str(payload.get("status") or "pending")
    if status not in PAYMENT_STATUSES:
        status = "pending"
    amount = payload.get("transaction_amount") or 0
    return PaymentSnapshot(
        provider_payment_id=str(payload.get("id") or ""),
        status=status,
        status_detail=(
            str(payload["status_detail"])[:64] if payload.get("status_detail") else None
        ),
        amount_cents=int(round(float(amount) * 100)),
        currency=str(payload.get("currency_id") or "COP")[:3],
        external_reference=(
            str(payload["external_reference"])[:64] if payload.get("external_reference") else None
        ),
    )


class LabGateway(PaymentGateway):
    """Proveedor de laboratorio: aprueba cualquier pago **sin cobrar nada**.

    Existe para poder recorrer el flujo comercial completo en un portátil, sin
    credenciales de Mercado Pago: el frontend manda un ``paymentId`` cualquiera y
    la compra queda pagada. Es, literalmente, una puerta abierta.

    Por eso está acotado por dos candados independientes:

    - ``Settings.validate_runtime`` rechaza el arranque si ``PAYMENT_PROVIDER=fake``
      con ``APP_ENV`` distinto de ``local``.
    - :func:`build_gateway` vuelve a comprobarlo antes de instanciarlo, para que un
      cambio futuro en la configuración no lo cuele en un entorno remoto.

    El importe declarado es exactamente el de la compra, así que la verificación de
    monto de ``purchases.apply_snapshot`` sigue ejecutándose de verdad en vez de
    saltarse; y no se declara ``external_reference``, para que el pago simulado se
    aplique a la compra que se está verificando.
    """

    configured = True

    def __init__(self, settings: Settings):
        self.settings = settings

    async def fetch_payment(self, payment_id: str) -> PaymentSnapshot:
        if not payment_id.isdigit() or len(payment_id) > 32:
            raise ApiError(422, "INVALID_PAYMENT_ID", "Identificador de pago inválido.")
        return PaymentSnapshot(
            provider_payment_id=payment_id,
            status="approved",
            status_detail="accredited",
            amount_cents=self.settings.purchase_amount_cents,
            currency=self.settings.purchase_currency,
            external_reference=None,
        )

    async def create_preference(
        self, reference: str, amount_cents: int, currency: str, return_url: str
    ) -> tuple[str | None, str | None]:
        # Sin checkout externo: el frontend se queda en su botón de simulación.
        return f"lab-{reference}", None

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> None:
        """Acepta el webhook sin firma. Solo alcanzable con APP_ENV=local."""
        LOG.warning("Webhook aceptado sin firma: proveedor de laboratorio (APP_ENV=local)")


def build_gateway(settings: Settings) -> PaymentGateway:
    if settings.payment_provider == "mercadopago":
        return MercadoPagoGateway(settings)
    if settings.payment_provider == "fake":
        # Defensa en profundidad: el validador de Settings ya lo impide, pero este
        # objeto aprueba pagos y no puede depender de una sola comprobación.
        if settings.app_env != "local":
            raise RuntimeError("PAYMENT_PROVIDER=fake solo se admite con APP_ENV=local")
        LOG.warning(
            "Proveedor de pagos de LABORATORIO activo: cualquier pago se aprueba sin cobrar"
        )
        return LabGateway(settings)
    return UnconfiguredGateway()
