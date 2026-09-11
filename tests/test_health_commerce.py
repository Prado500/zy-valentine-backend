"""El diagnóstico refleja el publicador real, no la mera presencia de variables.

Rule of 10 sobre ``CommerceService.health``: es lo que mira un operador para decidir
si el asincronismo está vivo. Si dice ``service-bus`` mientras la aplicación escribe
de forma síncrona, certifica salud sobre un sistema degradado y nadie investiga.

No se abre base de datos ni Azure: el publicador se sustituye por un doble, así que
estas pruebas corren en cualquier máquina y en el pipeline.
"""

from app.core.config import Settings
from app.services.commerce import CommerceService
from app.services.service_bus import MockPublisher

SECRET = "test-only-secret-000000000000000000000"
CONNECTION = (
    "Endpoint=sb://ejemplo.servicebus.windows.net/;SharedAccessKeyName=send;"
    "SharedAccessKey=" + "A" * 43 + "="
)


class PublicadorReal(MockPublisher):
    """Doble del publicador de Azure: aquí lo único que importa es ``enabled``."""

    enabled = True


def _servicio(queue, **cambios) -> CommerceService:
    settings = Settings(_env_file=None, session_secret=SECRET, **cambios)
    return CommerceService(
        db=None, settings=settings, storage=None, mailer=None, payments=None, queue=queue
    )


def _con_cola(queue) -> CommerceService:
    return _servicio(
        queue,
        azure_service_bus_connection_string=CONNECTION,
        service_bus_queue_name="letters-dev",
    )


def test_sin_cola_configurada_informa_sincrono():
    """Camino feliz del modo síncrono: ni variables ni publicador."""
    assert _servicio(MockPublisher()).health().letterQueue == "sync"


def test_con_publicador_real_informa_service_bus():
    """Camino feliz del modo asíncrono: variables puestas y cliente construido."""
    assert _con_cola(PublicadorReal()).health().letterQueue == "service-bus"


def test_variables_presentes_pero_publicador_degradado_informa_sincrono():
    """El caso que antes mentía: la cadena existe pero el cliente no se pudo construir.

    ``build_publisher`` devuelve un ``MockPublisher`` cuando la cadena es inválida o
    falta ``azure-servicebus``, y la aplicación sigue escribiendo de forma síncrona.
    El diagnóstico tiene que decir eso, no lo que se le pidió que hiciera.
    """
    assert _con_cola(MockPublisher("cliente no construible")).health().letterQueue == "sync"


def test_el_resto_del_diagnostico_no_cambia():
    """Guarda de no regresión: de este método solo se toca ``letterQueue``."""
    salud = _servicio(MockPublisher()).health()
    assert salud.paymentProvider == "none"
    assert salud.storageBackend == "local"
    assert salud.mailBackend == "console"
    assert salud.freezeAfterPublish is True


def test_el_diagnostico_nunca_expone_la_cadena_de_conexion():
    """El endpoint es abierto: publica modos, jamás credenciales ni URLs."""
    volcado = _con_cola(PublicadorReal()).health().model_dump_json()
    assert "SharedAccessKey" not in volcado
    assert "servicebus.windows.net" not in volcado
