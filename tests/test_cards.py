"""El PDF de la tarjeta: la postal del tema centrada en una hoja A5.

El dibujo se prueba en ``test_postcard.py``. Aquí solo se comprueba el envoltorio: que
la hoja lleva la postal, que cabe con margen para recortarla, que los metadatos van en
español y que el archivo se llama de forma que no rompa una cabecera MIME.
"""

import re
from types import SimpleNamespace

import pytest

from app.services.cards import (
    CARD_WIDTH,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    CardContent,
    card_name,
    render_card_pdf,
)
from app.services.qr_card import metrics
from app.services.themes import THEMES

PAGE = re.compile(rb"/Type\s*/Page\b(?!s)")
URL = "https://frontend.example.com/carta/" + "x" * 22


def content(**changes) -> CardContent:
    defaults = {
        "title": "Para ti",
        "recipient_name": "Ana",
        "sender_name": "Sebastián",
        "note": "Eres mi lugar favorito.",
        "public_url": URL,
        "theme": "classic",
        "letter_id": "8c1f",
        "version": 1,
    }
    return CardContent(**{**defaults, **changes})


@pytest.mark.parametrize("slug", list(THEMES))
def test_happy_path_every_theme_fits_on_one_page(slug):
    pdf = render_card_pdf(content(theme=slug))

    assert pdf.startswith(b"%PDF-")
    assert len(PAGE.findall(pdf)) == 1
    assert len(pdf) < 600_000
    assert b"/Image" in pdf  # la postal va dentro, no se dibuja de nuevo


def test_the_postcard_is_centred_with_room_to_cut_it_out():
    """A 100 mm de ancho la postal son ~297 puntos por pulgada: calidad de imprenta."""
    design = metrics()
    height = CARD_WIDTH * design.height / design.width

    assert height < PAGE_HEIGHT - 20  # queda margen arriba y abajo
    assert CARD_WIDTH < PAGE_WIDTH - 40
    assert round(design.width * 4 / (CARD_WIDTH / 25.4)) > 280  # puntos por pulgada


def test_the_pdf_carries_spanish_metadata():
    pdf = render_card_pdf(content(title="Feliz aniversario"))

    assert b"/Lang (es)" in pdf
    assert b"/Title" in pdf and b"/Subject" in pdf


def test_exception_emojis_in_the_title_do_not_break_the_metadata():
    assert render_card_pdf(content(title="Para ti 💌🌹\x00")).startswith(b"%PDF-")


def test_an_unknown_theme_falls_back_to_classic():
    assert len(PAGE.findall(render_card_pdf(content(theme="no-existe")))) == 1


def test_card_name_is_safe_for_a_mime_header():
    name = card_name(SimpleNamespace(title='Para ti: "hola"/../x\r\n'))

    assert name.startswith("tarjeta-qr-") and name.endswith(".pdf")
    assert not set('/\\"\r\n:') & set(name)
    assert card_name(SimpleNamespace(title="💌")) == "tarjeta-qr-carta.pdf"
