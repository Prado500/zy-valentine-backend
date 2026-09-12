"""Tarjeta QR imprimible: la postal del tema, centrada en una hoja A5.

El dibujo no vive aquí. La postal se pinta una sola vez en
:mod:`app.services.postcard`, y este módulo se limita a ponerla en un papel con el fondo
del tema y un pie discreto. Así la tarjeta del correo, la del PDF y la que el comprador
descarga desde la app son exactamente la misma imagen; antes el PDF se dibujaba aparte
con trazos vectoriales y cualquier retoque del frontend lo dejaba atrás.

La postal se coloca a 100 mm de ancho a partir de un PNG de 1168 px, o sea unos 297
puntos por pulgada: calidad de imprenta con el margen suficiente para recortarla.

``fpdf`` se importa dentro de la función: arrastra Pillow y no debe tocar el arranque de
la API ni del worker.
"""

import io
from dataclasses import dataclass

from app.core.html import safe_file_name
from app.services.fonts import SANS, font_path, printable
from app.services.postcard import PostcardContent, render_postcard
from app.services.qr_card import metrics
from app.services.themes import hex_to_rgb, mix, palette_for

# Hoja A5 vertical, en milímetros.
PAGE_WIDTH, PAGE_HEIGHT = 148.0, 210.0
# Ancho de la postal sobre el papel. El alto sale de su proporción.
CARD_WIDTH = 100.0


@dataclass(frozen=True, slots=True)
class CardContent:
    title: str
    recipient_name: str
    sender_name: str | None
    note: str
    public_url: str
    theme: str
    letter_id: str
    version: int


def card_name(letter) -> str:
    """Nombre del adjunto. El título lo escribe el comprador: pasa por la lista blanca."""
    return f"tarjeta-qr-{safe_file_name(letter.title, fallback='carta')}.pdf"


def render_card_pdf(content: CardContent) -> bytes:
    """Devuelve el PDF de la tarjeta. Síncrono y puro: no toca red ni disco."""
    from fpdf import FPDF  # noqa: PLC0415 - arrastra Pillow; no debe cargarse al arrancar

    palette = palette_for(content.theme)
    design = metrics()
    postcard = render_postcard(
        PostcardContent(
            recipient_name=content.recipient_name,
            sender_name=content.sender_name,
            note=content.note,
            public_url=content.public_url,
            theme=content.theme,
        )
    )

    pdf = FPDF(orientation="P", unit="mm", format=(PAGE_WIDTH, PAGE_HEIGHT))
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)
    pdf.add_font(SANS, "", font_path(SANS))
    title = printable(content.title) or "Una carta especial"
    pdf.set_title(f"Tarjeta QR · {title}")
    pdf.set_lang("es")
    pdf.set_subject(f"Carta {content.letter_id} · versión {content.version}")
    pdf.set_author("Zyvencore")
    pdf.set_creator("zy-valentine-backend")

    pdf.add_page()
    pdf.set_fill_color(*hex_to_rgb(palette.bg))
    pdf.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, style="F")

    height = CARD_WIDTH * design.height / design.width
    pdf.image(
        io.BytesIO(postcard),
        x=(PAGE_WIDTH - CARD_WIDTH) / 2,
        y=(PAGE_HEIGHT - height) / 2,
        w=CARD_WIDTH,
    )

    pdf.set_font(SANS, "", 5.5)
    pdf.set_text_color(*hex_to_rgb(mix(palette.text, palette.bg, 0.5)))
    stamp = printable(f"Carta {content.letter_id} · versión {content.version}", limit=80)
    pdf.set_xy(0, PAGE_HEIGHT - 12)
    pdf.cell(PAGE_WIDTH, 6, stamp, align="C")
    return bytes(pdf.output())
