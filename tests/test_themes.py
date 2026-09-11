"""Catálogo de temas: paletas del front, colores del QR y consejos por estilo."""

import pytest

from app.services.gift_tips import GENERAL_MESSAGE, HOW_TO_DELIVER, TIPS, tips_for
from app.services.themes import (
    EMBLEM_HIGHLIGHTS,
    EMBLEM_MOTIF,
    EMBLEM_RATIO,
    EMBLEM_SCALE,
    EMBLEM_WINGS,
    MIN_QR_CONTRAST,
    MOTIF_BOX,
    MOTIF_SHAPES,
    THEMES,
    contrast_ratio,
    darken_until_contrast,
    metal_ink,
    mix,
    motif_transform,
    palette_for,
    qr_colors,
    relative_luminance,
    text_on,
)

SLUGS = ["classic", "pastel-pink", "starry", "sunset", "lavender", "emerald", "midnight", "vintage"]

# Calculados con el algoritmo del front (`buildQrPalette`): son el contrato visual.
EXPECTED_QR = {
    "classic": ("#a20513", "#fef8fa"),
    "pastel-pink": ("#9e1432", "#fff5f7"),
    "starry": ("#644c0e", "#ffffff"),
    "sunset": ("#8c3507", "#fff7ed"),
    "lavender": ("#632ebe", "#f5f3ff"),
    "emerald": ("#0d622c", "#f0fdf4"),
    "midnight": ("#1c5f7c", "#ffffff"),  # 94.5 sube a 95 (Math.round), no baja a 94
    "vintage": ("#873e07", "#faf5ef"),
}


def test_the_catalog_matches_the_frontend_presets():
    assert list(THEMES) == SLUGS
    assert THEMES["emerald"].name == "Jardín Esmeralda"
    assert THEMES["emerald"].accent == "#16a34a"
    assert [slug for slug in SLUGS if THEMES[slug].dark] == ["starry", "midnight"]
    for palette in THEMES.values():
        assert palette.motif in MOTIF_SHAPES and palette.motif in MOTIF_BOX
        assert palette.texture in {"dots", "weave", "stardust", "diagonal", "grid", "ruled"}
        assert palette.edge in {"double", "dashed", "plain"}


@pytest.mark.parametrize(
    ("theme", "slug"),
    [
        (None, "classic"),
        ("", "classic"),
        ("no-existe", "classic"),
        ("pastel-pink", "pastel-pink"),
        ("pastelPink", "pastel-pink"),  # id del preset sin pasar por themeSlug
        ("PASTEL_PINK", "pastel-pink"),
        (" emerald ", "emerald"),
    ],
)
def test_palette_for_normalizes_and_falls_back_to_classic(theme, slug):
    assert palette_for(theme).slug == slug


@pytest.mark.parametrize("slug", SLUGS)
def test_qr_colors_follow_the_frontend_rule_and_scan(slug):
    dark, light = qr_colors(THEMES[slug])

    assert (dark, light) == EXPECTED_QR[slug]
    assert contrast_ratio(dark, light) >= MIN_QR_CONTRAST
    if THEMES[slug].dark:
        assert light == "#ffffff"  # nunca un QR invertido sobre fondo oscuro


def test_color_math_matches_the_frontend_helpers():
    assert round(relative_luminance("#ffffff"), 4) == 1.0
    assert round(relative_luminance("#000000"), 4) == 0.0
    assert round(contrast_ratio("#000000", "#ffffff"), 2) == 21.0
    assert darken_until_contrast("#a20513", "#fef8fa", 7) == "#a20513"  # ya cumple
    assert darken_until_contrast("#ffffff", "#ffffff", 21) == "#000000"  # ni así se llega
    assert mix("#000000", "#ffffff", 0.5) == "#808080"  # .5 sube, como Math.round
    assert mix("#102030", "#ffffff", 0) == "#102030"
    assert text_on("#fbbf24") == "#111111"
    assert text_on("#a20513") == "#ffffff"


@pytest.mark.parametrize("motif", list(MOTIF_SHAPES))
def test_every_motif_declares_what_is_filled_and_what_is_stroked(motif):
    """El pintor necesita saberlo: un trazo relleno sale como un borrón."""
    shapes = MOTIF_SHAPES[motif]

    assert shapes
    for shape in shapes:
        assert bool(shape.path) != bool(shape.circle)  # o trazado o círculo, nunca ambos
        assert shape.width >= 0
    # Solo la hoja y los rayos del sol se trazan; el resto se rellenan.
    stroked = [shape for shape in shapes if shape.width]
    assert bool(stroked) is (motif in {"leaf", "sun"})


def test_motif_transform_centers_the_motif_where_it_is_asked():
    """Coloca el motivo dentro del emblema: si se descuadra, el código queda torcido."""
    size, cx, cy = EMBLEM_MOTIF
    for motif, (left, top, box) in MOTIF_BOX.items():
        scale, dx, dy = motif_transform(motif, size, cx, cy)

        assert scale == size / box
        # La caja del motivo, ya colocada, queda centrada en (cx, cy).
        assert round(left * scale + dx + size / 2, 6) == cx
        assert round(top * scale + dy + size / 2, 6) == cy


def test_the_winged_emblem_is_small_enough_for_the_error_correction():
    """Tapa módulos del código: con el nivel H se puede perder hasta un 30 % del área."""
    area = EMBLEM_SCALE * (EMBLEM_SCALE * EMBLEM_RATIO)

    assert area < 0.10
    assert len(EMBLEM_WINGS) == 2 and len(EMBLEM_HIGHLIGHTS) == 2


@pytest.mark.parametrize("slug", SLUGS)
def test_the_names_are_legible_over_the_paper(slug):
    """El metal puro da ~2:1 sobre un papel claro y los nombres se perdían."""
    palette = THEMES[slug]
    ink = metal_ink(palette)

    if palette.dark:
        assert ink == palette.metal  # el papel ya es oscuro
    else:
        assert contrast_ratio(ink, palette.card_bg) >= 3


@pytest.mark.parametrize("slug", SLUGS)
def test_every_theme_has_its_own_gift_tips(slug):
    tips = tips_for(slug)

    assert tips is TIPS[slug]
    assert tips.flowers and tips.sweets and tips.touch
    assert tips.flowers.endswith(".") and tips.sweets.endswith(".")


def test_unknown_themes_get_the_classic_tips_and_the_general_copy_exists():
    assert tips_for("no-existe") is TIPS["classic"]
    assert tips_for("pastelPink") is TIPS["pastel-pink"]
    assert GENERAL_MESSAGE and len(HOW_TO_DELIVER) == 2
