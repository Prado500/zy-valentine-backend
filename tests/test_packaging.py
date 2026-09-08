"""Guarda contra el *shadowing* del paquete ``app``.

Un ``pip install .`` sin ``-e`` deja una copia de ``app/`` en site-packages junto a
la del repositorio. Las dos responden a ``import app`` y gana la que anteceda en
``sys.path``, que cambia según desde dónde se arranque el proceso: el mismo árbol
de trabajo puede servir reglas distintas en pytest, en uvicorn y en el contenedor.

Lo peligroso es el síntoma. Cuando la copia instalada se queda atrás no hay ningún
error de importación —eso se vería enseguida—, sino una validación que rechaza
datos válidos: un 422 que no está en el código que uno tiene delante. Ya ocurrió
con ``Register.password``, que en la fuente aceptaba 4 caracteres y en la copia
instalada seguía exigiendo 12. Este módulo convierte ese fallo silencioso en rojo.

Son comprobaciones estáticas: no abren base de datos ni levantan la aplicación, así
que corren siempre, también sin ``TEST_DATABASE_URL``.
"""

import sysconfig
from pathlib import Path

import app
from tests.conftest import REPO_ROOT

DOCKERFILE = REPO_ROOT / "Dockerfile"
EDITABLE = 'pip install -c requirements.lock -e ".[test]"'


def test_app_se_importa_desde_el_repositorio():
    """Lo que importa pytest tiene que ser la fuente, no una copia de hace días."""
    importado = Path(app.__file__).resolve().parent
    esperado = REPO_ROOT / "app"
    assert importado == esperado, (
        f"`import app` resolvió a {importado} en vez de {esperado}: hay una copia "
        f"instalada tapando la fuente. Reinstala en modo editable con `{EDITABLE}`."
    )


def test_no_queda_ninguna_copia_instalada_de_app():
    """El editable deja un `.pth`, nunca un directorio ``app/`` en site-packages."""
    rutas = {Path(sysconfig.get_paths()[clave]) / "app" for clave in ("purelib", "platlib")}
    copias = sorted(str(ruta) for ruta in rutas if ruta.exists())
    assert not copias, (
        f"El paquete está duplicado en site-packages: {copias}. Es la instalación no "
        f"editable que provoca el shadowing; reinstala con `{EDITABLE}`."
    )


def test_la_imagen_instala_el_paquete_en_modo_editable():
    """El contenedor ya copia ``app/``; instalar sin ``-e`` volvería a duplicarlo."""
    instalaciones = [
        linea.strip()
        for linea in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if "pip install" in linea
    ]
    assert instalaciones, "El Dockerfile ya no instala el paquete; revisa esta guarda."
    assert all(" -e " in linea for linea in instalaciones), (
        f"El Dockerfile instala sin `-e`: {instalaciones}. La imagen acabaría con dos "
        "copias de `app/` —la del COPY y la de site-packages— compitiendo por `import app`."
    )
