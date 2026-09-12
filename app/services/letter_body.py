"""Cuerpo de la carta: mensaje, remitente y canción viajan en un solo campo.

``letters.body`` no tiene columnas para el remitente ni para la canción. El frontend
(``src/modules/editor/services/letterBody.ts``) las escribe como dos líneas al final
del cuerpo —``De parte de: X`` y ``Canción: <url>``— y las recupera al mostrar la
carta. Este módulo es la misma regla vista desde el servidor: sin él, el correo y el
documento adjunto imprimían "Canción: https://…" como un párrafo más y firmaban
"Alguien que te quiere" aunque el comprador hubiera puesto su nombre.

La lectura es fiel al ``parseBody`` del front: las marcas se buscan **solo en la última
línea**, se recortan de atrás hacia delante y como mucho una vez cada una. Un
"De parte de:" en mitad del mensaje es del mensaje y ahí se queda.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.html import sanitize

SENDER_MARK = "De parte de:"
SONG_MARK = "Canción:"

# Firma cuando el comprador dejó el campo vacío. Es la misma del visor público, y no
# se sustituye por el nombre de la cuenta: quien no firmó eligió no hacerlo.
DEFAULT_SENDER = "Alguien que te quiere"
MAX_SENDER_LENGTH = 120

# Solo se enlaza una canción de YouTube: es lo único que el formulario acepta y lo
# único que el visor sabe reproducir. Cualquier otra URL se queda como texto.
SONG_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
)


def _trailing(mark: str) -> re.Pattern[str]:
    """Última línea que empieza por la marca; captura lo que la sigue."""
    return re.compile(rf"^[ \t]*{re.escape(mark)}[ \t]*(.+?)[ \t]*$")


_SENDER = _trailing(SENDER_MARK)
_SONG = _trailing(SONG_MARK)


@dataclass(frozen=True, slots=True)
class ParsedBody:
    message: str
    sender: str | None
    song_url: str | None


def song_url_or_none(raw: str) -> str | None:
    """Devuelve la URL limpia si es una dirección https de YouTube; si no, ``None``."""
    url = sanitize(raw).strip()
    if not url or any(character.isspace() for character in url) or len(url) > 500:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or (parts.hostname or "") not in SONG_HOSTS:
        return None
    return url


def parse_body(body: str | None) -> ParsedBody:
    """Separa mensaje, remitente y canción de un cuerpo guardado."""
    rest = (body or "").replace("\r\n", "\n").replace("\r", "\n").rstrip()
    sender: str | None = None
    song: str | None = None
    seen_sender = seen_song = False

    for _ in range(2):
        head, _, last = rest.rpartition("\n")
        if not seen_song:
            found = _SONG.match(last)
            if found and (url := song_url_or_none(found.group(1))):
                song, seen_song = url, True
                rest = head.rstrip()
                continue
        if not seen_sender:
            found = _SENDER.match(last)
            if found:
                sender = sanitize(found.group(1)).strip()[:MAX_SENDER_LENGTH] or None
                seen_sender = True
                rest = head.rstrip()
                continue
        break

    return ParsedBody(message=rest.strip(), sender=sender, song_url=song)


def sender_or_default(sender: str | None) -> str:
    return sender or DEFAULT_SENDER
