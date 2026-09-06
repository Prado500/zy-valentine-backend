import os
import tempfile
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import text

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# URL de relleno para construir objetos Settings en pruebas que nunca abren conexión.
PLACEHOLDER_URL = "postgresql+asyncpg://postgres@127.0.0.1:5432/zy_auth_validation"
NO_DATABASE = (
    "TEST_DATABASE_URL no está definida: se omiten las pruebas que necesitan PostgreSQL. "
    "Para ejecutarlas, levanta una base desechable y expórtala:\n"
    "  docker run -d --name zy-test-db -e POSTGRES_PASSWORD=local \\\n"
    "    -e POSTGRES_DB=zy_auth_validation -p 127.0.0.1:5432:5432 postgres:15-alpine\n"
    "  export TEST_DATABASE_URL="
    "postgresql+asyncpg://postgres:local@127.0.0.1:5432/zy_auth_validation\n"
    "  python -m alembic upgrade head && python -m pytest"
)


def checked_test_url() -> str:
    """Devuelve la URL de pruebas, o cadena vacía si no hay ninguna configurada.

    La suite ejecuta ``TRUNCATE`` entre pruebas, así que la base **nunca** puede ser
    una real. Por eso:

    - Sin la variable no se falla: las pruebas que no tocan la base siguen corriendo y
      las de integración se omiten con un motivo explícito. Así un pipeline sin
      PostgreSQL no queda bloqueado.
    - Con la variable definida pero apuntando a algo que no es una base desechable en
      loopback, se aborta. Preferimos un error ruidoso a borrar datos ajenos: una URL
      de Azure heredada del entorno jamás debe llegar hasta aquí.
    """
    raw = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    database = parsed.path.lstrip("/")
    if (
        not parsed.scheme.startswith("postgresql")
        or parsed.hostname not in LOOPBACK_HOSTS
        or not (database == "zy_auth_validation" or "test" in database)
    ):
        raise RuntimeError(
            "TEST_DATABASE_URL debe ser una base PostgreSQL desechable en loopback, "
            "llamada 'zy_auth_validation' o con 'test' en el nombre. "
            "La suite ejecuta TRUNCATE y nunca debe apuntar a una base real."
        )
    return raw


test_url = checked_test_url()
# Settings exige una URL válida al importar app.main; sin base de pruebas se usa una de
# relleno que nadie llega a abrir, para que las pruebas sin base de datos sí se ejecuten.
os.environ["DATABASE_URL"] = test_url or PLACEHOLDER_URL
os.environ["APP_ENV"] = "local"
os.environ["DB_SSL_MODE"] = "disable"
os.environ["SESSION_SECRET"] = "test-only-secret-000000000000000000000"
os.environ["LOCAL_STORAGE_DIR"] = tempfile.mkdtemp(prefix="zy-storage-")
from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

# Variables que este conftest controla a propósito; el resto no debe filtrarse.
CONTROLLED_ENV = {"DATABASE_URL", "APP_ENV", "DB_SSL_MODE", "SESSION_SECRET", "LOCAL_STORAGE_DIR"}


@pytest.fixture(autouse=True)
def isolated_settings_env(monkeypatch):
    """Aísla las pruebas del entorno del runner.

    `Settings` lee variables de entorno, así que un pipeline que exporte
    FRONTEND_URL o AZURE_STORAGE_CONNECTION_STRING cambiaría el resultado de las
    pruebas de configuración. Se limpian todas las que correspondan a un campo de
    `Settings`, salvo las que este archivo define deliberadamente.
    """
    for field in Settings.model_fields:
        name = field.upper()
        if name not in CONTROLLED_ENV:
            monkeypatch.delenv(name, raising=False)


def pytest_report_header():
    """Deja claro en el log del pipeline si las pruebas de integración se ejecutaron."""
    if test_url:
        return f"PostgreSQL de pruebas: {urlsplit(test_url).path.lstrip('/')} en loopback"
    return "PostgreSQL de pruebas: NO configurado; se omiten las pruebas de integración"


@pytest.fixture
def storage_dir(tmp_path_factory):
    return str(tmp_path_factory.mktemp("storage"))


@pytest.fixture
def settings(storage_dir):
    return Settings(
        _env_file=None,
        database_url=test_url or PLACEHOLDER_URL,
        session_secret="test-only-secret-000000000000000000000",
        cors_origins=["http://localhost:5173"],
        auth_rate_limit=100,
        local_storage_dir=storage_dir,
        purchase_amount_cents=1500000,
        purchase_currency="COP",
    )


@pytest.fixture
async def app(settings):
    if not test_url:
        pytest.skip(NO_DATABASE)
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
