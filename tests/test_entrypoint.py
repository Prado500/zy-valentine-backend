"""El contenedor arranca el worker de la cola, y su muerte no pasa desapercibida.

Sin supervisión, `python worker.py &` deja el peor fallo posible: el silencioso. La
API seguiría respondiendo 202 —"tu carta está en camino"— mientras nadie vacía la
cola, y el comprador que ya pagó no recibiría nada.

Son comprobaciones del guion de arranque, no de la imagen: no se construye ningún
contenedor ni se abre ninguna conexión, así que corren en cualquier máquina y en el
pipeline. Las funcionales ejecutan el guion de verdad con `sh`, sustituyendo
`python` y `uvicorn` por dobles en el `PATH`.
"""

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
