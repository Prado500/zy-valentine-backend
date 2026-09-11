"""Trazados SVG del frontend convertidos en polígonos que Pillow sabe pintar.

Los adornos de la postal —las alas del emblema, las enredaderas de las esquinas y los
ocho motivos— viven en el frontend como cadenas de ``path``. Copiarlos es trivial;
dibujarlos, no: Pillow no entiende SVG ni curvas.

Aquí se aplanan con ``fontTools`` (que ya llega con fpdf2, y es el mismo parser que usa
fpdf2 para su propio SVG) en vez de escribir un intérprete de trazados a mano: los
motivos del frontend usan comandos relativos, cúbicas suaves y repeticiones implícitas, y
un parser casero fallaría justo en el que no se probó.

Todo son funciones puras sobre tuplas, y el aplanado se cachea: los mismos ocho motivos
se dibujan en cada postal.
"""

import math
from functools import lru_cache

Point = tuple[float, float]
# (puntos, cerrado). Un trazado puede tener varios: la mariposa son dos alas.
Contour = tuple[tuple[Point, ...], bool]

# Segmentos por curva. Doce bastan para un motivo de 48 px dibujado a 4×: por encima solo
# se gastan puntos que luego caen en el mismo píxel.
STEPS = 12


def _cubic(start: Point, one: Point, two: Point, end: Point, steps: int) -> list[Point]:
    points = []
    for index in range(1, steps + 1):
        t = index / steps
        u = 1 - t
        points.append(
            (
                u * u * u * start[0] + 3 * u * u * t * one[0] + 3 * u * t * t * two[0] + t**3 * end[0],
                u * u * u * start[1] + 3 * u * u * t * one[1] + 3 * u * t * t * two[1] + t**3 * end[1],
            )
        )
    return points


def _quadratic(start: Point, control: Point, end: Point, steps: int) -> list[Point]:
    points = []
    for index in range(1, steps + 1):
        t = index / steps
        u = 1 - t
        points.append(
            (
                u * u * start[0] + 2 * u * t * control[0] + t * t * end[0],
                u * u * start[1] + 2 * u * t * control[1] + t * t * end[1],
            )
        )
    return points


@lru_cache(maxsize=64)
def contours(path: str, steps: int = STEPS) -> tuple[Contour, ...]:
    """Aplana un trazado SVG en contornos de puntos."""
    from fontTools.pens.recordingPen import RecordingPen  # noqa: PLC0415
    from fontTools.svgLib.path import parse_path  # noqa: PLC0415

    pen = RecordingPen()
    parse_path(path, pen)

    out: list[Contour] = []
    current: list[Point] = []
    for operation, arguments in pen.value:
        if operation == "moveTo":
            if current:
                out.append((tuple(current), False))
            current = [arguments[0]]
        elif operation == "lineTo":
            current.append(arguments[0])
        elif operation == "curveTo":
            current.extend(_cubic(current[-1], *arguments, steps))
        elif operation == "qCurveTo":
            # La forma implícita (varios controles seguidos) no aparece en estos trazados.
            points = [point for point in arguments if point is not None]
            current.extend(_quadratic(current[-1], points[0], points[-1], steps))
        elif operation == "closePath" and current:
            out.append((tuple(current), True))
            current = []
    if current:
        out.append((tuple(current), False))
    return tuple(out)


def circle(cx: float, cy: float, radius: float, steps: int = 48) -> tuple[Contour, ...]:
    """Círculo como contorno, para los motivos que traen un ``<circle>`` (el sol)."""
    points = tuple(
        (cx + radius * math.cos(2 * math.pi * index / steps), cy + radius * math.sin(2 * math.pi * index / steps))
        for index in range(steps)
    )
    return ((points, True),)


def place(
    shapes: tuple[Contour, ...],
    scale: float = 1.0,
    dx: float = 0.0,
    dy: float = 0.0,
    rotate: float = 0.0,
    center: Point = (0.0, 0.0),
) -> list[tuple[list[Point], bool]]:
    """Escala, gira sobre ``center`` y traslada. El giro va en grados, como en el SVG."""
    angle = math.radians(rotate)
    cos, sin = math.cos(angle), math.sin(angle)
    placed = []
    for points, closed in shapes:
        moved = []
        for x, y in points:
            if rotate:
                ox, oy = x - center[0], y - center[1]
                x, y = center[0] + ox * cos - oy * sin, center[1] + ox * sin + oy * cos
            moved.append((x * scale + dx, y * scale + dy))
        placed.append((moved, closed))
    return placed


def bounds(shapes: tuple[Contour, ...]) -> tuple[float, float, float, float]:
    xs = [point[0] for points, _ in shapes for point in points]
    ys = [point[1] for points, _ in shapes for point in points]
    return min(xs), min(ys), max(xs), max(ys)
