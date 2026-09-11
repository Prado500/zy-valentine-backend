"""Tarjeta QR imprimible: un PDF A5 con el diseño del tema de la carta.

Es lo que el comprador imprime y pone dentro del ramo o de la caja: el código hacia
la carta, "De X con cariño para Y" y el título, en una sola página. Es el único sitio
donde va la dedicatoria: el correo se dirige al comprador y no la repite. Sigue la
tarjeta QR del frontend (``ThemeQRCode.tsx``): la misma paleta, las mismas fuentes y
los mismos trazos, aquí dibujados con fpdf2.

Decisiones:

- **Todo vectorial.** El QR se dibuja módulo a módulo como rectángulos y los adornos
  como SVG, así que se imprime nítido a cualquier resolución y no pasa por Pillow.
- **Fuentes propias.** Se incrustan subconjuntos de Playfair Display, Be Vietnam Pro y
  Great Vibes (``app/assets/fonts``); no dependen del sistema ni de la red.
- **Nunca un cuadro vacío.** fpdf2 no lanza con un glifo ausente: dibuja un ``.notdef``
  y avisa en el log. :func:`printable` filtra antes lo que ninguna fuente tiene
  (emojis, sobre todo).
- **Síncrono a propósito.** Quien llama lo ejecuta en un hilo (``anyio.to_thread``)
  y con un ``CapacityLimiter``: subconjuntar cuatro fuentes cuesta CPU en la B1ms.
- ``fpdf`` se importa dentro de la función: arrastra Pillow y no debe tocar el
  arranque de la API ni del worker.
"""

import math
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

import segno

from app.core.html import safe_file_name, sanitize
from app.services.themes import (
    Palette,
    corner_flourish_svg,
    hex_to_rgb,
    mix,
    motif_svg,
    palette_for,
    qr_colors,
)

# Geometría en milímetros. A5 vertical; la hoja va centrada con 10 mm de fondo alrededor.
PAGE_WIDTH, PAGE_HEIGHT = 148.0, 210.0
SHEET_X, SHEET_Y, SHEET_WIDTH, SHEET_HEIGHT = 10.0, 10.0, 128.0, 190.0
TEXT_X, TEXT_WIDTH = 20.0, 108.0
QR_FRAME_X, QR_FRAME_Y, QR_FRAME_SIZE = 46.0, 106.0, 56.0
QR_SIZE = 44.0  # lado de los módulos; los 6 mm restantes por lado son la zona de silencio
FOOTER_Y = 190.0

# La trama del papel cuesta cientos de trazos. Se puede apagar sin tocar nada más.
TEXTURE = True

BRAND = "Eternal Dedications"
TITLE_FALLBACK = "Una carta especial"
RECIPIENT_FALLBACK = "Tu persona especial"

FONT_DIR = files("app.assets.fonts")
FONTS = (
    ("playfair", "", "PlayfairDisplay-SemiBold.ttf"),
    ("bevietnam", "", "BeVietnamPro-Regular.ttf"),
    ("bevietnam", "B", "BeVietnamPro-SemiBold.ttf"),
    ("greatvibes", "", "GreatVibes-Regular.ttf"),
)

# Categorías Unicode que no se imprimen aunque una fuente tuviera el glifo: símbolos
# (emojis), modificadores, formato (ZWJ, selectores), sustitutos, privados y marcas.
_DROPPED_CATEGORIES = frozenset({"So", "Sk", "Cf", "Cs", "Co", "Cn", "Mn", "Me"})

# "Para ti 💌, mi amor" pierde el emoji y quedaría "Para ti , mi amor".
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?…)\]])")


@dataclass(frozen=True, slots=True)
class CardContent:
    title: str
    recipient_name: str
    sender_name: str | None
    public_url: str
    theme: str
    letter_id: str
    version: int


def card_name(letter) -> str:
    """Nombre del adjunto. El título lo escribe el comprador: pasa por la lista blanca."""
    return f"tarjeta-qr-{safe_file_name(letter.title, fallback='carta')}.pdf"


