"""La postal del QR: medidas, adornos y textos, en un solo sitio.

Port de ``src/utils/qrCard.ts``. En el frontend ese módulo existe porque la postal se
pinta dos veces —en pantalla con HTML y en el PNG con canvas— y tienen que salir
idénticas. Aquí cumple el mismo papel entre lo que se ve en la app y lo que sale por
correo: las medidas se calculan a partir del lado del QR, no se escriben sueltas en cada
pintor.

Todo está en píxeles de diseño, para un QR de 212. El pintor multiplica por una escala
entera, así que los módulos del código caen siempre en píxeles exactos.
"""

import math
from dataclasses import dataclass

# Lado del QR para el que está pensado el diseño; el resto escala desde aquí.
REF_QR = 212


def _px(value: float, k: float) -> int:
    """Redondeo de JavaScript: el .5 sube. Con ``k = 1`` no cambia nada, pero una
    postal pedida a otro tamaño debe dar las mismas medidas que el frontend."""
    return math.floor(value * k + 0.5)


@dataclass(frozen=True, slots=True)
class Metrics:
    qr: int
    width: int
    height: int
    pad_x: int
    pad_top: int
    pad_bottom: int
    radius: int
    # Lado de la enredadera de esquina y su separación del borde.
    corner: int
    corner_inset: int
    # Margen claro alrededor del código, dentro de su baldosa.
    tile_pad: int
    tile_radius: int
    name_label: int
    name_size: int
    name_line: int
    gap_after_name: int
    note_size: int
    note_line: int
    gap_after_note: int
    gap_after_qr: int
    from_label: int
    from_size: int
    from_line: int

    @property
    def tile(self) -> int:
        """Lado de la baldosa del código: es lo que viaja suelto en el correo."""
        return self.qr + self.tile_pad * 2


def metrics(qr: int = REF_QR, with_note: bool = True) -> Metrics:
    """Medidas de la postal.

    ``with_note`` reserva la banda de la nota. Sin ella —la baldosa suelta, o las
    postales de muestra de la landing— el hueco desaparece en vez de quedar vacío.
    """
    k = qr / REF_QR
    pad_top = _px(34, k)
    pad_bottom = _px(28, k)
    tile_pad = _px(10, k)
    name_label = _px(11, k)
    name_line = _px(44, k)
    gap_after_name = _px(8, k)
    note_line = _px(20, k)
    gap_after_note = _px(14, k)
    gap_after_qr = _px(16, k)
    from_label = _px(10, k)
    from_line = _px(38, k)
    pad_x = _px(30, k)

    note_band = note_line * 2 + gap_after_note if with_note else 0
    height = (
        pad_top
        + name_label
        + name_line
        + gap_after_name
        + note_band
        + qr
        + tile_pad * 2
        + gap_after_qr
        + from_label
        + from_line
        + pad_bottom
    )
    return Metrics(
        qr=qr,
        width=qr + tile_pad * 2 + pad_x * 2,
        height=height,
        pad_x=pad_x,
        pad_top=pad_top,
        pad_bottom=pad_bottom,
        radius=_px(24, k),
        corner=_px(38, k),
        corner_inset=_px(12, k),
        tile_pad=tile_pad,
        tile_radius=_px(12, k),
        name_label=name_label,
        name_size=_px(38, k),
        name_line=name_line,
        gap_after_name=gap_after_name,
        note_size=_px(13, k),
        note_line=note_line,
        gap_after_note=gap_after_note,
        gap_after_qr=gap_after_qr,
        from_label=from_label,
        from_size=_px(32, k),
        from_line=from_line,
    )


# --- Enredadera de esquina ------------------------------------------------------------

# Los mismos trazados de ``CornerFlourish``, en una caja de 64×64.
CORNER_BOX = 64
CORNER_ARC_OUTER = "M2 40 C2 19 19 2 40 2"
CORNER_ARC_INNER = "M9 40 C9 23 23 9 40 9"
CORNER_LEAF = "M20 14 C24 9 30 8 34 9 C31 14 25 16 20 14 Z"
CORNER_DOTS = ((40.0, 2.0), (2.0, 40.0))
CORNER_DOT_RADIUS = 1.4
CORNERS = ("tl", "tr", "bl", "br")
CORNER_ANGLE = {"tl": 0, "tr": 90, "br": 180, "bl": 270}

# Opacidades ya multiplicadas por el 0.6 con que el componente monta la enredadera.
CORNER_ALPHA = {"outer": 0.55 * 0.6, "inner": 0.3 * 0.6, "leaf": 0.45 * 0.6, "dot": 0.5 * 0.6}
CORNER_WIDTH = {"outer": 1.0, "inner": 0.9, "leaf": 0.9}


# --- Flores del tema ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Flower:
    # Esquina superior izquierda, en fracción del ancho y del alto.
    x: float
    y: float
    # Lado, en fracción del ancho de la tarjeta.
    size: float
    rot: float
    alpha: float
    # Cuál de las tres flores del tema.
    pick: int


# Cuatro flores, dos arriba y dos abajo, pegadas a los costados: ocupan solo las bandas
# donde no hay texto. Son cuatro y muy apagadas a propósito —acompañan al código, no
# compiten con él—, y van en fracción para que valgan a cualquier tamaño.
CARD_FLOWERS = (
    Flower(x=0.015, y=0.055, size=0.20, rot=-16, alpha=0.50, pick=0),
    Flower(x=0.790, y=0.115, size=0.16, rot=20, alpha=0.40, pick=1),
    Flower(x=0.010, y=0.790, size=0.17, rot=13, alpha=0.44, pick=2),
    Flower(x=0.810, y=0.855, size=0.145, rot=-14, alpha=0.38, pick=0),
)

# --- Texto ----------------------------------------------------------------------------

TO_LABEL = "P A R A"
FROM_LABEL = "D E"
# Nota por defecto cuando la dedicatoria aún no tiene mensaje.
DEFAULT_NOTE = "Eres muy especial en mi vida."
# Segunda línea fija, la que invita a escanear.
SCAN_LINE = "Escanéalo…"
# Mismos respaldos que el visor: quien no firmó eligió no hacerlo.
DEFAULT_RECIPIENT = "ti"
DEFAULT_SENDER = "Alguien que te quiere"


def fit_note(text: str, max_width: float, measure) -> str:
    """Recorta la nota a una línea que quepa en ``max_width``.

    ``measure`` la pone quien llama: el pintor mide de verdad con la fuente cargada.
    """
    if measure(text) <= max_width:
        return text

    out = ""
    for word in text.split():
        candidate = f"{out} {word}" if out else word
        if measure(f"{candidate}…") > max_width:
            break
        out = candidate
    # Si ni la primera palabra cabe, se corta por letras.
    if not out:
        out = text
        while len(out) > 1 and measure(f"{out}…") > max_width:
            out = out[:-1]
    return out.rstrip(",;:.") + "…"
