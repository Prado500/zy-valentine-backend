"""La postal del QR dibujada en el servidor.

Lo que de verdad importa aquí es que **el código escanee**: todo lo demás es adorno, y
un adorno feo se corrige; un código ilegible deja al comprador sin regalo. Por eso se
comprueban tres cosas encadenadas: que la matriz dibujada es la que segno calculó, que
el emblema tapa solo lo que la corrección de errores puede recuperar, y —cuando la
máquina trae un lector— que un decodificador de verdad lee la URL.

El resto de las pruebas son de contrato y de color, no de forma: comparar píxeles con una
imagen de referencia haría fallar la suite cada vez que alguien mueva una flor.
"""

import io
import shutil
import subprocess

import pytest
from PIL import Image

from app.services.postcard import (
    FLOWER_FOLDER,
    SCALE,
    PostcardContent,
    code_geometry,
    code_modules,
    excavate,
    flowers_for,
    render_postcard,
    render_qr_tile,
)
from app.services.qr_card import DEFAULT_NOTE, DEFAULT_RECIPIENT, DEFAULT_SENDER, metrics
from app.services.themes import THEMES, hex_to_rgb, qr_colors

URL = "https://frontend.example.com/carta/" + "x" * 22
SLUGS = list(THEMES)


def content(**changes) -> PostcardContent:
    defaults = {
        "recipient_name": "Ana María",
        "sender_name": "Sebastián",
        "note": "Eres mi lugar favorito.",
        "public_url": URL,
        "theme": "classic",
    }
    return PostcardContent(**{**defaults, **changes})


def opened(png: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(png))
    image.load()
    return image


def decode(png: bytes, tmp_path) -> str:
    """Lee el código con zbar. Es la única prueba que mira la imagen como un lector."""
    path = tmp_path / "code.png"
    path.write_bytes(png)
    done = subprocess.run(
        ["zbarimg", "-q", "--raw", str(path)], capture_output=True, text=True, check=False
    )
    return done.stdout.strip()


needs_zbar = pytest.mark.skipif(
    shutil.which("zbarimg") is None, reason="zbarimg no está instalado en esta máquina"
)


# --- El código -------------------------------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_the_drawn_code_is_module_for_module_the_one_segno_computed(slug):
    """Se muestrea el centro de cada módulo y se compara con la matriz esperada.

    Es la prueba que sobrevive sin lector instalado: si un módulo se corre medio píxel o
    el excavado se come uno de más, aquí se ve.
    """
    palette = THEMES[slug]
    m = metrics()
    image = opened(render_qr_tile(slug, URL))
    dark, light = (hex_to_rgb(color) for color in qr_colors(palette))

    matrix = code_modules(URL)
    geometry = code_geometry(m.qr * SCALE, len(matrix), SCALE)
    excavate(matrix, geometry)
    origin = m.tile_pad * SCALE + geometry.offset

    for row, cells in enumerate(matrix):
        for column, cell in enumerate(cells):
            x = origin + column * geometry.module + geometry.module // 2
            y = origin + row * geometry.module + geometry.module // 2
            pixel = image.getpixel((x, y))[:3]
            if cell:
                assert pixel == dark, f"módulo ({row}, {column}) debería ser oscuro"
            elif pixel != light:
                # Solo el emblema puede pintar sobre un módulo apagado.
                assert geometry.emblem_width  # el resto del claro es del papel del código


def test_the_emblem_covers_far_less_than_the_error_correction_tolerates():
    """Nivel H recupera el 30 %. El emblema se lleva un 7 %: queda margen para una mancha."""
    matrix = code_modules(URL)
    total = sum(sum(row) for row in matrix)
    geometry = code_geometry(metrics().qr * SCALE, len(matrix), SCALE)

    lost = total - sum(sum(row) for row in excavate(matrix, geometry))

    assert 0 < lost / total < 0.12


def test_the_module_is_a_whole_multiple_of_the_scale():
    """Si no, al reducir la imagen los módulos caen a medio píxel y el código se emborrona."""
    geometry = code_geometry(metrics().qr * SCALE, len(code_modules(URL)), SCALE)

    assert geometry.module % SCALE == 0
    assert geometry.offset >= 0


@needs_zbar
@pytest.mark.parametrize("slug", SLUGS)
def test_a_real_reader_opens_the_letter_from_the_tile(slug, tmp_path):
    assert decode(render_qr_tile(slug, URL), tmp_path) == URL


