"""Fuentes incrustadas: rutas, glifos disponibles y saneado del texto.

Las mismas tres familias que carga el frontend desde Google Fonts, vendorizadas en
``app/assets/fonts`` porque aquí se dibuja sin red: Playfair Display para los títulos y
rótulos, Great Vibes para los nombres y Be Vietnam Pro para el texto menudo.

Lo usan los dos pintores —el PDF con fpdf2 y la postal con Pillow—, así que vive aparte:
si cada uno cargara sus fuentes, una carta podría salir con una tipografía en el correo y
otra en el adjunto.
"""

import re
import unicodedata
from functools import lru_cache
from importlib.resources import files

from PIL import ImageFont

from app.core.html import sanitize

FONT_DIR = files("app.assets.fonts")

SERIF = "playfair"
SANS = "bevietnam"
SCRIPT = "greatvibes"

# (familia, estilo, archivo). El estilo es el de fpdf2; Pillow pide el archivo.
FONTS = (
    (SERIF, "", "PlayfairDisplay-SemiBold.ttf"),
    (SANS, "", "BeVietnamPro-Regular.ttf"),
    (SANS, "B", "BeVietnamPro-SemiBold.ttf"),
    (SCRIPT, "", "GreatVibes-Regular.ttf"),
)

_FILE = {(family, style): name for family, style, name in FONTS}

# Categorías Unicode que no se imprimen aunque una fuente tuviera el glifo: símbolos
# (emojis), modificadores, formato (ZWJ, selectores), sustitutos, privados y marcas.
_DROPPED_CATEGORIES = frozenset({"So", "Sk", "Cf", "Cs", "Co", "Cn", "Mn", "Me"})

# "Para ti 💌, mi amor" pierde el emoji y quedaría "Para ti , mi amor".
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?…)\]])")


def font_path(family: str, style: str = "") -> str:
    return str(FONT_DIR.joinpath(_FILE[(family, style)]))


@lru_cache(maxsize=32)
def pil_font(family: str, size: int, style: str = "") -> ImageFont.FreeTypeFont:
    """Fuente de Pillow ya cargada. Se cachea: la postal pide la misma varias veces."""
    return ImageFont.truetype(font_path(family, style), size)


@lru_cache(maxsize=1)
def supported_codepoints() -> frozenset[int]:
    """Unión de los mapas de caracteres de las fuentes incrustadas."""
    from fontTools.ttLib import TTFont  # noqa: PLC0415 - llega con fpdf2, se carga una vez

    codepoints: set[int] = set()
    for family, style, _ in FONTS:
        font = TTFont(font_path(family, style), lazy=True)
        codepoints.update(font.getBestCmap())
        font.close()
    return frozenset(codepoints)


def printable(value: object, limit: int = 160) -> str:
    """Texto que las fuentes saben dibujar: sin emojis, controles ni glifos ausentes.

    Se filtra por categoría Unicode **y** por el mapa de caracteres de las fuentes, porque
    las dos listas fallan por separado: un símbolo raro con glifo se imprimiría como icono
    fuera de tono, y una letra de otro alfabeto sin glifo saldría como cuadro vacío.
    """
    text = unicodedata.normalize("NFC", sanitize(value))
    supported = supported_codepoints()
    kept = "".join(
        character
        for character in text
        if character.isspace()
        or (
            ord(character) in supported
            and unicodedata.category(character) not in _DROPPED_CATEGORIES
        )
    )
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", " ".join(kept.split()))[:limit]
