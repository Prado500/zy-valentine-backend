"""El contenedor arranca el worker de la cola, y su muerte no pasa desapercibida.

Sin supervisión, `python worker.py &` deja el peor fallo posible: el silencioso. La
API seguiría respondiendo 202 —"tu carta está en camino"— mientras nadie vacía la
cola, y el comprador que ya pagó no recibiría nada.

Son comprobaciones del guion de arranque, no de la imagen: no se construye ningún
contenedor ni se abre ninguna conexión, así que corren en cualquier máquina y en el
pipeline. Las funcionales ejecutan el guion de verdad con `sh`, sustituyendo
`python` y `uvicorn` por dobles en el `PATH`.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

GITATTRIBUTES = REPO_ROOT / ".gitattributes"


def test_los_scripts_de_shell_se_versionan_con_finales_lf():
    """Un `#!/bin/sh\\r` no se ejecuta en Linux, y aquí `core.autocrlf` es `true`.

    Sin esta regla, quien clone en Windows obtiene CRLF en el árbol de trabajo y su
    `docker build` produce una imagen que no arranca. El pipeline no lo vería: el
    agente es Ubuntu y respeta el LF del repositorio.
    """
    assert GITATTRIBUTES.is_file(), "falta .gitattributes"
    reglas = GITATTRIBUTES.read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in reglas


# --- El guion de arranque -------------------------------------------------------------

ENTRYPOINT = REPO_ROOT / "entrypoint.sh"


def test_el_guion_de_arranque_existe_y_tiene_shebang():
    assert ENTRYPOINT.is_file(), "falta entrypoint.sh"
    assert ENTRYPOINT.read_bytes().startswith(b"#!/bin/sh\n")


def test_el_guion_no_lleva_retornos_de_carro():
    """Blindaje del .gitattributes: si alguien se lo salta, esto se pone en rojo."""
    assert b"\r" not in ENTRYPOINT.read_bytes(), (
        "entrypoint.sh tiene CRLF; el contenedor Linux no podra ejecutarlo"
    )


def test_uvicorn_se_lanza_con_exec_para_seguir_siendo_pid_1():
    """Sin `exec`, el shell queda de PID 1 y uvicorn no recibe el SIGTERM del apagado."""
    guion = ENTRYPOINT.read_text(encoding="utf-8")
    assert "exec uvicorn app.main:app" in guion


def test_el_worker_solo_arranca_con_las_dos_variables_de_la_cola():
    """Misma puerta que `Settings.service_bus_enabled`: sin cola no hay worker."""
    guion = ENTRYPOINT.read_text(encoding="utf-8")
    assert "AZURE_SERVICE_BUS_CONNECTION_STRING" in guion
    assert "SERVICE_BUS_QUEUE_NAME" in guion
    assert "python worker.py &" in guion


def test_el_vigilante_no_asume_que_el_proceso_principal_sea_el_pid_1():
    """`kill -TERM 1` haria el guion inejecutable, y no comprobable, fuera del contenedor."""
    guion = ENTRYPOINT.read_text(encoding="utf-8")
    assert "kill -TERM 1" not in guion
    assert "main_pid=$$" in guion


# --- Comportamiento real del guion ----------------------------------------------------
#
# Las de arriba comprueban que el guion *dice* lo correcto; estas, que lo *hace*. Se
# ejecuta de verdad con `sh`, sustituyendo `python` y `uvicorn` por dobles en el PATH.
#
# La salida va a ficheros y no a tuberias a proposito: el subshell vigilante hereda los
# descriptores y los mantiene abiertos mientras el worker viva, asi que `capture_output`
# se quedaria esperando el EOF de un proceso que no ha terminado.

sin_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="requiere un shell POSIX")

CONEXION = "Endpoint=sb://ejemplo.servicebus.windows.net/;EntityPath=letters-dev"


def _doble(directorio: Path, nombre: str, cuerpo: str) -> Path:
    """Crea un ejecutable de mentira que el guion encontrara en el PATH."""
    ruta = directorio / nombre
    ruta.write_text(f"#!/bin/sh\n{cuerpo}\n", encoding="utf-8", newline="\n")
    ruta.chmod(0o755)
    return ruta


def _entorno(directorio: Path, **extra: str) -> dict:
    """PATH con los dobles delante y sin heredar la cola real de la maquina."""
    entorno = dict(os.environ)
    entorno["PATH"] = f"{directorio}{os.pathsep}{entorno['PATH']}"
    entorno.pop("AZURE_SERVICE_BUS_CONNECTION_STRING", None)
    entorno.pop("SERVICE_BUS_QUEUE_NAME", None)
    entorno.update(extra)
    return entorno


def _arranca(directorio: Path, entorno: dict) -> subprocess.CompletedProcess:
    salida = directorio / "salida"
    errores = directorio / "errores"
    with salida.open("wb") as out, errores.open("wb") as err:
        return subprocess.run(
            ["sh", str(ENTRYPOINT)],
            cwd=REPO_ROOT,
            env=entorno,
            stdout=out,
            stderr=err,
            timeout=60,
            check=False,
        )


def _espera(ruta: Path, condicion, limite: float = 20.0) -> str:
    """Espera a que el fichero cumpla la condicion. Devuelve lo ultimo que leyo."""
    fin = time.monotonic() + limite
    visto = ""
    while time.monotonic() < fin:
        if ruta.exists():
            visto = ruta.read_text(encoding="utf-8", errors="replace")
            if condicion(visto):
                return visto
        time.sleep(0.05)
    return visto


@sin_sh
def test_sin_cola_configurada_no_arranca_el_worker(tmp_path):
    """Degradacion elegante: la API sola, exactamente como antes de este cambio."""
    huellas = tmp_path / "huellas"
    _doble(tmp_path, "python", f'echo worker >> "{huellas.as_posix()}"')
    _doble(tmp_path, "uvicorn", f'echo api >> "{huellas.as_posix()}"')

    resultado = _arranca(tmp_path, _entorno(tmp_path))

    assert resultado.returncode == 0
    _espera(huellas, lambda t: "api" in t)
    time.sleep(1)  # margen para que un worker indebido dejara su huella
    assert huellas.read_text(encoding="utf-8").split() == ["api"]
    assert "sin cola configurada" in (tmp_path / "errores").read_text(encoding="utf-8")


@sin_sh
def test_una_sola_de_las_dos_variables_no_activa_el_worker(tmp_path):
    """Misma puerta que `Settings.service_bus_enabled`: hacen falta las dos."""
    huellas = tmp_path / "huellas"
    _doble(tmp_path, "python", f'echo worker >> "{huellas.as_posix()}"')
    _doble(tmp_path, "uvicorn", f'echo api >> "{huellas.as_posix()}"')

    resultado = _arranca(tmp_path, _entorno(tmp_path, SERVICE_BUS_QUEUE_NAME="letters-dev"))

    assert resultado.returncode == 0
    _espera(huellas, lambda t: "api" in t)
    time.sleep(1)
    assert huellas.read_text(encoding="utf-8").split() == ["api"]


@sin_sh
def test_con_cola_configurada_arranca_el_worker_y_la_api(tmp_path):
    huellas = tmp_path / "huellas"
    _doble(tmp_path, "python", f'echo worker >> "{huellas.as_posix()}"; sleep 30')
    _doble(tmp_path, "uvicorn", f'echo api >> "{huellas.as_posix()}"')

    resultado = _arranca(
        tmp_path,
        _entorno(
            tmp_path,
            AZURE_SERVICE_BUS_CONNECTION_STRING=CONEXION,
            SERVICE_BUS_QUEUE_NAME="letters-dev",
        ),
    )

    assert resultado.returncode == 0
    _espera(huellas, lambda t: {"api", "worker"} <= set(t.split()))
    assert sorted(huellas.read_text(encoding="utf-8").split()) == ["api", "worker"]


@sin_sh
def test_si_el_worker_muere_el_guion_termina(tmp_path):
    """El caso que motiva todo esto: nada de 202 sin nadie que vacie la cola."""
    _doble(tmp_path, "python", "exit 1")  # el worker se cae al instante
    _doble(tmp_path, "uvicorn", "sleep 45")  # la API viviria tres cuartos de minuto

    inicio = time.monotonic()
    resultado = _arranca(
        tmp_path,
        _entorno(
            tmp_path,
            AZURE_SERVICE_BUS_CONNECTION_STRING=CONEXION,
            SERVICE_BUS_QUEUE_NAME="letters-dev",
            WORKER_WATCH_INTERVAL="1",
        ),
    )
    transcurrido = time.monotonic() - inicio

    assert resultado.returncode != 0, "el guion sobrevivio a la muerte del worker"
    assert transcurrido < 40, "el vigilante tardo demasiado en reaccionar"
    errores = _espera(tmp_path / "errores", lambda t: "FATAL" in t)
    assert "FATAL" in errores


@sin_sh
def test_un_worker_vivo_no_interrumpe_la_api(tmp_path):
    """El vigilante no debe disparar por su cuenta mientras todo va bien."""
    _doble(tmp_path, "python", "sleep 30")
    _doble(tmp_path, "uvicorn", "sleep 4; echo api-termino-sola")

    resultado = _arranca(
        tmp_path,
        _entorno(
            tmp_path,
            AZURE_SERVICE_BUS_CONNECTION_STRING=CONEXION,
            SERVICE_BUS_QUEUE_NAME="letters-dev",
            WORKER_WATCH_INTERVAL="1",
        ),
    )

    assert resultado.returncode == 0, "el vigilante tumbo una API sana"
    assert "api-termino-sola" in (tmp_path / "salida").read_text(encoding="utf-8")
    assert "FATAL" not in (tmp_path / "errores").read_text(encoding="utf-8")
