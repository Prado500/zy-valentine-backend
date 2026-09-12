"""Publicación en Azure Service Bus y degradación elegante.

Rule of 10 sobre ``publish``: es lo que decide si la petición del comprador dura
milisegundos o arrastra la latencia de una infraestructura remota. No se abre
ninguna conexión: el SDK se sustituye por un doble, así que estas pruebas corren en
cualquier máquina y en el pipeline.
"""

import asyncio
import json
import uuid

import pytest

from app.core.config import Settings
from app.schemas.commerce import LetterCreate, TempPhotoRef
from app.services.service_bus import (
    LETTER_MESSAGE_TYPE,
    MAX_MESSAGE_BYTES,
    AzureServiceBusPublisher,
    MockPublisher,
    build_letter_message,
    build_publisher,
    message_payload,
)

CONNECTION = (
    "Endpoint=sb://ejemplo.servicebus.windows.net/;SharedAccessKeyName=send;"
    "SharedAccessKey=" + "A" * 43 + "="
)
SECRET = "test-only-secret-000000000000000000000"


def make_settings(**changes) -> Settings:
    return Settings(_env_file=None, session_secret=SECRET, **changes)


def make_payload(**changes) -> LetterCreate:
    return LetterCreate(
        purchaseId=changes.pop("purchaseId", uuid.uuid4()),
        title="Para ti",
        recipientName="Ana",
        recipientEmail="ana@example.com",
        body="Hola",
        theme="classic",
        **changes,
    )


class FakeSender:
    """Doble del *sender* del SDK. Registra lo enviado y puede tardar o fallar."""

    def __init__(self, delay: float = 0.0, error: Exception | None = None):
        self.delay = delay
        self.error = error
        self.sent: list[object] = []
        self.closed = False

    async def send_messages(self, message):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        self.sent.append(message)

    async def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, sender: FakeSender):
        self.sender = sender
        self.senders_created = 0
        self.closed = False

    def get_queue_sender(self, queue_name: str):
        self.senders_created += 1
        self.queue_name = queue_name
        return self.sender

    async def close(self):
        self.closed = True


@pytest.fixture
def publisher(mocker):
    """`AzureServiceBusPublisher` real, con el cliente del SDK sustituido."""
    pytest.importorskip("azure.servicebus")
    sender = FakeSender()
    client = FakeClient(sender)
    mocker.patch(
        "azure.servicebus.aio.ServiceBusClient.from_connection_string", return_value=client
    )
    return AzureServiceBusPublisher(CONNECTION, "cartas", send_timeout=1.0), client, sender


# --- publish: los cinco casos ------------------------------------------------------------


async def test_happy_path_publishes_a_json_envelope(publisher):
    bus, client, sender = publisher
    message = build_letter_message(uuid.uuid4(), make_payload())

    assert await bus.publish(message, message_id="letter-1") is True

    assert client.queue_name == "cartas"
    envelope = sender.sent[0]
    assert envelope.content_type == "application/json"
    assert envelope.message_id == "letter-1"
    assert envelope.subject == LETTER_MESSAGE_TYPE
    assert message_payload(envelope.body) == message


async def test_sad_path_without_configuration_nothing_is_published(caplog):
    """Sin cadena ni cola no hay transporte: se devuelve False, no una excepción."""
    publisher = build_publisher(make_settings())

    assert isinstance(publisher, MockPublisher)
    assert publisher.enabled is False
    assert await publisher.publish({"type": "letter.create"}) is False


async def test_edge_oversized_message_is_rejected_before_parsing():
    """Tope defensivo: lo que sale de la cola es una entrada, no un dato de confianza."""
    exact = json.dumps({"relleno": "x" * (MAX_MESSAGE_BYTES - 20)}).encode()
    assert len(exact) <= MAX_MESSAGE_BYTES
    assert message_payload(exact)["relleno"].startswith("x")

    with pytest.raises(ValueError, match="tamaño máximo"):
        message_payload(b"x" * (MAX_MESSAGE_BYTES + 1))


