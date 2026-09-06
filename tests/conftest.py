import os
import tempfile
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import text

# Tests must never inherit an arbitrary deployed DATABASE_URL.
test_url = os.environ.get("TEST_DATABASE_URL", "")
parsed = urlsplit(test_url)
if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path != "/zy_auth_validation":
    raise RuntimeError(
        "Use scripts/validate_local.py: dedicated loopback TEST_DATABASE_URL required"
    )
os.environ["DATABASE_URL"] = test_url
os.environ["APP_ENV"] = "local"
os.environ["DB_SSL_MODE"] = "disable"
os.environ["SESSION_SECRET"] = "test-only-secret-000000000000000000000"
os.environ["LOCAL_STORAGE_DIR"] = tempfile.mkdtemp(prefix="zy-storage-")
from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture
def storage_dir(tmp_path_factory):
    return str(tmp_path_factory.mktemp("storage"))


@pytest.fixture
def settings(storage_dir):
    return Settings(
        _env_file=None,
        database_url=test_url,
        session_secret="test-only-secret-000000000000000000000",
        cors_origins=["http://localhost:5173"],
        auth_rate_limit=100,
        local_storage_dir=storage_dir,
        purchase_amount_cents=1500000,
        purchase_currency="COP",
    )


@pytest.fixture
async def app(settings):
    instance = create_app(settings)
    async with instance.router.lifespan_context(instance):
        async with instance.state.engine.begin() as conn:
            # payment_events no referencia a users: se limpia explícitamente.
            await conn.execute(text("TRUNCATE users, payment_events CASCADE"))
        yield instance


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        token = (await client.get("/api/v1/auth/csrf")).json()["csrfToken"]
        client.headers["X-CSRF-Token"] = token
        yield client


# --- Soporte del dominio comercial -------------------------------------------------
from app.core.errors import ApiError  # noqa: E402
from app.services.payments import PaymentGateway, PaymentSnapshot  # noqa: E402


class FakeGateway(PaymentGateway):
    """Proveedor determinista: ninguna prueba toca Mercado Pago ni usa credenciales."""

    configured = True

    def __init__(self):
        self.snapshots: dict[str, PaymentSnapshot] = {}
        self.calls: list[str] = []
        self.accept_signature = True

    def approve(self, payment_id: str, reference: str, amount_cents: int, currency: str = "COP"):
        self.snapshots[payment_id] = PaymentSnapshot(
            provider_payment_id=payment_id,
            status="approved",
            status_detail="accredited",
            amount_cents=amount_cents,
            currency=currency,
            external_reference=reference,
        )

    def set_status(self, payment_id: str, reference: str, status: str, amount_cents: int = 0):
        self.snapshots[payment_id] = PaymentSnapshot(
            provider_payment_id=payment_id,
            status=status,
            status_detail=None,
            amount_cents=amount_cents,
            currency="COP",
            external_reference=reference,
        )

    async def create_preference(self, reference, amount_cents, currency, return_url):
        return f"pref-{reference}", f"https://checkout.test/{reference}"

    async def fetch_payment(self, payment_id: str) -> PaymentSnapshot:
        self.calls.append(payment_id)
        if payment_id not in self.snapshots:
            raise ApiError(404, "PAYMENT_NOT_FOUND", "El pago no existe para esta cuenta.")
        return self.snapshots[payment_id]

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> None:
        if not self.accept_signature or headers.get("x-signature") != "valid":
            raise ApiError(401, "WEBHOOK_SIGNATURE_INVALID", "Firma de webhook inválida.")


@pytest.fixture
def gateway(app):
    fake = FakeGateway()
    app.state.payments = fake
    app.state.settings.payment_provider = "mercadopago"
    return fake


BUYER = {
    "email": "comprador@example.com",
    "password": "Long-test-password-123!",
    "name": "Comprador",
}


@pytest.fixture
async def buyer(client):
    assert (await client.post("/api/v1/auth/register", json=BUYER)).status_code == 201
    response = await client.post(
        "/api/v1/auth/login", json={k: BUYER[k] for k in ("email", "password")}
    )
    assert response.status_code == 200
    return response.json()


async def new_purchase(client, key="idem-key-0001"):
    response = await client.post("/api/v1/purchases", json={"idempotencyKey": key})
    assert response.status_code in (200, 201), response.text
    return response.json()


async def pay(client, gateway, purchase, payment_id="900001"):
    gateway.approve(payment_id, purchase["externalReference"], purchase["amountCents"])
    response = await client.post(
        f"/api/v1/purchases/{purchase['id']}/verify", json={"paymentId": payment_id}
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
async def paid_purchase(client, gateway, buyer):
    purchase = await new_purchase(client)
    await pay(client, gateway, purchase)
    return purchase
