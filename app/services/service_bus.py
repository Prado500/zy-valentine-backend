"""Puerto de publicación en Azure Service Bus (cartas fuera del camino síncrono).

Motivación (IOP #6): insertar la carta dentro de la petición HTTP ata la latencia
del comprador a los 240 IOPS del disco de la B1ms. Aquí la petición solo publica un
mensaje y responde; el `worker.py` hace el INSERT en lotes controlados.

**Degradación elegante.** El transporte es opcional en los dos sentidos:

- Si faltan ``AZURE_SERVICE_BUS_CONNECTION_STRING`` o ``SERVICE_BUS_QUEUE_NAME``,
  ``build_publisher`` devuelve un :class:`MockPublisher` inofensivo y la API sigue
  escribiendo de forma síncrona, exactamente como antes de este cambio.
- Si el paquete ``azure-servicebus`` no está instalado, o la cadena no se puede
  interpretar, tampoco se aborta el arranque: se registra el motivo y se degrada.

Ninguna ruta de este módulo lanza durante el arranque: un App Service que ya está
sirviendo tráfico no puede caer por una variable de entorno nueva.
"""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings

LOG = logging.getLogger("app.service_bus")

# Tope defensivo al leer de la cola. Service Bus ya limita el tamaño del mensaje
# (256 KB en el nivel estándar), pero el worker no debe fiarse de que quien escribió
# en la cola sea esta misma aplicación: un cuerpo desmedido se rechaza antes de
# intentar interpretarlo como JSON.
MAX_MESSAGE_BYTES = 256 * 1024

# Versión del contrato del mensaje. El worker rechaza (dead-letter) lo que no entienda,
# de modo que un despliegue mixto nunca escribe una carta a medias.
LETTER_MESSAGE_TYPE = "letter.create"
LETTER_MESSAGE_VERSION = 1


def build_letter_message(user_id: uuid.UUID, payload, *, auto_publish: bool = True) -> dict:
    """Sobre JSON de una carta pendiente de escribir.

    ``purchaseId`` ya viene validado por la API (existe, es del usuario y está pagada),
    así que el worker no repite la comprobación de propiedad: solo la de unicidad, que
    la garantiza la base con ``letters.purchase_id`` UNIQUE.
    """
    return {
        "type": LETTER_MESSAGE_TYPE,
        "version": LETTER_MESSAGE_VERSION,
        "enqueuedAt": datetime.now(UTC).isoformat(),
        "userId": str(user_id),
        "purchaseId": str(payload.purchaseId),
        "autoPublish": auto_publish,
        "letter": {
            "title": payload.title,
            "recipientName": payload.recipientName,
            "recipientEmail": str(payload.recipientEmail) if payload.recipientEmail else None,
            "body": payload.body,
            "theme": payload.theme,
        },
        # Referencias a las fotos ya subidas al contenedor efímero. Solo viajan claves y
        # nombres: los bytes se quedan en el almacenamiento, el mensaje sigue pequeño.
        "tempPhotos": [
            {
                "tempId": photo.tempId,
                "fileName": photo.fileName,
                "position": index if photo.position is None else photo.position,
            }
            for index, photo in enumerate(payload.temp_photos)
        ],
    }


class LetterPublisher:
    """Puerto. ``enabled`` decide si la API toma el camino asíncrono."""

    enabled = False

    async def publish(self, message: dict, *, message_id: str | None = None) -> bool:
        raise NotImplementedError  # pragma: no cover - puerto

    async def aclose(self) -> None:  # pragma: no cover - puerto
        return None


class MockPublisher(LetterPublisher):
    """Publicador inofensivo: no habla con Azure y no rompe nada.

    Se usa cuando la cola no está configurada. Guarda los mensajes en memoria para que
    las pruebas puedan inspeccionarlos y devuelve ``False``: el llamador entiende que
    el mensaje **no** quedó encolado y debe seguir por el camino síncrono.
    """

    enabled = False

    def __init__(self, reason: str = "cola no configurada"):
        self.reason = reason
        self.published: list[dict] = []

    async def publish(self, message: dict, *, message_id: str | None = None) -> bool:
        self.published.append(message)
        LOG.debug("Service Bus inactivo (%s): la carta se escribe de forma síncrona", self.reason)
        return False


