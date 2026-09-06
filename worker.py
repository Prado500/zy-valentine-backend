#!/usr/bin/env python
"""Consumidor de la cola de cartas (Azure Service Bus).

Corre como proceso aparte, en el mismo servidor que la API, y es quien realmente
escribe en PostgreSQL. Existe para proteger la instancia B1ms (2048 MB, 240 IOPS,
20 conexiones): la API publica y responde, y el ritmo de escritura lo marca aquí un
lote acotado, no el pico de tráfico del frontend.

Presupuesto respetado:

- **Lotes de 12 mensajes como máximo** (``SERVICE_BUS_MAX_BATCH``). Cada lote son 12
  escrituras, el tope acordado para los 240 IOPS del disco.
- **Pool dedicado de 5 conexiones** (``WORKER_DB_POOL_SIZE``), de las 20 del servidor;
  las otras 15 quedan para la API. La concurrencia dentro del lote está limitada por
  ese mismo número, así que el worker nunca pide más conexiones de las que tiene.

Tolerancia a fallos: un mensaje que falla no tumba el bucle. Los errores permanentes
(JSON inválido, compra inexistente o sin pagar, usuario dado de baja) van directos a la
*dead-letter queue*; los transitorios (base caída, timeout) se abandonan para que
Service Bus los reentregue, y solo tras ``SERVICE_BUS_MAX_ATTEMPTS`` entregas pasan a
la DLQ. Nada se pierde en silencio.

Uso::

    python worker.py

Sin ``AZURE_SERVICE_BUS_CONNECTION_STRING`` y ``SERVICE_BUS_QUEUE_NAME`` el proceso
informa y termina con éxito: la API sigue funcionando en modo síncrono y el despliegue
no entra en un ciclo de reinicios.
"""

import asyncio
import contextlib
import logging
import signal
import sys
from dataclasses import dataclass
from typing import Any

from app.core.config import ConfigurationError, Settings, get_settings
from app.core.errors import ApiError
from app.db.database import create_worker_engine, session_factory
from app.services.commerce import CommerceService
from app.services.mailer import build_mailer
from app.services.payments import build_gateway
from app.services.service_bus import MockPublisher, message_payload
from app.services.storage import build_storage

LOG = logging.getLogger("app.worker")

# Errores de negocio que jamás mejoran con un reintento: mensaje mal formado, compra
# inexistente o sin pagar, comprador inactivo. Reintentarlos solo gastaría IOPS, así
# que van directos a la dead-letter queue. Cualquier otro estado —o cualquier
# excepción no controlada— se trata como transitorio y se reintenta.
PERMANENT_STATUS = (400, 403, 404, 409, 410, 422)


@dataclass(frozen=True, slots=True)
class WorkerDeps:
    """Lo que el worker necesita para atender un mensaje, montado una sola vez.

    El servicio se construye por mensaje porque lleva la sesión de esa unidad de
    trabajo; los puertos (almacenamiento, correo) son compartidos y sin estado por
    petición, así que se crean al arrancar.
    """

    settings: Settings
    sessions: Any
    storage: Any
    mailer: Any

    def service(self, db) -> CommerceService:
        return CommerceService(
            db=db,
            settings=self.settings,
            storage=self.storage,
            mailer=self.mailer,
            payments=build_gateway(self.settings),
            # El worker consume la cola; no publica en ella.
            queue=MockPublisher("el worker no publica"),
        )


# --- Ciclo de vida de un mensaje ------------------------------------------------------


async def handle_message(receiver, message, deps: "WorkerDeps", limiter) -> None:
    """Procesa un mensaje y lo liquida (complete / abandon / dead-letter).

    Toda la lógica de negocio vive en ``CommerceService.fulfil_queued_letter``, la
    misma que usa la API en su camino síncrono. Aquí solo queda lo que es propio de
    una cola: interpretar el cuerpo, decidir entre reintento y dead-letter, y
    liquidar el mensaje.

    El semáforo acota cuántos mensajes del lote tocan la base a la vez: nunca más que
    el tamaño del pool dedicado, para no encolar esperas dentro de SQLAlchemy.
    """
    try:
        data = message_payload(message.body)
    except Exception as error:  # noqa: BLE001 - cuerpo ilegible: no hay reintento posible
        await _dead_letter(receiver, message, "INVALID_JSON", type(error).__name__)
        return

    try:
        async with limiter, deps.sessions() as db:
            letter_id = await deps.service(db).fulfil_queued_letter(data)
    except ApiError as error:
        detail = error.detail if isinstance(error.detail, dict) else {}
        code = detail.get("code", "BUSINESS_ERROR")
        if error.status_code in PERMANENT_STATUS:
            await _dead_letter(receiver, message, code, detail.get("message", ""))
        else:
            await _retry(receiver, message, deps.settings, code)
        return
    except Exception as error:  # noqa: BLE001 - transitorio: base, red o correo
        await _retry(receiver, message, deps.settings, type(error).__name__)
        return

    try:
        await receiver.complete_message(message)
    except Exception:  # noqa: BLE001 - el lock expiró; Service Bus reentregará
        LOG.warning("Carta %s escrita pero el mensaje no se pudo completar", letter_id)