async def test_edge_concurrent_publishes_share_a_single_sender(publisher):
    """Diez publicaciones simultáneas abren **una** conexión AMQP, no diez.

    El candado del publicador es lo que lo garantiza; sin él, un pico de tráfico
    abriría un *sender* por petición y gastaría más que el INSERT que se evita.
    """
    bus, client, sender = publisher

    results = await asyncio.gather(
        *(bus.publish(build_letter_message(uuid.uuid4(), make_payload())) for _ in range(10))
    )

    assert results == [True] * 10
    assert client.senders_created == 1
    assert len(sender.sent) == 10


async def test_exception_a_hung_bus_times_out_instead_of_hanging_the_request(mocker):
    """Si el bus acepta la conexión pero no responde, se corta por tiempo.

    Es el caso peligroso: sin plazo, la petición del comprador quedaría colgada por
    la misma infraestructura que se introdujo para responder rápido.
    """
    pytest.importorskip("azure.servicebus")
    sender = FakeSender(delay=5.0)
    mocker.patch(
        "azure.servicebus.aio.ServiceBusClient.from_connection_string",
        return_value=FakeClient(sender),
    )
    bus = AzureServiceBusPublisher(CONNECTION, "cartas", send_timeout=0.05)

    with pytest.raises(asyncio.TimeoutError):
        await bus.publish(build_letter_message(uuid.uuid4(), make_payload()))
    assert sender.sent == []


async def test_exception_transport_failure_propagates_to_the_caller(mocker):
    pytest.importorskip("azure.servicebus")
    sender = FakeSender(error=RuntimeError("AMQP caído"))
    mocker.patch(
        "azure.servicebus.aio.ServiceBusClient.from_connection_string",
        return_value=FakeClient(sender),
    )
    bus = AzureServiceBusPublisher(CONNECTION, "cartas")

    with pytest.raises(RuntimeError):
        await bus.publish(build_letter_message(uuid.uuid4(), make_payload()))


# --- Construcción del publicador -----------------------------------------------------------


def test_a_broken_connection_string_degrades_instead_of_breaking_startup():
    """Una cadena inválida no puede tumbar un App Service que ya sirve tráfico."""
    publisher = build_publisher(
        make_settings(
            azure_service_bus_connection_string="esto-no-es-una-cadena",
            service_bus_queue_name="cartas",
        )
    )

    assert isinstance(publisher, MockPublisher)


def test_a_missing_sdk_degrades_instead_of_breaking_startup(mocker):
    mocker.patch(
        "azure.servicebus.aio.ServiceBusClient.from_connection_string",
        side_effect=ImportError("azure-servicebus no instalado"),
    )
    publisher = build_publisher(
        make_settings(
            azure_service_bus_connection_string=CONNECTION,
            service_bus_queue_name="cartas",
        )
    )

    assert isinstance(publisher, MockPublisher)


async def test_closing_is_tolerant_to_a_broken_connection(publisher, mocker):
    """Cerrar nunca puede romper el apagado: la conexión ya podía estar rota."""
    bus, client, sender = publisher
    await bus.publish(build_letter_message(uuid.uuid4(), make_payload()))
    mocker.patch.object(sender, "close", side_effect=RuntimeError("conexión ya rota"))

    await bus.aclose()

    assert client.closed is True


# --- Sobre del mensaje ----------------------------------------------------------------------


def test_the_envelope_carries_the_photos_by_reference_not_by_value():
    """Solo viajan claves y nombres: los bytes se quedan en el almacenamiento."""
    temp_id = f"temporal/{uuid.uuid4()}/{uuid.uuid4().hex}.png"
    payload = make_payload(temp_photos=[TempPhotoRef(tempId=temp_id, fileName="mi foto.png")])

    message = build_letter_message(uuid.uuid4(), payload)

    assert message["tempPhotos"] == [{"tempId": temp_id, "fileName": "mi foto.png", "position": 0}]
    assert len(json.dumps(message)) < 1024


@pytest.mark.parametrize("body", [b"{no es json}", b"[]", b'"texto"', b"", b"\xff\xfe"])
def test_a_malformed_body_is_rejected(body):
    with pytest.raises(ValueError):
        message_payload(body)


def test_the_body_is_accepted_as_bytes_str_or_chunks():
    """El SDK entrega el cuerpo de tres formas según la versión y el modo de recepción."""
    message = {"type": "letter.create", "version": 1}
    raw = json.dumps(message).encode()

    assert message_payload(raw) == message
    assert message_payload(raw.decode()) == message
    assert message_payload(iter([raw[:5], raw[5:]])) == message
