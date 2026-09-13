"""Catálogo de temas de la carta: paletas, colores del QR y trazos decorativos.

Es el espejo en el servidor de lo que el frontend tiene repartido en
``src/modules/editor/types.ts`` (paletas), ``src/utils/themeDecor.ts`` (motivo, metal,
trama y filo) y ``src/utils/qrTheme.ts`` (colores del QR y emblema alado). El backend no
lo tenía: el campo ``theme`` es texto libre y el correo salía siempre rosa. Con esto la
postal del correo y la del PDF se pintan con el estilo que el comprador eligió y vio en
la previsualización.

Los valores se copian tal cual, no se derivan: si el front cambia un color, se cambia
aquí. Un slug desconocido cae en ``classic``, igual que hace ``themeFromSlug`` allí.
"""

import re
from dataclasses import dataclass

DEFAULT_THEME = "classic"

# Contraste mínimo entre módulos y fondo del QR. Por debajo, muchos lectores fallan.
MIN_QR_CONTRAST = 7.0


@dataclass(frozen=True, slots=True)
class Palette:
    slug: str
    name: str
    bg: str
    card_bg: str
    text: str
    accent: str
    border: str
    metal: str
    motif: str
    texture: str
    edge: str

    @property
    def dark(self) -> bool:
        """Fondo oscuro: quien consuma la paleta se adapta (QR sobre blanco, etc.)."""
        return relative_luminance(self.bg) < 0.2


def _theme(slug, name, bg, card_bg, text, accent, border, metal, motif, texture, edge):
    return Palette(slug, name, bg, card_bg, text, accent, border, metal, motif, texture, edge)


THEMES: dict[str, Palette] = {
    palette.slug: palette
    for palette in (
        _theme(
            "classic",
            "Romántico Clásico",
            "#fef8fa",
            "#ffffff",
            "#1d1b1d",
            "#a20513",
            "#e4beba",
            "#D4AF37",
            "heart",
            "dots",
            "double",
        ),
        _theme(
            "pastel-pink",
            "Rosado Pastel",
            "#fff5f7",
            "#ffe4e6",
            "#881337",
            "#e11d48",
            "#fecdd3",
            "#E0A899",
            "petals",
            "weave",
            "dashed",
        ),
        _theme(
            "starry",
            "Noche Estrellada",
            "#0f172a",
            "#1e293b",
            "#f8fafc",
            "#fbbf24",
            "#334155",
            "#C7CBD4",
            "star",
            "stardust",
            "double",
        ),
        _theme(
            "sunset",
            "Atardecer Cálido",
            "#fff7ed",
            "#ffedd5",
            "#431407",
            "#ea580c",
            "#fed7aa",
            "#C98A4B",
            "sun",
            "diagonal",
            "plain",
        ),
        _theme(
            "lavender",
            "Sueño de Lavanda",
            "#f5f3ff",
            "#ede9fe",
            "#4c1d95",
            "#7c3aed",
            "#ddd6fe",
            "#B9A7D6",
            "sparkle",
            "stardust",
            "dashed",
        ),
        _theme(
            "emerald",
            "Jardín Esmeralda",
            "#f0fdf4",
            "#dcfce7",
            "#14532d",
            "#16a34a",
            "#bbf7d0",
            "#9BAE7F",
            "leaf",
            "grid",
            "double",
        ),
        _theme(
            "midnight",
            "Medianoche Azul",
            "#090d16",
            "#111827",
            "#f3f4f6",
            "#38bdf8",
            "#1f2937",
            "#9FB2C4",
            "moon",
            "stardust",
            "plain",
        ),
        _theme(
            "vintage",
            "Carta Vintage",
            "#faf5ef",
            "#f5ebe0",
            "#4a3b32",
            "#b45309",
            "#e6d5c3",
            "#B08653",
            "butterfly",
            "ruled",
            "double",
        ),
    )
}

# El front envía el id en kebab-case (`themeSlug`), pero un cliente viejo o una prueba
# puede mandar el id tal cual (`pastelPink`) o con guion bajo. Se normaliza antes de
# buscar para no castigar al comprador con la paleta por defecto.
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")


def palette_for(theme: str | None) -> Palette:
    slug = _CAMEL.sub(r"\1-\2", str(theme or "").strip()).lower().replace("_", "-")
    return THEMES.get(slug, THEMES[DEFAULT_THEME])


# --- Color (port de themePalette.ts) --------------------------------------------------


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    digits = value.strip().lstrip("#")
    if len(digits) == 3:
        digits = "".join(character * 2 for character in digits)
    digits = digits[:6]
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def _channel(value: float) -> int:
    """``Math.round`` de JavaScript sobre 0..255: el .5 sube, no va al par."""
    return int(min(255.0, max(0.0, value)) + 0.5)


def rgb_to_hex(red: float, green: float, blue: float) -> str:
    return "#" + "".join(f"{_channel(value):02x}" for value in (red, green, blue))


