"""La postal del QR, dibujada en el servidor.

Es la misma tarjeta que el comprador ve y descarga desde la app: papel del tema, trama,
filo, flores, filigranas en las esquinas, "P A R A" con el nombre de quien la recibe, la
primera frase de la carta, el código con el emblema alado en el centro y "D E" con la
firma. El frontend la pinta dos veces —en pantalla con HTML y en el PNG con canvas— y
aquí se pinta una tercera, con las mismas medidas de :mod:`app.services.qr_card`.

Un solo dibujo sirve para todo: el correo lleva la **baldosa** suelta (el código
estilizado, sin nombres) y el PDF lleva la postal entera. Así no pueden verse distintos.

Dos decisiones que explican el código:

- **Se dibuja a 4× y se reduce.** Pillow no antialiasa sus primitivas: un arco dibujado a
  tamaño natural sale con los bordes dentados. Reducir desde 4× con LANCZOS los suaviza.
- **El módulo del código mide un múltiplo entero de la escala.** Así, al reducir, cada
  módulo cae en un número exacto de píxeles y el código no se emborrona, que es lo único
  que haría que dejara de escanear.
"""

import io
import math
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

import segno
from PIL import Image, ImageDraw

from app.services import vector
from app.services.fonts import SCRIPT, SERIF, pil_font, printable
from app.services.qr_card import (
    CARD_FLOWERS,
    CORNER_ALPHA,
    CORNER_ANGLE,
    CORNER_ARC_INNER,
    CORNER_ARC_OUTER,
    CORNER_BOX,
    CORNER_DOT_RADIUS,
    CORNER_DOTS,
    CORNER_LEAF,
    CORNER_WIDTH,
    CORNERS,
    DEFAULT_NOTE,
    DEFAULT_RECIPIENT,
    DEFAULT_SENDER,
    FROM_LABEL,
    SCAN_LINE,
    TO_LABEL,
    Metrics,
    fit_note,
    metrics,
)
from app.services.themes import (
    EMBLEM_HIGHLIGHT_ALPHA,
    EMBLEM_HIGHLIGHTS,
    EMBLEM_MOTIF,
    EMBLEM_RADIUS,
    EMBLEM_RATIO,
    EMBLEM_SCALE,
    EMBLEM_WIDTH,
    EMBLEM_WINGS,
    MOTIF_SHAPES,
    STROKED_MOTIFS,
    Palette,
    hex_to_rgb,
    metal_ink,
    motif_transform,
    palette_for,
    qr_colors,
)

# Supermuestreo: se dibuja a este factor y se reduce. No bajarlo sin mirar los bordes.
SCALE = 4

# Zona de silencio del código, en módulos. Es la del estándar y la que usa el frontend.
QUIET_ZONE = 4

# Nivel de corrección: el emblema tapa módulos del centro, así que hace falta el más alto.
ERROR_LEVEL = "h"

FLOWER_DIR = files("app.assets.flowers")

# El número de carpeta es el tema, y **el orden no coincide** con el de la lista de
# estilos: la 3 es el atardecer y la 4 la noche estrellada. Ver el README de las flores.
FLOWER_FOLDER = {
    "classic": 1,
    "pastel-pink": 2,
    "sunset": 3,
    "starry": 4,
    "lavender": 5,
    "emerald": 6,
    "midnight": 7,
    "vintage": 8,
}

# Trama del papel: medidas en píxeles de diseño y opacidad de capa, copiadas de
# ``textureCss``. Las opacidades del color (0.07 y 0.05) las pone el pintor.
_TEXTURE_OPACITY = {
    "dots": 0.4,
    "stardust": 0.65,
    "grid": 0.45,
    "ruled": 0.7,
    "diagonal": 0.5,
    "weave": 0.5,
}


@dataclass(frozen=True, slots=True)
class PostcardContent:
    recipient_name: str
    sender_name: str | None
    note: str
    public_url: str
    theme: str


def _rgba(color: str, alpha: float = 1.0) -> tuple[int, int, int, int]:
    red, green, blue = hex_to_rgb(color)
    return red, green, blue, max(0, min(255, round(alpha * 255)))


@lru_cache(maxsize=8)
def flowers_for(slug: str) -> tuple[Image.Image, ...]:
    """Las tres flores del tema, ya decodificadas. Se cachean: pesan 13 kB cada una."""
    folder = FLOWER_FOLDER.get(slug, FLOWER_FOLDER["classic"])
    loaded = []
    for number in (1, 2, 3):
        data = FLOWER_DIR.joinpath(f"tema {folder}", f"flor_{number}.webp").read_bytes()
        image = Image.open(io.BytesIO(data))
        image.load()
        loaded.append(image.convert("RGBA"))
    return tuple(loaded)