class AzureServiceBusPublisher(LetterPublisher):
    """Cliente asíncrono con un único *sender* reutilizado por todo el proceso.

    El SDK abre una conexión AMQP por *sender*; crear uno por petición gastaría más que
    el INSERT que estamos evitando. El *sender* se crea de forma perezosa y protegido
    por un candado para que varias peticiones concurrentes no abran dos.
    """

    enabled = True

    def __init__(self, connection_string: str, queue_name: str, send_timeout: float = 3.0):
        from azure.servicebus.aio import ServiceBusClient  # noqa: PLC0415 - extra opcional

        self._client = ServiceBusClient.from_connection_string(connection_string)
        self._queue = queue_name
        self._send_timeout = send_timeout
        self._sender = None
        self._lock = asyncio.Lock()

    async def _get_sender(self):
        if self._sender is None:
            async with self._lock:
                if self._sender is None:
                    self._sender = self._client.get_queue_sender(queue_name=self._queue)
        return self._sender

    async def publish(self, message: dict, *, message_id: str | None = None) -> bool:
        """Publica con un tope de tiempo.

        Sin ``wait_for``, un Service Bus que acepta la conexión pero no responde
        dejaría la petición HTTP colgada: exactamente el problema que esta cola
        venía a evitar. Al agotarse el plazo se propaga la excepción y quien llama
        cae al camino síncrono.
        """
        from azure.servicebus import ServiceBusMessage  # noqa: PLC0415 - extra opcional

        sender = await self._get_sender()
        envelope = ServiceBusMessage(
            json.dumps(message, ensure_ascii=False, separators=(",", ":")),
            content_type="application/json",
            # Con la detección de duplicados activada en la cola, un doble clic
            # publica el mismo message_id y Service Bus descarta el segundo.
            message_id=message_id,
            subject=message.get("type"),
        )
        await asyncio.wait_for(sender.send_messages(envelope), timeout=self._send_timeout)
        return True

    async def aclose(self) -> None:
        sender, self._sender = self._sender, None
        if sender is not None:
            try:
                await sender.close()
            except Exception:  # noqa: BLE001 - cerrar nunca debe romper el apagado
                LOG.warning("No se pudo cerrar el sender de Service Bus", exc_info=False)
        try:
            await self._client.close()
        except Exception:  # noqa: BLE001 - idem
            LOG.warning("No se pudo cerrar el cliente de Service Bus", exc_info=False)


def build_publisher(settings: Settings) -> LetterPublisher:
    """Devuelve el publicador real o el inofensivo. **Nunca lanza.**"""
    if not settings.service_bus_enabled:
        return MockPublisher("AZURE_SERVICE_BUS_CONNECTION_STRING/SERVICE_BUS_QUEUE_NAME ausentes")
    try:
        publisher = AzureServiceBusPublisher(
            settings.azure_service_bus_connection_string.get_secret_value(),
            settings.service_bus_queue_name,
            settings.service_bus_send_timeout,
        )
    except ImportError:
        LOG.warning(
            "azure-servicebus no está instalado: la API escribe las cartas de forma "
            "síncrona. Instala el extra: pip install '.[azure]'"
        )
        return MockPublisher("paquete azure-servicebus ausente")
    except Exception as error:  # noqa: BLE001 - una cadena inválida degrada, no tumba
        LOG.warning(
            "Service Bus no disponible (%s): se escribe de forma síncrona", type(error).__name__
        )
        return MockPublisher(f"cliente no construible ({type(error).__name__})")
    LOG.info("Service Bus activo sobre la cola configurada")
    return publisher


def message_payload(raw: str | bytes | Any) -> dict:
    """Normaliza y valida el cuerpo del mensaje del SDK.

    El SDK entrega ``bytes``, ``str`` o un generador de trozos según la versión y el
    modo de recepción. Se acepta cualquiera de los tres, se limita el tamaño y se
    exige que el resultado sea un objeto JSON: lo que venga de la cola es una
    entrada más, no un dato de confianza.
    """
    if isinstance(raw, bytes | bytearray):
        data = bytes(raw)
    elif isinstance(raw, str):
        data = raw.encode("utf-8")
    else:
        data = b"".join(bytes(part) for part in raw)
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError("El mensaje supera el tamaño máximo admitido")
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("El mensaje no es un objeto JSON")
    return payload
