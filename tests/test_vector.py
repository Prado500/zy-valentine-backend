"""Aplanado de los trazados del frontend.

Es la pieza que convierte un ``<path>`` en algo que Pillow sabe pintar, así que lo que
se comprueba es geométrico: que cada motivo cae donde dice su caja y que las piezas
cerradas salen cerradas. Si esto se tuerce, los adornos aparecen descolocados.
"""

import pytest

from app.services import vector
from app.services.qr_card import CORNER_ARC_OUTER, CORNER_LEAF
from app.services.themes import EMBLEM_WINGS, MOTIF_BOX, MOTIF_SHAPES


def all_contours(motif: str):
    shapes = []
    for shape in MOTIF_SHAPES[motif]:
        shapes.extend(vector.circle(*shape.circle) if shape.circle else vector.contours(shape.path))
    return tuple(shapes)


@pytest.mark.parametrize("motif", list(MOTIF_SHAPES))
def test_happy_path_every_motif_fits_inside_the_box_it_declares(motif):
    """La caja de cada motivo es la que lo centra en el emblema: si miente, se descoloca."""
    left, top, side = MOTIF_BOX[motif]
    min_x, min_y, max_x, max_y = vector.bounds(all_contours(motif))

    assert left <= min_x and top <= min_y
    assert max_x <= left + side and max_y <= top + side
    # Y la caja no es absurdamente grande: el motivo llena al menos media caja.
    assert max_x - min_x >= side * 0.5 and max_y - min_y >= side * 0.5


def test_the_tricky_paths_of_the_frontend_survive_the_parser():
    """Cúbicas suaves, comandos relativos y subtrazados: lo que rompería un parser casero."""
    assert len(vector.contours(MOTIF_SHAPES["petals"][0].path)) == 1  # S y c relativos
    assert len(vector.contours(MOTIF_SHAPES["butterfly"][0].path)) == 2  # dos alas, m y Z
    assert len(vector.contours(MOTIF_SHAPES["sun"][1].path)) == 8  # ocho rayos sueltos
    assert all(closed for _, closed in vector.contours(EMBLEM_WINGS[0]))


def test_open_and_closed_contours_are_told_apart():
    """Un arco abierto no se puede rellenar y una hoja cerrada sí: el pintor lo decide aquí."""
    assert [closed for _, closed in vector.contours(CORNER_ARC_OUTER)] == [False]
    assert [closed for _, closed in vector.contours(CORNER_LEAF)] == [True]


def test_place_scales_rotates_and_moves():
    square = ((((0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)), True),)

    scaled = vector.place(square, 3, 10, 20)
    assert scaled[0][0][0] == (10.0, 20.0) and scaled[0][0][2] == (16.0, 26.0)

    turned = vector.place(square, 1, 0, 0, rotate=90, center=(1, 1))
    corners = [(round(x, 6), round(y, 6)) for x, y in turned[0][0]]
    assert (2.0, 0.0) in corners and (0.0, 2.0) in corners


def test_the_flattening_is_cached_and_deterministic():
    first = vector.contours(CORNER_LEAF)

    assert vector.contours(CORNER_LEAF) is first  # misma tupla, no se recalcula
    assert vector.contours(CORNER_LEAF, steps=4) is not first
    assert len(vector.contours(CORNER_LEAF, steps=4)[0][0]) < len(first[0][0])


def test_a_circle_closes_on_itself():
    ((points, closed),) = vector.circle(10, 10, 5, steps=16)

    assert closed and len(points) == 16
    assert all(abs(((x - 10) ** 2 + (y - 10) ** 2) ** 0.5 - 5) < 1e-9 for x, y in points)