def _fill(draw: ImageDraw.ImageDraw, shapes, color) -> None:
    for points, _ in shapes:
        if len(points) >= 3:
            draw.polygon(points, fill=color)


def _stroke(draw: ImageDraw.ImageDraw, shapes, color, width: float) -> None:
    line_width = max(1, round(width))
    for points, closed in shapes:
        path = [*points, points[0]] if closed else list(points)
        if len(path) >= 2:
            draw.line(path, fill=color, width=line_width, joint="curve")
        if line_width > 2 and not closed:
            # Remates redondos: `line` los deja a escuadra y los rayos del sol cantan.
            radius = line_width / 2
            for x, y in (path[0], path[-1]):
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)


def _draw_motif(draw, motif: str, color: str, scale: float, dx: float, dy: float) -> None:
    """Dibuja un motivo del tema ya colocado. ``scale/dx/dy`` van en píxeles de destino."""
    for shape in MOTIF_SHAPES[motif]:
        base = vector.circle(*shape.circle) if shape.circle else vector.contours(shape.path)
        placed = vector.place(base, scale, dx, dy)
        if shape.width or motif in STROKED_MOTIFS:
            _stroke(draw, placed, _rgba(color), max(shape.width, 3) * scale)
        else:
            _fill(draw, placed, _rgba(color))


def draw_emblem(draw, palette: Palette, left: float, top: float, width: float, light: str) -> None:
    """Emblema alado del centro del código: alas en el metal y motivo en el color del QR."""
    k = width / EMBLEM_WIDTH
    height = round(width * EMBLEM_RATIO)
    dark, _ = qr_colors(palette)
    draw.rounded_rectangle(
        (left, top, left + width - 1, top + height - 1),
        radius=EMBLEM_RADIUS * k,
        fill=_rgba(light),
    )
    for path in EMBLEM_WINGS:
        _fill(draw, vector.place(vector.contours(path), k, left, top), _rgba(palette.metal))
    for path in EMBLEM_HIGHLIGHTS:
        _fill(
            draw,
            vector.place(vector.contours(path), k, left, top),
            (255, 255, 255, round(255 * EMBLEM_HIGHLIGHT_ALPHA)),
        )
    size, cx, cy = EMBLEM_MOTIF
    motif_scale, motif_dx, motif_dy = motif_transform(palette.motif, size, cx, cy)
    _draw_motif(draw, palette.motif, dark, motif_scale * k, motif_dx * k + left, motif_dy * k + top)


@dataclass(frozen=True, slots=True)
class CodeGeometry:
    """Dónde cae cada módulo dentro del cuadro del código, y qué tapa el emblema."""

    module: int
    offset: int
    emblem_width: int
    emblem_height: int

    def module_center(self, index: int) -> float:
        """Centro del módulo ``index``, desde el borde del cuadro."""
        return self.offset + (index + 0.5) * self.module