@needs_zbar
@pytest.mark.parametrize("slug", SLUGS)
def test_a_real_reader_opens_the_letter_from_the_postcard(slug, tmp_path):
    assert decode(render_postcard(content(theme=slug)), tmp_path) == URL


@needs_zbar
def test_the_code_survives_being_shrunk_to_its_design_size(tmp_path):
    """La postal se dibuja a 4× y se reduce: es donde un código mal alineado se rompe."""
    m = metrics()

    assert decode(render_postcard(content(), width=m.width), tmp_path) == URL
    assert decode(render_qr_tile("emerald", URL, width=m.tile), tmp_path) == URL


# --- La tarjeta ------------------------------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_happy_path_every_theme_draws_its_postcard(slug):
    m = metrics()
    png = render_postcard(content(theme=slug))
    image = opened(png)

    assert image.size == (m.width * SCALE, m.height * SCALE)
    assert len(png) < 400_000
    # Las esquinas quedan fuera de la tarjeta redondeada: se ve el fondo del tema.
    assert image.getpixel((0, 0))[:3] == hex_to_rgb(THEMES[slug].bg)
    # Y el centro del papel es el papel, no el fondo.
    assert image.getpixel((image.width // 2, 30 * SCALE))[:3] == hex_to_rgb(THEMES[slug].card_bg)


def test_the_postcard_can_be_asked_at_its_design_size():
    m = metrics()

    assert opened(render_postcard(content(), width=m.width)).size == (m.width, m.height)


@pytest.mark.parametrize("slug", SLUGS)
def test_the_tile_is_square_and_sits_on_the_theme_paper(slug):
    """El correo la pone sobre esa misma tarjeta: si el fondo no casa, se ve un recuadro."""
    image = opened(render_qr_tile(slug, URL, width=464))

    assert image.size == (464, 464)
    assert image.getpixel((0, 0))[:3] == hex_to_rgb(THEMES[slug].card_bg)


def test_edge_a_letter_without_sender_or_note_uses_the_viewer_fallbacks(monkeypatch):
    drawn: list[str] = []
    from app.services import postcard

    original = postcard._Painter.line

    def record(self, text, *args, **kwargs):
        drawn.append(text)
        return original(self, text, *args, **kwargs)

    monkeypatch.setattr(postcard._Painter, "line", record)
    render_postcard(content(sender_name=None, recipient_name="  ", note=""))

    assert DEFAULT_SENDER in drawn and DEFAULT_RECIPIENT in drawn and DEFAULT_NOTE in drawn


def test_edge_long_names_and_notes_are_fitted_instead_of_overflowing(monkeypatch):
    drawn: list[str] = []
    from app.services import postcard

    original = postcard._Painter.line
    monkeypatch.setattr(
        postcard._Painter,
        "line",
        lambda self, text, *a, **k: (drawn.append(text), original(self, text, *a, **k))[1],
    )
    png = render_postcard(
        content(
            recipient_name="Ana " * 30,
            sender_name="Sebastián " * 30,
            note="una nota larguísima que no cabe de ninguna manera en una sola línea " * 3,
        )
    )

    assert opened(png).size == (metrics().width * SCALE, metrics().height * SCALE)
    assert any(text.endswith("…") for text in drawn)  # la nota se recortó


def test_exception_emojis_and_control_characters_never_reach_the_font():
    """Las fuentes no traen emojis: sin filtrar saldrían cuadros vacíos."""
    png = render_postcard(content(recipient_name="Ana 🥰\x00", sender_name="💌", note="🌹🌹"))

    assert opened(png).size[0] > 0  # no revienta
    # Una firma que era solo un emoji no es una firma.
    assert render_postcard(content(sender_name="💌")) != render_postcard(content(sender_name="Ana"))


def test_an_unknown_theme_falls_back_to_classic():
    assert render_postcard(content(theme="no-existe")) == render_postcard(content(theme="classic"))


# --- Las flores ------------------------------------------------------------------------


def test_the_flower_folders_are_not_in_the_order_of_the_themes():
    """La trampa del juego de flores: la carpeta 3 es el atardecer, no la noche."""
    assert FLOWER_FOLDER["sunset"] == 3
    assert FLOWER_FOLDER["starry"] == 4
    assert set(FLOWER_FOLDER) == set(THEMES)


@pytest.mark.parametrize("slug", SLUGS)
def test_every_theme_has_its_three_flowers(slug):
    petals = flowers_for(slug)

    assert len(petals) == 3
    assert all(flower.mode == "RGBA" and flower.width > 100 for flower in petals)
