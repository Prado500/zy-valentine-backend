"""Ciclo de vida de un mensaje en el worker: completar, reintentar o dead-letter.

Rule of 10 sobre ``handle_message``. Es la pieza que decide qué pasa cuando algo
falla a mitad de camino, así que se prueba entera sin PostgreSQL ni Azure: el
receptor de la cola y el servicio de aplicación se sustituyen por dobles.
"""

import asyncio
import json

import pytest

import worker
from app.core.config import Settings
from app.core.errors import ApiError

SECRET = "test-only-secret-000000000000000000000"
VALID = {
    "type": "letter.create",
    "version": 1,
    "userId": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
    "purchaseId": "3f2504e0-4f89-11d3-9a0c-0305e82c3302",
    "letter": {},
}


@pytest.fixture
def settings():
    return Settings(_env_file=None, session_secret=SECRET)


class Receiver:
    """Doble del receptor de Service Bus: anota cómo se liquidó cada mensaje."""

    def __init__(self, broken: bool = False):
        self.actions: list[tuple[str, str | None]] = []
        self.broken = broken

    async def complete_message(self, message):
        if self.broken:
            raise RuntimeError("el lock del mensaje expiró")
        self.actions.append(("complete", None))

    async def abandon_message(self, message):
        self.actions.append(("abandon", None))

    async def dead_letter_message(self, message, reason=None, error_description=None):
        self.actions.append(("dead_letter", reason))


class Message:
    def __init__(self, body, delivery_count: int = 0):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.delivery_count = delivery_count


class Session:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class Deps:
    """Sustituto de ``WorkerDeps``: mismo contrato, sin base de datos."""

    def __init__(self, settings: Settings, outcome=None):
        self.settings = settings
        self.outcome = outcome
        self.calls = 0

    def sessions(self):
        return Session()

    def service(self, db):
        return self

    async def fulfil_queued_letter(self, message: dict):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return "carta-1"


async def run(settings, body=VALID, outcome=None, delivery_count=0, broken=False):
    receiver = Receiver(broken=broken)
    deps = Deps(settings, outcome)
    await worker.handle_message(receiver, Message(body, delivery_count), deps, asyncio.Semaphore(5))
    return receiver.actions, deps


# --- handle_message: los cinco casos -----------------------------------------------------


async def test_happy_path_completes_the_message(settings):
    actions, deps = await run(settings)

    assert actions == [("complete", None)]
    assert deps.calls == 1


async def test_sad_path_business_error_goes_straight_to_the_dead_letter_queue(settings):
    """Una compra sin pagar no mejora con reintentos: gastaría IOPS para nada."""
    actions, _ = await run(
        settings, outcome=ApiError(409, "PURCHASE_NOT_PAID", "La compra no está pagada.")
    )

    assert actions == [("dead_letter", "PURCHASE_NOT_PAID")]


async def test_edge_last_allowed_delivery_is_retried_and_the_next_one_is_not(settings):
    """Frontera exacta de ``SERVICE_BUS_MAX_ATTEMPTS`` (5 por defecto)."""
    penultimate, _ = await run(settings, outcome=TimeoutError("base ocupada"), delivery_count=3)
    last, _ = await run(settings, outcome=TimeoutError("base ocupada"), delivery_count=4)

    assert penultimate == [("abandon", None)]
    assert last == [("dead_letter", "MAX_ATTEMPTS")]


async def test_edge_a_full_batch_is_processed_without_exceeding_the_pool(settings):
    """Doce mensajes a la vez, nunca más de cinco tocando la base simultáneamente."""
    receiver = Receiver()
    deps = Deps(settings)
    limiter = asyncio.Semaphore(settings.worker_db_pool_size)
    live = 0
    peak = 0

    async def counted(message):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0)
        live -= 1
        return "carta"

    deps.fulfil_queued_letter = counted

    await asyncio.gather(
        *(
            worker.handle_message(receiver, Message(VALID), deps, limiter)
            for _ in range(settings.service_bus_max_batch)
        )
    )

    assert len(receiver.actions) == 12
    assert peak <= settings.worker_db_pool_size == 5


async def test_exception_unexpected_failures_are_retried_not_discarded(settings):
    """Una caída de red o de la base es transitoria: el mensaje vuelve a la cola."""
    actions, _ = await run(settings, outcome=OSError("conexión rechazada"))

    assert actions == [("abandon", None)]


# --- Cuerpo del mensaje --------------------------------------------------------------------


@pytest.mark.parametrize("body", [b"{no-json", b"[]", b"", b"\xff\xfe\x00"])
async def test_an_unreadable_body_is_dead_lettered_without_retrying(settings, body):
    actions, deps = await run(settings, body=body)

    assert actions == [("dead_letter", "INVALID_JSON")]
    assert deps.calls == 0  # No se abre sesión de base de datos para un cuerpo ilegible.


async def test_a_5xx_from_a_dependency_is_treated_as_transient(settings):
    """503 no está en `PERMANENT_STATUS`: el correo o el almacenamiento pueden volver."""
    actions, _ = await run(
        settings, outcome=ApiError(503, "PAYMENTS_UNAVAILABLE", "Proveedor caído.")
    )

    assert actions == [("abandon", None)]


async def test_a_completed_letter_whose_lock_expired_is_not_lost(settings):
    """La carta ya está escrita: el mensaje se reentregará y la idempotencia la protege."""
    actions, deps = await run(settings, broken=True)

    assert actions == []  # complete falló; Service Bus volverá a entregar el mensaje.
    assert deps.calls == 1


async def test_liquidation_failures_never_break_the_loop(settings):
    """Si hasta el dead-letter falla, el worker sigue vivo para el resto del lote."""

    class Hostile(Receiver):
        async def dead_letter_message(self, message, reason=None, error_description=None):
            raise RuntimeError("la cola no responde")

    receiver = Hostile()
    await worker.handle_message(
        receiver, Message(b"{no-json"), Deps(settings), asyncio.Semaphore(1)
    )

    assert receiver.actions == []


# --- Arranque -------------------------------------------------------------------------------


async def test_without_a_queue_the_worker_exits_quietly(settings, monkeypatch, caplog):
    """Degradación elegante: sin cola configurada no hay nada que consumir.

    Termina con código 0 a propósito: un código de error metería al App Service en un
    ciclo de reinicios por una variable que puede no existir todavía.
    """
    monkeypatch.setattr(worker, "get_settings", lambda: settings)

    assert await worker.main() == 0


async def test_a_configuration_error_stops_the_worker(monkeypatch, capsys):
    from app.core.config import ConfigurationError

    def broken():
        raise ConfigurationError("DATABASE_URL no es válida")

    monkeypatch.setattr(worker, "get_settings", broken)

    assert await worker.main() == 1
    assert "DATABASE_URL" in capsys.readouterr().err
