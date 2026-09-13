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

Los consentimientos ya firmados conservan lo que aceptaron: la versión ``2026-09-10``
apunta al checksum ``089167034d22916a4e11890b4ad04e40634872b0e520f7f823fbd66ec2e95706``,
y ese texto se recupera con ``git log -p -- app/legal/terms_v1.md``.
"""

import hashlib
from importlib import resources

TERMS_KIND = "terms_and_privacy"
# El número lo manda el documento (``**Versión:** 1.1``): ``TERMS_VERSION`` tiene que
# aparecer dentro del texto, y poner aquí una fecha obligaría a alterarlo.
TERMS_VERSION = "1.1"

_RAW = resources.files(__name__).joinpath("terms_v1.md").read_text(encoding="utf-8")
TERMS_TEXT = _RAW.replace("\r\n", "\n").replace("\r", "\n")
TERMS_CHECKSUM = hashlib.sha256(TERMS_TEXT.encode("utf-8")).hexdigest()
