"""Proveedor de pagos de laboratorio (``PAYMENT_PROVIDER=fake``).

Aprueba cualquier pago sin cobrar, para poder recorrer el flujo comercial completo
en un portátil. Es, literalmente, una puerta abierta, así que lo que más se prueba
aquí es que **no puede existir fuera de ``APP_ENV=local``**.
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import ApiError
from app.services.payments import (
    LabGateway,
    MercadoPagoGateway,
    UnconfiguredGateway,
    build_gateway,
)

SECRET = "test-only-secret-000000000000000000000"
REMOTE = {
    "app_env": "develop",
    "cors_origins": ["https://frontend.example.com"],
    "frontend_url": "https://frontend.example.com",
    "database_url": "postgresql+asyncpg://u:p@srv.postgres.database.azure.com:5432/db?ssl=require",
    "storage_backend": "azure",
    "azure_storage_connection_string": "UseDevelopmentStorage=true",
    "azure_container_name": "letters",
}


def make_settings(**changes) -> Settings:
    return Settings(_env_file=None, session_secret=SECRET, **changes)


# --- Los cinco casos -------------------------------------------------------------------


async def test_happy_path_approves_the_exact_amount_of_the_purchase():
    """Devuelve el importe de la compra, así que la comprobación de monto sí se ejecuta."""
    settings = make_settings(
        payment_provider="fake", purchase_amount_cents=3_000_000, purchase_currency="COP"
    )
    gateway = build_gateway(settings)

    snapshot = await gateway.fetch_payment("900001")

    assert isinstance(gateway, LabGateway)
    assert snapshot.status == "approved"
    assert snapshot.amount_cents == 3_000_000
    assert snapshot.currency == "COP"
    # Sin referencia externa: el pago simulado se aplica a la compra que se verifica.
    assert snapshot.external_reference is None


async def test_sad_path_an_invalid_payment_identifier_is_still_rejected():
    """Ser de laboratorio no lo hace laxo con la forma de la entrada."""
    gateway = build_gateway(make_settings(payment_provider="fake"))

    for bad in ["abc", "", "9" * 40, "900001; DROP TABLE payments"]:
        with pytest.raises(ApiError) as error:
            await gateway.fetch_payment(bad)
        assert error.value.status_code == 422


def test_edge_the_configuration_refuses_the_lab_provider_outside_local():
    """Frontera del candado: `local` sí, cualquier otro entorno no."""
    assert make_settings(payment_provider="fake").payment_provider == "fake"

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, session_secret=SECRET, payment_provider="fake", **REMOTE)

    assert "APP_ENV=local" in str(error.value)


def test_edge_each_provider_gets_its_own_gateway():
    assert isinstance(build_gateway(make_settings()), UnconfiguredGateway)
    assert isinstance(build_gateway(make_settings(payment_provider="fake")), LabGateway)
    assert isinstance(
        build_gateway(
            make_settings(
                payment_provider="mercadopago",
                mercadopago_access_token="token-de-prueba",
                mercadopago_webhook_secret="secreto-de-prueba",
            )
        ),
        MercadoPagoGateway,
    )


def test_exception_defense_in_depth_blocks_the_lab_gateway_in_a_remote_environment():
    """Segundo candado: aunque alguien esquive el validador, la fábrica no lo construye.

    Se fuerza el estado con `model_construct`, que salta la validación, para simular
    una configuración manipulada o un cambio futuro que se olvide del primer candado.
    """
    tampered = Settings.model_construct(
        app_env="production", payment_provider="fake", purchase_amount_cents=0
    )

    with pytest.raises(RuntimeError, match="APP_ENV=local"):
        build_gateway(tampered)


# --- Recorrido completo con el proveedor de laboratorio ---------------------------------


@pytest.fixture
def lab(app):
    """Sustituye el proveedor de la aplicación por el de laboratorio."""
    app.state.settings.payment_provider = "fake"
    app.state.payments = LabGateway(app.state.settings)
    return app.state.payments


async def test_the_browser_can_complete_a_purchase_without_external_credentials(client, buyer, lab):
    """Es el recorrido que hace el modal del frontend: comprar, simular pago, crear."""
    purchase = (await client.post("/api/v1/purchases", json={"idempotencyKey": "lab-0001"})).json()

    verification = await client.post(
        f"/api/v1/purchases/{purchase['id']}/verify", json={"paymentId": "900001"}
    )

    assert verification.status_code == 200
    body = verification.json()
    assert body["purchase"]["status"] == "paid"
    assert body["payment"]["status"] == "approved"
    assert body["canCreateLetter"] is True

    letter = await client.post(
        "/api/v1/letters",
        json={
            "purchaseId": purchase["id"],
            "title": "Para ti",
            "recipientName": "Ana",
            "recipientEmail": "ana@example.com",
            "body": "Hola",
            "theme": "classic",
            "temp_photos": [],
        },
    )
    assert letter.status_code == 201


async def test_the_webhook_of_the_lab_provider_says_so_in_the_log(lab, caplog):
    """Acepta sin firma; queda registrado para que nadie lo confunda con el real."""
    import logging

    with caplog.at_level(logging.WARNING):
        lab.verify_webhook(b"{}", {})

    assert any("laboratorio" in record.message for record in caplog.records)
