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