async def _dead_letter(receiver, message, reason: str, description: str) -> None:
    LOG.error("Mensaje a dead-letter (%s): %s", reason, description)
    with contextlib.suppress(Exception):
        await receiver.dead_letter_message(
            message, reason=reason[:200], error_description=(description or reason)[:4000]
        )


async def _retry(receiver, message, settings: Settings, reason: str) -> None:
    """Abandona el mensaje para que Service Bus lo reentregue, con tope de intentos."""
    attempts = (getattr(message, "delivery_count", 0) or 0) + 1
    if attempts >= settings.service_bus_max_attempts:
        await _dead_letter(receiver, message, "MAX_ATTEMPTS", f"{reason} tras {attempts} entregas")
        return
    LOG.warning("Fallo transitorio (%s); entrega %s, se reintentará", reason, attempts)
    with contextlib.suppress(Exception):
        await receiver.abandon_message(message)


# --- Bucle principal ------------------------------------------------------------------


async def consume(settings: Settings, stop: asyncio.Event) -> None:
    """Una sesión completa contra la cola. Al salir devuelve todas las conexiones."""
    from azure.servicebus.aio import ServiceBusClient  # noqa: PLC0415 - extra opcional

    engine = create_worker_engine(settings)
    deps = WorkerDeps(
        settings=settings,
        sessions=session_factory(engine),
        # Mismo puerto que usa la API: Azure Blob fuera de local, disco en desarrollo.
        storage=build_storage(settings),
        mailer=build_mailer(settings),
    )
    # Nunca más operaciones simultáneas que conexiones en el pool dedicado.
    limiter = asyncio.Semaphore(settings.worker_db_pool_size)
    batch_size = min(settings.service_bus_max_batch, 12)

    try:
        async with ServiceBusClient.from_connection_string(
            settings.azure_service_bus_connection_string.get_secret_value()
        ) as client:
            async with client.get_queue_receiver(
                queue_name=settings.service_bus_queue_name,
                max_wait_time=settings.service_bus_max_wait,
                prefetch_count=batch_size,
            ) as receiver:
                LOG.info(
                    "Worker activo: lotes de %s mensajes, pool de %s conexiones",
                    batch_size,
                    settings.worker_db_pool_size,
                )
                while not stop.is_set():
                    try:
                        batch = await receiver.receive_messages(
                            max_message_count=batch_size,
                            max_wait_time=settings.service_bus_max_wait,
                        )
                    except Exception as error:  # noqa: BLE001 - la cola no tumba el bucle
                        LOG.warning("No se pudo recibir de la cola (%s)", type(error).__name__)
                        await _wait(stop, settings.worker_idle_backoff)
                        continue
                    if not batch:
                        continue
                    # Un lote = como mucho 12 escrituras, y el siguiente no empieza
                    # hasta que este termina: el ritmo de IOPS queda acotado por diseño.
                    await asyncio.gather(
                        *(handle_message(receiver, message, deps, limiter) for message in batch),
                        return_exceptions=True,
                    )
    finally:
        await engine.dispose()


async def _wait(stop: asyncio.Event, seconds: int) -> None:
    """Espera interrumpible: un SIGTERM no tiene que aguantar el backoff completo."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


def _install_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        received = getattr(signal, name, None)
        if received is None:
            continue
        try:
            loop.add_signal_handler(received, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows no lo admite
            signal.signal(received, lambda *_: stop.set())


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        settings = get_settings()
    except ConfigurationError as error:
        print(error, file=sys.stderr)
        return 1
    if not settings.service_bus_enabled:
        # Degradación elegante: sin cola no hay nada que consumir y salir con 0 evita
        # que el App Service entre en un ciclo de reinicios.
        LOG.warning(
            "AZURE_SERVICE_BUS_CONNECTION_STRING y SERVICE_BUS_QUEUE_NAME no están "
            "definidas: no hay cola que consumir. La API escribe las cartas de forma "
            "síncrona; el worker termina sin error."
        )
        return 0
    try:
        import azure.servicebus  # noqa: F401, PLC0415 - solo para dar un error legible
    except ImportError:
        LOG.error("Falta azure-servicebus. Instálalo con: pip install '.[azure]'")
        return 1

    stop = asyncio.Event()
    _install_signals(stop)
    while not stop.is_set():
        try:
            await consume(settings, stop)
        except Exception as error:  # noqa: BLE001 - reconecta en vez de morir
            LOG.exception("El consumidor cayó (%s); reintentando", type(error).__name__)
            await _wait(stop, settings.worker_idle_backoff)
    LOG.info("Worker detenido de forma ordenada")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
