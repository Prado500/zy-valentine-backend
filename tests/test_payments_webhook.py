"""Validación de la firma HMAC del webhook de Mercado Pago.

Rule of 10 sobre ``MercadoPagoGateway.verify_webhook``. Es la puerta por la que
Mercado Pago marca una compra como pagada sin que intervenga el navegador del
comprador, así que una firma mal validada tiene dos caras igual de malas: si
rechaza lo legítimo se pierden pagos en silencio, y si acepta lo ajeno cualquiera
puede declarar pagada una compra.

Estas pruebas no abren red: ``verify_webhook`` es síncrona y solo usa el secreto de
la configuración. El cliente HTTP se cierra en el *fixture* para no dejar sockets.

**El manifiesto lo construye el ayudante de abajo a partir de la especificación
publicada por Mercado Pago, no copiando la implementación.** Es lo que da valor a la
prueba: si el código se desvía de la especificación, esto se pone en rojo. La
plantilla es ``id:<data.id>;request-id:<x-request-id>;ts:<ts>;``, donde ``data.id``
es **el parámetro de la query de la URL de notificación**, nunca una cabecera.
"""

import hashlib
import hmac

import pytest

from app.core.config import Settings
from app.core.errors import ApiError
from app.services.payments import MercadoPagoGateway

SECRET = "test-only-secret-000000000000000000000"
WEBHOOK_SECRET = "clave-de-webhook-solo-para-pruebas"
REQUEST_ID = "b2e4f1a0-0000-4000-8000-000000000000"
TIMESTAMP = "1704908010"
DATA_ID = "44399992610"


def _firma(data_id: str, *, secret: str = WEBHOOK_SECRET, request_id: str = REQUEST_ID) -> str:
    """Firma una notificación como la firma Mercado Pago."""
    manifiesto = f"id:{data_id.lower()};request-id:{request_id};ts:{TIMESTAMP};"
    v1 = hmac.new(secret.encode(), manifiesto.encode(), hashlib.sha256).hexdigest()
    return f"ts={TIMESTAMP},v1={v1}"


def _cabeceras(firma: str, **extra: str) -> dict[str, str]:
    """Cabeceras en minúsculas, tal como las normaliza el router."""
    return {"x-signature": firma, "x-request-id": REQUEST_ID, **extra}


@pytest.fixture
async def gateway():
    settings = Settings(
        _env_file=None,
        session_secret=SECRET,
        payment_provider="mercadopago",
        mercadopago_access_token="APP_USR-solo-para-pruebas",
        mercadopago_webhook_secret=WEBHOOK_SECRET,
    )
    puerta = MercadoPagoGateway(settings)
    yield puerta
    await puerta.aclose()


# --- 1. Camino feliz -----------------------------------------------------------------


async def test_una_firma_valida_se_acepta(gateway):
    """El `data.id` llega por la query, y con él la firma cuadra."""
    gateway.verify_webhook(b"{}", _cabeceras(_firma(DATA_ID)), DATA_ID)


# --- 2. Camino triste ----------------------------------------------------------------


async def test_sin_cabecera_de_firma_se_rechaza(gateway):
    with pytest.raises(ApiError) as error:
        gateway.verify_webhook(b"{}", {"x-request-id": REQUEST_ID}, DATA_ID)
    assert error.value.status_code == 401


# --- 3. Regresión: el `data.id` tiene que participar de verdad -----------------------


async def test_una_firma_de_otro_data_id_se_rechaza(gateway):
    """La prueba que fija el fallo original.

    Antes, el identificador se leía de una cabecera ``x-data-id`` que Mercado Pago
    no envía nunca, así que siempre valía cadena vacía: la firma daba igual el
    ``data.id`` que trajera la notificación. Con esto en rojo, ese fallo no vuelve.
    """
    firma_de_otro_pago = _firma("99999999999")
    with pytest.raises(ApiError) as error:
        gateway.verify_webhook(b"{}", _cabeceras(firma_de_otro_pago), DATA_ID)
    assert error.value.status_code == 401


async def test_una_cabecera_x_data_id_no_se_tiene_en_cuenta(gateway):
    """Aunque alguien mande `x-data-id`, lo que firma es el valor de la query.

    Mercado Pago no envía esa cabecera. Si volviera a leerse, un tercero podría
    elegir qué identificador se firma y la validación dejaría de proteger nada.
    """
    gateway.verify_webhook(
        b"{}",
        _cabeceras(_firma(DATA_ID), **{"x-data-id": "99999999999"}),
        DATA_ID,
    )


# --- 4. Casos límite -----------------------------------------------------------------


async def test_un_data_id_alfanumerico_se_firma_en_minusculas(gateway):
    """Lo exige la especificación de Mercado Pago cuando el id no es numérico.

    Para un id de pago —siempre numérico— la conversión no cambia nada, así que
    esta prueba es la única que ejercita esa regla.
    """
    identificador = "AbC123XyZ"
    gateway.verify_webhook(b"{}", _cabeceras(_firma(identificador)), identificador)


async def test_sin_data_id_se_rechaza(gateway):
    """El router entrega ``None`` cuando la query no trae ``data.id``.

    No se acepta a la ligera: sin identificador el manifiesto queda incompleto y la
    comparación debe fallar, nunca dar por buena la notificación.
    """
    with pytest.raises(ApiError) as error:
        gateway.verify_webhook(b"{}", _cabeceras(_firma(DATA_ID)), None)
    assert error.value.status_code == 401


# --- 5. Manipulación y entradas rotas ------------------------------------------------


async def test_una_firma_hecha_con_otro_secreto_se_rechaza(gateway):
    """Quien no tenga el secreto de la aplicación no puede declarar un pago."""
    with pytest.raises(ApiError) as error:
        gateway.verify_webhook(
            b"{}", _cabeceras(_firma(DATA_ID, secret="secreto-que-no-es")), DATA_ID
        )
    assert error.value.status_code == 401


@pytest.mark.parametrize(
    "firma",
    [
        "",
        "basura-sin-signo-igual",
        f"ts={TIMESTAMP}",  # falta v1
        "v1=abc123",  # falta ts
        f"ts=,v1=,extra={TIMESTAMP}",
    ],
    ids=["vacia", "sin-igual", "sin-v1", "sin-ts", "partes-vacias"],
)
async def test_una_cabecera_de_firma_malformada_se_rechaza(gateway, firma):
    """Lo que manda un tercero por la red es una entrada, no un dato de confianza."""
    with pytest.raises(ApiError) as error:
        gateway.verify_webhook(b"{}", _cabeceras(firma), DATA_ID)
    assert error.value.status_code == 401


async def test_un_request_id_distinto_invalida_la_firma(gateway):
    """El `x-request-id` también entra en el manifiesto: no se puede reutilizar."""
    with pytest.raises(ApiError) as error:
        gateway.verify_webhook(
            b"{}",
            {"x-signature": _firma(DATA_ID), "x-request-id": "otro-request-id"},
            DATA_ID,
        )
    assert error.value.status_code == 401