def _font_path(name: str) -> str:
    return str(FONT_DIR.joinpath(name))


@lru_cache(maxsize=1)
def supported_codepoints() -> frozenset[int]:
    """Unión de los mapas de caracteres de las fuentes incrustadas."""
    from fontTools.ttLib import TTFont  # noqa: PLC0415 - llega con fpdf2, se carga una vez

    codepoints: set[int] = set()
    for _, _, name in FONTS:
        font = TTFont(_font_path(name), lazy=True)
        codepoints.update(font.getBestCmap())
        font.close()
    return frozenset(codepoints)


def printable(value: object, limit: int = 160) -> str:
    """Texto que las fuentes saben dibujar: sin emojis, controles ni glifos ausentes.

    Se filtra por categoría Unicode **y** por el cmap de las fuentes, porque las dos
    listas fallan por separado: un símbolo raro con glifo se imprimiría como icono
    fuera de tono, y una letra de otro alfabeto sin glifo saldría como cuadro.
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


# --- Lienzo: ayudas sobre FPDF ------------------------------------------------------------


class _Canvas:
    """Colores por hex, texto por líneas y formas centradas, sobre un ``FPDF``."""

    def __init__(self, pdf, palette: Palette):
        self.pdf = pdf
        self.palette = palette
        self.muted = mix(palette.text, palette.card_bg, 0.45)

    def fill(self, color: str) -> None:
        self.pdf.set_fill_color(*hex_to_rgb(color))

    def stroke(self, color: str, width: float = 0.3) -> None:
        self.pdf.set_draw_color(*hex_to_rgb(color))
        self.pdf.set_line_width(width)

    def ink(self, color: str) -> None:
        self.pdf.set_text_color(*hex_to_rgb(color))

    def font(self, family: str, size: float, style: str = "") -> None:
        self.pdf.set_font(family, style, size)

    def width(self, text: str) -> float:
        return self.pdf.get_string_width(text)

    def text(self, text: str, x: float, y: float, w: float, h: float, align: str = "C") -> None:
        self.pdf.set_xy(x, y)
        self.pdf.cell(w, h, text, align=align)

    def svg(self, markup: str, x: float, y: float, w: float, h: float) -> None:
        self.pdf.image(markup.encode("utf-8"), x=x, y=y, w=w, h=h)

    def dot(self, cx: float, cy: float, radius: float, style: str = "F") -> None:
        # `ellipse` toma la esquina del cuadro que lo contiene en todas las versiones
        # de fpdf2; `circle` cambió de firma entre versiones.
        self.pdf.ellipse(cx - radius, cy - radius, radius * 2, radius * 2, style=style)

    def speck(self, cx: float, cy: float, radius: float) -> None:
        """Punto de trama: a 0.2 mm un cuadrado y un círculo se ven igual, pero el
        cuadrado son 30 bytes en el PDF y el círculo, cuatro curvas de Bézier."""
        self.pdf.rect(cx - radius, cy - radius, radius * 2, radius * 2, style="F")

    def rounded(self, x: float, y: float, w: float, h: float, radius: float, style: str) -> None:
        self.pdf.rect(x, y, w, h, style=style, round_corners=True, corner_radius=radius)

    def wrap(self, text: str, width: float) -> list[str]:
        """Reparto voraz por palabras; una palabra más ancha que la caja se parte."""
        lines: list[str] = []
        current = ""
        for word in text.split(" "):
            candidate = f"{current} {word}".strip()
            if self.width(candidate) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
            piece = ""
            for character in word:
                if self.width(piece + character) <= width or not piece:
                    piece += character
                else:
                    lines.append(piece)
                    piece = character
            current = piece
        if current:
            lines.append(current)
        return lines or [""]

    def fit(
        self,
        family: str,
        text: str,
        width: float,
        max_pt: float,
        min_pt: float,
        max_lines: int,
    ) -> tuple[float, list[str]]:
        """Baja el cuerpo hasta que el texto quepa en ``max_lines``; si no, elipsis."""
        size = max_pt
        while True:
            self.font(family, size)
            lines = self.wrap(text, width)
            if len(lines) <= max_lines or size <= min_pt:
                break
            size = max(min_pt, size - 0.5)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            last = lines[-1]
            while last and self.width(last + "…") > width:
                last = last[:-1]
            lines[-1] = last.rstrip() + "…"
        return size, lines

    def paragraph(
        self, lines: list[str], x: float, y: float, w: float, line_h: float, align: str = "C"
    ) -> float:
        for index, line in enumerate(lines):
            self.text(line, x, y + index * line_h, w, line_h, align)
        return y + len(lines) * line_h


# --- Piezas compartidas por las dos páginas ------------------------------------------------


def _draw_sheet(canvas: _Canvas) -> None:
    palette = canvas.palette
    canvas.fill(palette.bg)
    canvas.pdf.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, style="F")
    canvas.fill(palette.card_bg)
    canvas.stroke(palette.border, 0.4)
    canvas.rounded(SHEET_X, SHEET_Y, SHEET_WIDTH, SHEET_HEIGHT, 6, "DF")
    if TEXTURE:
        _draw_texture(canvas)
    _draw_edge(canvas)
    for corner, (x, y) in {
        "tl": (13.0, 13.0),
        "tr": (119.0, 13.0),
        "bl": (13.0, 181.0),
        "br": (119.0, 181.0),
    }.items():
        canvas.svg(corner_flourish_svg(palette.metal, 16, corner), x, y, 16, 16)


def _hatch(
    canvas: _Canvas, angle: float, spacing: float, box: tuple[float, float, float, float]
) -> None:
    """Líneas paralelas con la inclinación pedida, recortadas a ``box``."""
    x0, y0, x1, y1 = box
    direction = (math.cos(math.radians(angle)), math.sin(math.radians(angle)))
    normal = (-direction[1], direction[0])
    corners = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
    projections = [cx * normal[0] + cy * normal[1] for cx, cy in corners]
    reach = 400.0
    with canvas.pdf.rect_clip(x0, y0, x1 - x0, y1 - y0):
        offset = min(projections)
        while offset <= max(projections):
            cx, cy = offset * normal[0], offset * normal[1]
            canvas.pdf.line(
                cx - direction[0] * reach,
                cy - direction[1] * reach,
                cx + direction[0] * reach,
                cy + direction[1] * reach,
            )
            offset += spacing


def _draw_texture(canvas: _Canvas) -> None:
    """Trama del papel, derivada del color de texto y casi transparente."""
    palette = canvas.palette
    box = (14.0, 14.0, 134.0, 196.0)
    x0, y0, x1, y1 = box
    with canvas.pdf.local_context(fill_opacity=0.07, stroke_opacity=0.07):
        canvas.fill(palette.text)
        canvas.stroke(palette.text, 0.15)
        texture = palette.texture
        if texture in ("dots", "stardust"):
            step = 4.0 if texture == "dots" else 8.0
            for i in range(int((x1 - x0) / step) + 1):
                for j in range(int((y1 - y0) / step) + 1):
                    canvas.speck(x0 + i * step, y0 + j * step, 0.2)
            if texture == "stardust":
                for i in range(int((x1 - x0) / 4.5)):
                    for j in range(int((y1 - y0) / 4.5)):
                        canvas.speck(x0 + 2.25 + i * 4.5, y0 + 2.25 + j * 4.5, 0.12)
        elif texture == "grid":
            _hatch(canvas, 0, 5.5, box)
            _hatch(canvas, 90, 5.5, box)
        elif texture == "ruled":
            _hatch(canvas, 0, 7.0, box)
        elif texture == "diagonal":
            _hatch(canvas, 115, 3.0, box)
        elif texture == "weave":
            _hatch(canvas, 45, 2.1, box)
            _hatch(canvas, -45, 2.1, box)


def _draw_edge(canvas: _Canvas) -> None:
    """Filo interior de la hoja: doble filete, punteado o nada, según el tema."""
    palette = canvas.palette
    if palette.edge == "plain":
        return
    pdf = canvas.pdf
    if palette.edge == "double":
        with pdf.local_context(stroke_opacity=0.16):
            canvas.stroke(palette.metal, 0.9)
            canvas.rounded(12, 12, 124, 186, 5, "D")
        with pdf.local_context(stroke_opacity=0.5):
            canvas.stroke(palette.metal, 0.25)
            canvas.rounded(13, 13, 122, 184, 4.5, "D")
        return
    with pdf.local_context(stroke_opacity=0.55):
        canvas.stroke(palette.metal, 0.25)
        pdf.set_dash_pattern(dash=1.2, gap=1.2)
        canvas.rounded(13, 13, 122, 184, 4.5, "D")
        pdf.set_dash_pattern()


def _draw_ornament(canvas: _Canvas, y: float) -> None:
    """Filigrana horizontal con el motivo del tema en el centro."""
    palette = canvas.palette
    with canvas.pdf.local_context(stroke_opacity=0.55, fill_opacity=0.55):
        canvas.stroke(palette.metal, 0.3)
        canvas.fill(palette.metal)
        canvas.pdf.line(34, y, 64, y)
        canvas.pdf.line(84, y, 114, y)
        canvas.dot(66, y, 0.5)
        canvas.dot(82, y, 0.5)
    canvas.svg(motif_svg(palette.motif, 5, palette.metal), 71.5, y - 2.5, 5, 5)


def _draw_footer(canvas: _Canvas, content: CardContent) -> None:
    palette = canvas.palette
    canvas.font("greatvibes", 11)
    canvas.ink(palette.accent)
    brand_width = canvas.width(BRAND)
    canvas.text(BRAND, TEXT_X, FOOTER_Y, brand_width + 1, 6, align="L")
    canvas.svg(
        motif_svg("heart", 3.2, palette.accent),
        TEXT_X + brand_width + 1.5,
        FOOTER_Y + 1.5,
        3.2,
        3.2,
    )
    canvas.font("bevietnam", 5.5)
    canvas.ink(canvas.muted)
    stamp = printable(f"Carta {content.letter_id} · versión {content.version}", limit=80)
    canvas.text(stamp, TEXT_X, FOOTER_Y + 0.5, TEXT_WIDTH, 5, align="R")


# --- Página 1: la tarjeta --------------------------------------------------------------------


def _draw_lockup(canvas: _Canvas, sender: str | None, recipient: str) -> None:
    """«De X con cariño para Y», con los nombres en cursiva y el resto en serif."""
    palette = canvas.palette
    y = 36.0
    if sender:
        canvas.ink(palette.text)
        canvas.font("playfair", 11)
        canvas.text("De", TEXT_X, y, TEXT_WIDTH, 5)
        y += 5.5
        size, lines = canvas.fit("greatvibes", sender, TEXT_WIDTH, 26, 18, 1)
        canvas.ink(palette.accent)
        canvas.font("greatvibes", size)
        canvas.text(lines[0], TEXT_X, y, TEXT_WIDTH, 12)
        y += 12.5
        lead = "con cariño para"
    else:
        y = 44.0
        lead = "Con cariño para"
    canvas.ink(palette.text)
    canvas.font("playfair", 11)
    canvas.text(lead, TEXT_X, y, TEXT_WIDTH, 5)
    y += 5.5
    size, lines = canvas.fit("greatvibes", recipient, TEXT_WIDTH, 26, 18, 1)
    canvas.ink(palette.accent)
    canvas.font("greatvibes", size)
    canvas.text(lines[0], TEXT_X, y, TEXT_WIDTH, 12)


def _draw_title(canvas: _Canvas, title: str) -> None:
    size, lines = canvas.fit("playfair", title, TEXT_WIDTH, 13, 11, 3)
    canvas.ink(canvas.palette.text)
    canvas.font("playfair", size)
    canvas.paragraph(lines, TEXT_X, 78.0, TEXT_WIDTH, 6)


def _draw_seal(canvas: _Canvas) -> None:
    """Lacre con el motivo, justo sobre el marco del QR y fuera de su zona de silencio."""
    palette = canvas.palette
    canvas.fill(palette.accent)
    canvas.dot(PAGE_WIDTH / 2, 100.5, 4.5)
    canvas.svg(motif_svg(palette.motif, 5, palette.card_bg), PAGE_WIDTH / 2 - 2.5, 98, 5, 5)


def _draw_qr(canvas: _Canvas, url: str) -> None:
    """QR vectorial: un rectángulo por cada tramo horizontal de módulos oscuros."""
    palette = canvas.palette
    dark, light = qr_colors(palette)
    frame_stroke = mix(palette.accent, light, 0.55) if palette.dark else palette.border
    canvas.fill(light)
    canvas.stroke(frame_stroke, 0.35)
    canvas.rounded(QR_FRAME_X, QR_FRAME_Y, QR_FRAME_SIZE, QR_FRAME_SIZE, 3, "DF")

    matrix = [list(row) for row in segno.make(url, error="m").matrix_iter(scale=1, border=0)]
    count = len(matrix)
    module = QR_SIZE / count
    x0 = QR_FRAME_X + (QR_FRAME_SIZE - QR_SIZE) / 2
    y0 = QR_FRAME_Y + (QR_FRAME_SIZE - QR_SIZE) / 2
    canvas.fill(dark)
    overlap = module * 0.02  # evita líneas claras entre módulos al rasterizar
    for row_index, row in enumerate(matrix):
        column = 0
        while column < count:
            if not row[column]:
                column += 1
                continue
            start = column
            while column < count and row[column]:
                column += 1
            canvas.pdf.rect(
                x0 + start * module,
                y0 + row_index * module,
                (column - start) * module + overlap,
                module + overlap,
                style="F",
            )


def _draw_link(canvas: _Canvas, url: str) -> None:
    palette = canvas.palette
    canvas.font("bevietnam", 8)
    canvas.ink(canvas.muted)
    canvas.text("Escanea el código con la cámara o abre el enlace", TEXT_X, 165.0, TEXT_WIDTH, 4.5)
    size, lines = canvas.fit("bevietnam", url, TEXT_WIDTH, 7, 6, 2)
    canvas.font("bevietnam", size)
    canvas.ink(palette.text)
    canvas.paragraph(lines, TEXT_X, 170.5, TEXT_WIDTH, 3.6)


def _draw_card_page(
    canvas: _Canvas, content: CardContent, sender: str | None, recipient: str, title: str, url: str
) -> None:
    canvas.pdf.add_page()
    _draw_sheet(canvas)
    _draw_ornament(canvas, 26.0)
    _draw_lockup(canvas, sender, recipient)
    _draw_title(canvas, title)
    _draw_seal(canvas)
    _draw_qr(canvas, url)
    _draw_link(canvas, url)
    _draw_ornament(canvas, 183.0)
    _draw_footer(canvas, content)


# --- Punto de entrada -------------------------------------------------------------------------


def render_card_pdf(content: CardContent) -> bytes:
    """Devuelve el PDF de la tarjeta. Síncrono y puro: no toca red ni disco."""
    from fpdf import FPDF  # noqa: PLC0415 - arrastra Pillow; no debe cargarse al arrancar

    palette = palette_for(content.theme)
    title = printable(content.title) or TITLE_FALLBACK
    recipient = printable(content.recipient_name) or RECIPIENT_FALLBACK
    sender = printable(content.sender_name or "") or None
    url = printable(content.public_url, limit=300)

    pdf = FPDF(orientation="P", unit="mm", format=(PAGE_WIDTH, PAGE_HEIGHT))
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)
    for family, style, name in FONTS:
        pdf.add_font(family, style, _font_path(name))
    pdf.set_fallback_fonts(["playfair", "bevietnam"])
    pdf.set_title(f"Tarjeta QR · {title}")
    pdf.set_lang("es")
    pdf.set_subject(f"Carta {content.letter_id} · versión {content.version}")
    pdf.set_author("Zyvencore")
    pdf.set_creator("zy-valentine-backend")

    canvas = _Canvas(pdf, palette)
    _draw_card_page(canvas, content, sender, recipient, title, url)
    return bytes(pdf.output())