def code_geometry(box: int, count: int, step: int) -> CodeGeometry:
    """Geometría del código: el módulo mide un múltiplo entero de ``step``.

    Lo que sobra se reparte como zona de silencio, que nunca estorba, y a cambio cada
    módulo cae en un número exacto de píxeles al reducir la imagen. Es lo único que
    separa un código nítido de uno que no escanea.
    """
    module = max(step, (box // count) // step * step)
    emblem_width = round(box / step * EMBLEM_SCALE) * step
    return CodeGeometry(
        module=module,
        offset=(box - module * count) // 2,
        emblem_width=emblem_width,
        emblem_height=round(emblem_width * EMBLEM_RATIO),
    )


def code_modules(url: str) -> list[list[int]]:
    """Matriz del código, con su zona de silencio incluida."""
    return [
        list(row)
        for row in segno.make(url, error=ERROR_LEVEL).matrix_iter(scale=1, border=QUIET_ZONE)
    ]


def excavate(matrix: list[list[int]], geometry: CodeGeometry) -> list[list[int]]:
    """Apaga los módulos que tapa el emblema, como hace ``excavate`` en el frontend.

    Si se dejaran encendidos, el emblema pintaría encima y el lector vería módulos
    oscuros donde el código dice claro. El nivel H tolera perder hasta un 30 % del
    código y el emblema se lleva un 7 %.
    """
    count = len(matrix)
    box = geometry.module * count + geometry.offset * 2
    left = (box - geometry.emblem_width) / 2 - geometry.offset
    top = (box - geometry.emblem_height) / 2 - geometry.offset
    columns = range(
        max(0, math.floor(left / geometry.module)),
        min(count, math.ceil((left + geometry.emblem_width) / geometry.module)),
    )
    rows = range(
        max(0, math.floor(top / geometry.module)),
        min(count, math.ceil((top + geometry.emblem_height) / geometry.module)),
    )
    for row in rows:
        for column in columns:
            matrix[row][column] = 0
    return matrix


def draw_code(draw, palette: Palette, url: str, left: int, top: int, box: int, step: int) -> None:
    """Dibuja el código dentro de un cuadrado de ``box`` píxeles, con su emblema encima."""
    dark, light = qr_colors(palette)
    matrix = code_modules(url)
    count = len(matrix)
    geometry = code_geometry(box, count, step)
    excavate(matrix, geometry)

    module = geometry.module
    origin_x = left + geometry.offset
    origin_y = top + geometry.offset
    emblem_left = left + (box - geometry.emblem_width) / 2
    emblem_top = top + (box - geometry.emblem_height) / 2

    fill = _rgba(dark)
    for row_index, row in enumerate(matrix):
        column = 0
        while column < count:
            if not row[column]:
                column += 1
                continue
            start = column
            while column < count and row[column]:
                column += 1
            draw.rectangle(
                (
                    origin_x + start * module,
                    origin_y + row_index * module,
                    origin_x + column * module - 1,
                    origin_y + (row_index + 1) * module - 1,
                ),
                fill=fill,
            )
    draw_emblem(draw, palette, emblem_left, emblem_top, geometry.emblem_width, light)


def draw_tile(draw, palette: Palette, m: Metrics, url: str, left: int, top: int, step: int) -> None:
    """Baldosa clara con el código: en los temas oscuros el papel es oscuro y un código
    invertido no lo lee la mitad de los lectores."""
    _, light = qr_colors(palette)
    side = m.tile * step
    draw.rounded_rectangle(
        (left, top, left + side - 1, top + side - 1),
        radius=m.tile_radius * step,
        fill=_rgba(light),
        outline=_rgba(palette.metal, 0.4),
        width=max(1, round(step)),
    )
    draw_code(
        draw, palette, url, left + m.tile_pad * step, top + m.tile_pad * step, m.qr * step, step
    )


class _Painter:
    """Pinta la postal sobre un lienzo a escala. El orden es el del canvas del frontend."""

    def __init__(self, palette: Palette, m: Metrics, scale: int, background: str):
        self.palette = palette
        self.m = m
        self.s = scale
        self.size = (m.width * scale, m.height * scale)
        self.background = background
        # Se pinta sobre un rectángulo completo y las esquinas se redondean al final con
        # esta silueta. Así nada —ni la trama, ni una flor girada— se sale del papel.
        self.image = Image.new("RGBA", self.size, _rgba(palette.card_bg))
        self.draw = ImageDraw.Draw(self.image, "RGBA")
        self.mask = Image.new("L", self.size, 0)
        ImageDraw.Draw(self.mask).rounded_rectangle(
            (0, 0, self.size[0] - 1, self.size[1] - 1), radius=m.radius * scale, fill=255
        )

    # --- papel ------------------------------------------------------------------------

    def paper(self) -> None:
        self._texture()

    def _speckle(self, draw, color, spacing: float, radius: float, offset: float = 0.0) -> None:
        step = spacing * self.s
        size = radius * self.s
        start = offset * step
        y = start
        while y < self.size[1]:
            x = start
            while x < self.size[0]:
                draw.ellipse((x - size, y - size, x + size, y + size), fill=color)
                x += step
            y += step

    def _hatch(self, draw, color, angle: float, spacing: float, width: float) -> None:
        step = spacing * self.s
        radians = math.radians(angle)
        direction = (math.cos(radians), math.sin(radians))
        normal = (-direction[1], direction[0])
        width_px, height_px = self.size
        corners = ((0, 0), (width_px, 0), (0, height_px), (width_px, height_px))
        projections = [x * normal[0] + y * normal[1] for x, y in corners]
        reach = width_px + height_px
        offset = min(projections)
        while offset <= max(projections):
            cx, cy = offset * normal[0], offset * normal[1]
            draw.line(
                (
                    cx - direction[0] * reach,
                    cy - direction[1] * reach,
                    cx + direction[0] * reach,
                    cy + direction[1] * reach,
                ),
                fill=color,
                width=max(1, round(width * self.s)),
            )
            offset += step

    def _texture(self) -> None:
        """Trama del papel, derivada del color de texto. Es casi invisible a propósito."""
        kind = self.palette.texture
        opacity = _TEXTURE_OPACITY.get(kind)
        if opacity is None:
            return
        layer = Image.new("RGBA", self.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer, "RGBA")
        soft = _rgba(self.palette.text, 0.07)
        faint = _rgba(self.palette.text, 0.05)
        if kind == "dots":
            self._speckle(draw, soft, spacing=11, radius=0.5)
        elif kind == "stardust":
            self._speckle(draw, soft, spacing=23, radius=0.7)
            self._speckle(draw, faint, spacing=13, radius=0.5, offset=0.5)
        elif kind == "grid":
            self._hatch(draw, faint, angle=0, spacing=16, width=1)
            self._hatch(draw, faint, angle=90, spacing=16, width=1)
        elif kind == "ruled":
            self._hatch(draw, faint, angle=0, spacing=22, width=1)
        elif kind == "diagonal":
            self._hatch(draw, faint, angle=115, spacing=9, width=1)
        elif kind == "weave":
            self._hatch(draw, faint, angle=45, spacing=6, width=1)
            self._hatch(draw, faint, angle=-45, spacing=6, width=1)
        alpha = layer.getchannel("A").point(lambda value: round(value * opacity))
        layer.putalpha(alpha)
        self.image.paste(layer, (0, 0), layer)

    def edge(self) -> None:
        """Filo interior: el filete del tema y, por dentro, su sombra."""
        m, s = self.m, self.s
        metal = self.palette.metal
        inset = round(m.pad_x * 0.42) * s
        self.draw.rounded_rectangle(
            (inset, inset, self.size[0] - inset - 1, self.size[1] - inset - 1),
            radius=m.radius * 0.7 * s,
            outline=_rgba(metal, 0.5 if self.palette.edge != "plain" else 0.45),
            width=max(1, round(s)),
        )
        second = inset + round(3.5 * s)
        self.draw.rounded_rectangle(
            (second, second, self.size[0] - second - 1, self.size[1] - second - 1),
            radius=m.radius * 0.6 * s,
            outline=_rgba(metal, 0.18),
            width=max(1, round(2.5 * s)),
        )

    def flowers(self) -> None:
        petals = flowers_for(self.palette.slug)
        if not petals:
            return
        m, s = self.m, self.s
        for flower in CARD_FLOWERS:
            source = petals[flower.pick % len(petals)]
            width = max(1, round(m.width * flower.size) * s)
            height = max(1, round(width * source.height / source.width))
            scaled = source.resize((width, height), Image.LANCZOS)
            faded = scaled.getchannel("A").point(lambda value, a=flower.alpha: round(value * a))
            scaled.putalpha(faded)
            # El giro de CSS es horario; el de Pillow, antihorario.
            turned = scaled.rotate(-flower.rot, expand=True, resample=Image.BICUBIC)
            cx = m.width * flower.x * s + width / 2
            cy = m.height * flower.y * s + height / 2
            self.image.paste(
                turned, (round(cx - turned.width / 2), round(cy - turned.height / 2)), turned
            )

    def corners(self) -> None:
        m, s = self.m, self.s
        metal = self.palette.metal
        k = (m.corner * s) / CORNER_BOX
        center = (CORNER_BOX / 2, CORNER_BOX / 2)
        for corner in CORNERS:
            x = m.corner_inset if corner in ("tl", "bl") else m.width - m.corner_inset - m.corner
            y = m.corner_inset if corner in ("tl", "tr") else m.height - m.corner_inset - m.corner
            dx, dy, angle = x * s, y * s, CORNER_ANGLE[corner]
            for path, key in (
                (CORNER_ARC_OUTER, "outer"),
                (CORNER_ARC_INNER, "inner"),
                (CORNER_LEAF, "leaf"),
            ):
                shapes = vector.place(vector.contours(path), k, dx, dy, rotate=angle, center=center)
                _stroke(self.draw, shapes, _rgba(metal, CORNER_ALPHA[key]), CORNER_WIDTH[key] * k)
            for cx, cy in CORNER_DOTS:
                dot = vector.place(
                    vector.circle(cx, cy, CORNER_DOT_RADIUS), k, dx, dy, rotate=angle, center=center
                )
                _fill(self.draw, dot, _rgba(metal, CORNER_ALPHA["dot"]))

    # --- texto ------------------------------------------------------------------------

    def _fit(self, family: str, text: str, size: int, minimum: int, max_width: float):
        """Baja el cuerpo hasta que el texto quepa. Un nombre largo no debe desbordar."""
        font = pil_font(family, size)
        while size > minimum and self.draw.textlength(text, font=font) > max_width:
            size -= max(1, self.s // 2)
            font = pil_font(family, size)
        return font

    def line(self, text: str, top: float, height: float, font, color) -> None:
        """Centra el texto dentro de la banda que tiene reservada.

        No se coloca por la línea base: la letra manuscrita tiene rasgos descendentes
        enormes y con la base fija se comería la línea de abajo o se saldría por el borde.
        """
        bbox = self.draw.textbbox((0, 0), text, font=font, anchor="ls")
        ascent, descent = -bbox[1], bbox[3]
        baseline = top + (height - (ascent + descent)) / 2 + ascent
        self.draw.text((self.size[0] / 2, baseline), text, font=font, fill=color, anchor="ms")

    def content(self, content: PostcardContent) -> None:
        m, s = self.m, self.s
        palette = self.palette
        ink = _rgba(palette.text, 0.6)
        names = _rgba(metal_ink(palette))
        inner = (m.width - m.pad_x * 2) * s

        recipient = printable(content.recipient_name, 80) or DEFAULT_RECIPIENT
        sender = printable(content.sender_name or "", 80) or DEFAULT_SENDER
        note = printable(content.note, 120) or DEFAULT_NOTE

        y = m.pad_top * s
        self.line(TO_LABEL, y, m.name_label * s, pil_font(SERIF, m.name_label * s), ink)
        y += m.name_label * s
        self.line(
            recipient,
            y,
            m.name_line * s,
            self._fit(SCRIPT, recipient, m.name_size * s, 16 * s, inner),
            names,
        )
        y += (m.name_line + m.gap_after_name) * s

        note_font = pil_font(SERIF, m.note_size * s)
        trimmed = fit_note(note, inner, lambda text: self.draw.textlength(text, font=note_font))
        body = _rgba(palette.text, 0.8)
        self.line(trimmed, y, m.note_line * s, note_font, body)
        self.line(SCAN_LINE, y + m.note_line * s, m.note_line * s, note_font, body)
        y += (m.note_line * 2 + m.gap_after_note) * s

        draw_tile(self.draw, palette, m, content.public_url, m.pad_x * s, round(y), s)
        y += (m.tile + m.gap_after_qr) * s

        self.line(FROM_LABEL, y, m.from_label * s, pil_font(SERIF, m.from_label * s), ink)
        y += m.from_label * s
        self.line(
            sender,
            y,
            m.from_line * s,
            self._fit(SCRIPT, sender, m.from_size * s, 14 * s, inner),
            names,
        )

    def output(self, width: int | None) -> bytes:
        image = Image.new("RGB", self.size, _rgba(self.background)[:3])
        image.paste(self.image, (0, 0), self.mask)
        if width and width != image.width:
            height = round(image.height * width / image.width)
            image = image.resize((width, height), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "PNG", optimize=True)
        return buffer.getvalue()


def render_postcard(
    content: PostcardContent, width: int | None = None, scale: int = SCALE
) -> bytes:
    """PNG de la postal completa. Sin ``width`` sale a ``scale`` veces el tamaño de diseño."""
    palette = palette_for(content.theme)
    m = metrics()
    painter = _Painter(palette, m, scale, palette.bg)
    painter.paper()
    painter.edge()
    painter.flowers()
    painter.corners()
    painter.content(content)
    return painter.output(width)


def render_qr_tile(
    theme: str | None, url: str, width: int | None = None, scale: int = SCALE
) -> bytes:
    """PNG de la baldosa suelta: el código estilizado, sin nombres. Es lo que va al correo.

    El fondo es el papel del tema porque el correo la pone sobre esa misma tarjeta: así
    las esquinas redondeadas no dejan ver un cuadrado blanco alrededor.
    """
    palette = palette_for(theme)
    m = metrics()
    side = m.tile * scale
    image = Image.new("RGB", (side, side), _rgba(palette.card_bg)[:3])
    draw = ImageDraw.Draw(image, "RGBA")
    draw_tile(draw, palette, m, url, 0, 0, scale)
    if width and width != side:
        image = image.resize((width, width), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()
