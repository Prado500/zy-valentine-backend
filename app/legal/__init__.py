"""Términos y política de tratamiento de datos: una sola fuente de verdad.

El texto vive aquí, no en el frontend, por dos razones. La primera es que el
consentimiento guarda el **checksum** del texto aceptado, y ese checksum solo prueba
algo si lo calcula quien lo guarda. La segunda es que el negocio no quiere una
landing externa: sirviéndolo por API, la persona lo lee dentro del propio modal de
compra sin salir del embudo.

Los saltos de línea se normalizan a ``\n`` antes de calcular el checksum: git puede
sacar el archivo con CRLF en Windows y el hash no puede depender del sistema en el
que se hizo el checkout.

Si el texto cambia, cambia su checksum: sube ``TERMS_VERSION`` en el mismo commit.
"""

import hashlib
from importlib import resources

TERMS_KIND = "terms_and_privacy"
TERMS_VERSION = "2026-09-10"

_RAW = resources.files(__name__).joinpath("terms_v1.md").read_text(encoding="utf-8")
TERMS_TEXT = _RAW.replace("\r\n", "\n").replace("\r", "\n")
TERMS_CHECKSUM = hashlib.sha256(TERMS_TEXT.encode("utf-8")).hexdigest()