def _luminance_channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(value: str) -> float:
    red, green, blue = hex_to_rgb(value)
    return (
        0.2126 * _luminance_channel(red)
        + 0.7152 * _luminance_channel(green)
        + 0.0722 * _luminance_channel(blue)
    )


def contrast_ratio(one: str, other: str) -> float:
    """Razón de contraste WCAG entre dos colores, de 1 a 21."""
    first, second = relative_luminance(one), relative_luminance(other)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def darken_until_contrast(value: str, background: str, target: float) -> str:
    """Oscurece un color conservando su tono hasta alcanzar el contraste pedido.

    Devuelve negro si ni así se llega. Es lo que hace el front con acentos como el
    dorado ``#fbbf24``, que apenas da 1.7:1 sobre un fondo claro.
    """
    if contrast_ratio(value, background) >= target:
        return value
    red, green, blue = hex_to_rgb(value)
    for step in range(1, 21):
        factor = 1 - step / 20
        candidate = rgb_to_hex(red * factor, green * factor, blue * factor)
        if contrast_ratio(candidate, background) >= target:
            return candidate
    return "#000000"


def mix(value: str, other: str, amount: float) -> str:
    """Interpola dos colores; sustituye a ``rgba()`` en el correo, donde el alfa falla."""
    first, second = hex_to_rgb(value), hex_to_rgb(other)
    return rgb_to_hex(*(a + (b - a) * amount for a, b in zip(first, second, strict=True)))


def text_on(background: str) -> str:
    """Blanco o casi negro, el que mejor se lea sobre ``background`` (botones)."""
    return (
        "#ffffff"
        if contrast_ratio("#ffffff", background) >= contrast_ratio("#111111", background)
        else "#111111"
    )


def qr_colors(palette: Palette) -> tuple[str, str]:
    """``(oscuro, claro)`` del QR según la regla de ``qrTheme.ts``.

    Los temas oscuros no se mapean tal cual: un QR claro sobre fondo oscuro queda
    invertido y buena parte de los lectores lo rechaza, así que va sobre blanco. Y el
    acento se oscurece hasta cumplir el contraste mínimo: sigue leyéndose como el
    color del tema, pero escanea.
    """
    light = "#ffffff" if palette.dark else palette.bg
    raw = palette.text if relative_luminance(palette.accent) > 0.6 else palette.accent
    return darken_until_contrast(raw, light, MIN_QR_CONTRAST), light


# --- Trazos (port de themeDecor.ts y qrTheme.ts) ----------------------------------------


@dataclass(frozen=True, slots=True)
class Shape:
    """Una pieza de un motivo: un trazado SVG o un círculo, relleno o de trazo.

    Los motivos del frontend no son un ``<path>`` cada uno —el sol trae un círculo y ocho
    rayos, la mariposa dos alas—, así que aquí van desmenuzados: el pintor necesita saber
    qué se rellena y qué se traza, y con qué grosor.
    """

    path: str = ""
    circle: tuple[float, float, float] | None = None
    # 0 = relleno; mayor que 0 = trazo de ese grosor, en unidades de la caja de 48.
    width: float = 0.0


MOTIF_SHAPES: dict[str, tuple[Shape, ...]] = {
    "heart": (
        Shape(
            "M24 41C13.2 33.4 8 28.7 8 22.4 8 17.2 12.1 13 17.2 13c2.9 0 5.6 1.4 7.3 3.6C26.2 14.4 28.9 13 31.8 13 36.9 13 41 17.2 41 22.4 41 28.7 35.8 33.4 24 41Z"
        ),
    ),
    "petals": (
        Shape(
            "M24 6c4 0 7 3.4 7 7.6 0 1.3-.3 2.5-.8 3.6 1-.6 2.2-1 3.5-1 4 0 7.3 3.4 7.3 7.6S37.7 31.4 33.7 31.4c-1.3 0-2.5-.4-3.5-1 .5 1.1.8 2.3.8 3.6C31 38.2 28 41.6 24 41.6s-7-3.4-7-7.6c0-1.3.3-2.5.8-3.6-1 .6-2.2 1-3.5 1-4 0-7.3-3.4-7.3-7.6s3.3-7.6 7.3-7.6c1.3 0 2.5.4 3.5 1-.5-1.1-.8-2.3-.8-3.6C17 9.4 20 6 24 6Z"
        ),
    ),
    "star": (
        Shape("M24 6l4.9 12.3L42 20.4l-9.5 8.9 2.5 13.1L24 36.1l-11 6.3 2.5-13.1L6 20.4l13.1-2.1Z"),
    ),
    "sun": (
        Shape(circle=(24.0, 24.0, 10.0)),
        Shape(
            "M24 4v5M24 39v5M4 24h5M39 24h5M10 10l3.5 3.5M34.5 34.5 38 38M38 10l-3.5 3.5M13.5 34.5 10 38",
            width=3,
        ),
    ),
    "sparkle": (
        Shape(
            "M24 5c1.6 8.6 4.4 11.4 13 13-8.6 1.6-11.4 4.4-13 13-1.6-8.6-4.4-11.4-13-13 8.6-1.6 11.4-4.4 13-13Z"
        ),
        Shape(
            "M36.5 30c.8 4.3 2.2 5.7 6.5 6.5-4.3.8-5.7 2.2-6.5 6.5-.8-4.3-2.2-5.7-6.5-6.5 4.3-.8 5.7-2.2 6.5-6.5Z"
        ),
    ),
    "leaf": (
        Shape(
            "M39 9C20 11 10 20 10 31c0 3 .8 5.6 2.2 7.6C16 30 23 23.5 33 20.5 24 25 17 32 14.5 41.5",
            width=3,
        ),
    ),
    "moon": (
        Shape(
            "M34 10 C26 12 20 18 20 24 C20 30 26 36 34 38 C24 40 14 33 14 24 C14 15 24 8 34 10 Z"
        ),
    ),
    "butterfly": (
        Shape(
            "M24 24c-3-5-10-7-14-2 0 5.5 3.6 10.6 8.9 10.6 3.5 0 5.1-3.4 5.1-8.6Zm0 0c3-5 10-7 14-2 0 5.5-3.6 10.6-8.9 10.6-3.5 0-5.1-3.4-5.1-8.6Z"
        ),
        Shape(
            "M24 21.5c-.7-4-2-7.5-3.6-9.5 2.4-.6 3.6.9 3.6 9.5Zm0 0c.7-4 2-7.5 3.6-9.5-2.4-.6-3.6.9-3.6 9.5Z"
        ),
    ),
}

