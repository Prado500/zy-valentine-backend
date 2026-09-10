"""Tarjeta QR en PDF: los ocho temas, textos hostiles y el contrato del archivo.

No se rasteriza nada: se comprueba la estructura del PDF (páginas, metadatos,
tamaño), que ningún glifo ausente llegue a la fuente y, espiando el lienzo, qué
textos se dibujan. El aspecto se revisa a ojo con el script de humo del plan.
"""

import logging
import re
from types import SimpleNamespace

import pytest
import segno

from app.services import cards
from app.services.cards import (
    CardContent,
    card_name,
    printable,
    render_card_pdf,
    supported_codepoints,
)
from app.services.themes import THEMES

PAGE = re.compile(rb"/Type\s*/Page\b(?!s)")
URL = "https://frontend.example.com/carta/" + "x" * 22


def content(**changes) -> CardContent:
    defaults = {
        "title": "Para ti",
        "recipient_name": "Ana",
        "sender_name": "Sebastián",
        "public_url": URL,
        "theme": "classic",
        "letter_id": "8c1f",
        "version": 1,
    }
    return CardContent(**{**defaults, **changes})


@pytest.fixture
def drawn(monkeypatch) -> list[str]:
    """Textos que el lienzo dibuja, en orden. Espía casero: no depende de pytest-mock."""
    texts: list[str] = []
    original = cards._Canvas.text

    def record(self, text, *args, **kwargs):
        texts.append(text)
        return original(self, text, *args, **kwargs)

    monkeypatch.setattr(cards._Canvas, "text", record)
    return texts


def glyph_warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("fpdf") and "missing" in record.getMessage().lower()
    ]


@pytest.mark.parametrize("slug", list(THEMES))
def test_happy_path_every_theme_renders_two_pages_within_budget(slug, caplog):
    with caplog.at_level(logging.WARNING):
        pdf = render_card_pdf(content(theme=slug))

    assert pdf.startswith(b"%PDF-")
    assert len(PAGE.findall(pdf)) == 2
    assert len(pdf) < 400_000
    assert not glyph_warnings(caplog)
    assert not [r for r in caplog.records if r.name.startswith("fpdf.svg")]  # SVG entendido


def test_sad_path_emojis_and_controls_never_reach_the_font(caplog):
    with caplog.at_level(logging.WARNING):
        pdf = render_card_pdf(
            content(
                title="Para ti 💌🌹, mi amor\x00",
                recipient_name="Ana 🥰",
                sender_name="Seb‍astián ",
            )
        )

    assert pdf.startswith(b"%PDF-")
    assert not glyph_warnings(caplog)
    assert printable("Para ti 💌, mi amor") == "Para ti, mi amor"
    assert printable("Seb‍astián") == "Sebastián"
    assert printable("💌💌") == ""
    assert printable("x" * 500, limit=10) == "x" * 10


def test_edge_long_texts_are_fitted_instead_of_overflowing(drawn):
    pdf = render_card_pdf(
        content(
            title="Un título larguísimo " * 6,
            recipient_name="Ana " * 30,
            sender_name="Sebastián " * 30,
            public_url=URL + "?utm=" + "y" * 120,
        )
    )

    assert len(PAGE.findall(pdf)) == 2
    assert any(text.endswith("…") for text in drawn)  # algo se recortó con elipsis


def test_edge_without_sender_the_lockup_reads_con_carino_para(drawn):
    render_card_pdf(content(sender_name=None))
    assert "Con cariño para" in drawn and "De" not in drawn and "Sebastián" not in drawn

    drawn.clear()
    render_card_pdf(content(sender_name="Sebastián"))
    assert "De" in drawn and "Sebastián" in drawn and "con cariño para" in drawn


def test_edge_blank_names_use_the_fallback_copy(drawn):
    render_card_pdf(content(title="💌", recipient_name="  ", sender_name="🌹"))

    assert cards.TITLE_FALLBACK in drawn and cards.RECIPIENT_FALLBACK in drawn
    assert "De" not in drawn  # una firma que era solo un emoji no es una firma


def test_the_qr_encodes_the_public_url_with_the_expected_correction(monkeypatch):
    calls: list[tuple] = []
    original = segno.make

    def record(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(segno, "make", record)
    render_card_pdf(content())

    assert calls == [((URL,), {"error": "m"})]


def test_the_pdf_carries_language_and_title_metadata():
    pdf = render_card_pdf(content(title="Feliz aniversario"))

    assert b"/Lang (es)" in pdf
    assert b"/Title" in pdf and b"/Subject" in pdf


def test_exception_an_unknown_theme_falls_back_to_classic(caplog):
    with caplog.at_level(logging.WARNING):
        pdf = render_card_pdf(content(theme="no-existe"))

    assert pdf.startswith(b"%PDF-")
    assert not glyph_warnings(caplog)


def test_card_name_is_safe_for_a_mime_header():
    name = card_name(SimpleNamespace(title='Para ti: "hola"/../x\r\n'))

    assert name.startswith("tarjeta-qr-") and name.endswith(".pdf")
    assert not set('/\\"\r\n:') & set(name)
    assert card_name(SimpleNamespace(title="💌")) == "tarjeta-qr-carta.pdf"


def test_the_bundled_fonts_cover_spanish():
    assert all(ord(character) in supported_codepoints() for character in "áéíóúñÑ¿¡üÁÉ…·")