# Caja cuadrada ajustada a cada trazado: (x, y, lado). Medida sobre los extremos reales,
# con holgura para el trazo: con la caja común de 48 la hoja y la mariposa se veían
# diminutas y el corazón quedaba bajo, y en un emblema centrado eso canta.
MOTIF_BOX: dict[str, tuple[float, float, float]] = {
    "heart": (6, 8.5, 37),
    "petals": (4.2, 4, 39.6),
    "star": (3.8, 4, 40.4),
    "sun": (2, 2, 44),
    "sparkle": (6, 3, 42),
    "leaf": (6.25, 7, 36.5),
    "moon": (7.66, 7.66, 32.68),
    "butterfly": (8, 6.24, 32),
}


def motif_transform(motif: str, size: float, cx: float, cy: float) -> tuple[float, float, float]:
    """``(escala, dx, dy)`` que centra el motivo en ``(cx, cy)`` con el lado pedido."""
    x, y, box = MOTIF_BOX[motif]
    scale = size / box
    return scale, cx - size / 2 - x * scale, cy - size / 2 - y * scale


# --- Emblema alado del centro del QR ----------------------------------------------------

# Caja del emblema: apaisada, no cuadrada.
EMBLEM_WIDTH, EMBLEM_HEIGHT = 128.0, 72.0
EMBLEM_RATIO = EMBLEM_HEIGHT / EMBLEM_WIDTH
EMBLEM_RADIUS = 16.0
# Lado del emblema como fracción del lado del QR. Con esto ocupa ~6,6 % del área, muy por
# debajo del ~30 % que tolera la corrección de errores en nivel H.
EMBLEM_SCALE = 0.34
# Lado del motivo dentro del emblema y su centro, como en ``buildWingedCenterIcon``.
EMBLEM_MOTIF = (32.0, 64.0, 35.0)

EMBLEM_WINGS = (
    "M52 36 C44 26 34 20 22 17 C26 23 31 27 38 30 C28 30 19 27 10 22 C13 29 20 35 30 37 C21 39 14 43 9 49 C22 50 38 45 52 38 Z",
    "M76 36 C84 26 94 20 106 17 C102 23 97 27 90 30 C100 30 109 27 118 22 C115 29 108 35 98 37 C107 39 114 43 119 49 C106 50 90 45 76 38 Z",
)
EMBLEM_HIGHLIGHTS = (
    "M52 36 C44 29 34 25 24 23 C29 27 36 31 44 33 Z",
    "M76 36 C84 29 94 25 104 23 C99 27 92 31 84 33 Z",
)
EMBLEM_HIGHLIGHT_ALPHA = 0.3

# Los motivos de trazo se dibujan con stroke; el resto van rellenos.
STROKED_MOTIFS = frozenset({"leaf"})


def metal_ink(palette: Palette) -> str:
    """Color de los nombres: el metal del tema, legible sobre el papel.

    El metal puro da ~2:1 contra un papel claro y los nombres se perdían, así que se
    oscurece hasta 3:1 conservando el tono. En los temas oscuros el papel ya es oscuro y
    no hace falta tocarlo.
    """
    if palette.dark:
        return palette.metal
    return darken_until_contrast(palette.metal, palette.card_bg, 3)
