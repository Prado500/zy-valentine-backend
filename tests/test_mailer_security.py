"""Seguridad y contrato del correo al comprador.

El comprador escribe el título de la carta y ese texto acaba dentro de un HTML que
abrirá su cliente de correo. Aquí se comprueba que ese HTML no puede ejecutar nada,
sea cual sea el texto que se le meta, y que el correo dice lo que debe: gracias por la
compra, enlace y QR, deseo, consejos y la nota de la tarjeta adjunta. Ni la
dedicatoria ni la canción ni descargas sueltas.

Las comprobaciones son **estructurales**, no textuales: se interpreta el documento
con un analizador de HTML y se exige que no haya etiquetas ejecutables ni atributos
de evento. Buscar la cadena "<script>" a mano daría falsos positivos (el texto
escapado la contiene, y es inofensivo) y falsos negativos (`<ScRiPt>`, `<svg onload>`).
"""

from html.parser import HTMLParser

import pytest

from app.core.html import escape_attr, escape_text, safe_file_name, sanitize
from app.services.gift_tips import HOW_TO_DELIVER
from app.services.mailer import Attachment, build_mime, render_letter_email
from app.services.themes import THEMES

# Cargas típicas de XSS: cierre de etiqueta, atributo, protocolo y contexto de comentario.
PAYLOADS = [
    "<script>alert(1)</script>",
    "<ScRiPt>alert(1)</ScRiPt>",
    '"><img src=x onerror=alert(1)>',
    "'><svg/onload=alert(1)>",
    "</title><script>alert(1)</script>",
    "</p></figcaption><iframe src=javascript:alert(1)>",
    "<!--<script>alert(1)//--></script>",
    "javascript:alert(document.cookie)",
    "<img src=1 href=1 onerror=javascript:alert(1)>",
    " <script>alert(1)</script> ",
]

EXECUTABLE = {"script", "iframe", "object", "embed", "svg", "math", "base", "form", "link"}
PUBLIC_URL = "https://frontend.example.com/carta/abc"
CID = "<20260909.qr@zy-valentine.invalid>"


class Audit(HTMLParser):
    """Recorre el HTML y anota lo que podría ejecutar código."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.handlers: list[tuple[str, str]] = []
        self.urls: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for name, value in attrs:
            if name.startswith("on"):
                self.handlers.append((tag, name))
            if name in ("src", "href", "action", "formaction") and value:
                self.urls.append(value.strip().lower())

    handle_startendtag = handle_starttag


def audit(html: str) -> Audit:
    parser = Audit()
    parser.feed(html)
    parser.close()
    return parser


def email(**changes):
    defaults = {
        "to": "ana@example.com",
        "title": "Para ti",
        "public_url": PUBLIC_URL,
        "qr_source": f"cid:{CID[1:-1]}",
        "letter_id": "8c1f",
        "version": 1,
    }
    return render_letter_email(**{**defaults, **changes})


def card(name="tarjeta-qr-carta.pdf") -> Attachment:
    return Attachment(filename=name, content=b"%PDF-1.7", maintype="application", subtype="pdf")


def qr_inline() -> Attachment:
    return Attachment(
        filename="qr.png", content=b"\x89PNG", maintype="image", subtype="png", cid=CID
    )


# --- Seguridad ----------------------------------------------------------------------------------


@pytest.mark.parametrize("payload", PAYLOADS)
def test_sad_path_no_payload_survives_as_executable_markup(payload):
    """El mismo texto malicioso en el título, el tema y el nombre del adjunto."""
    mail = email(title=payload, theme=payload, attachments=(card(payload),))

    report = audit(mail.html)
    assert not EXECUTABLE & set(report.tags), f"etiqueta ejecutable con: {payload!r}"
    assert not report.handlers, f"atributo de evento con: {payload!r}"
    assert not [url for url in report.urls if url.startswith("javascript:")]


def test_the_subject_cannot_inject_email_headers():
    """Un salto de línea en el asunto partiría la cabecera y añadiría destinatarios."""
    mail = email(title="Hola\r\nBcc: intruso@example.com")

    assert "\n" not in mail.subject and "\r" not in mail.subject
    assert "Bcc:" in mail.subject  # se conserva como texto, ya no como cabecera
    assert mail.subject.startswith("Gracias por tu compra")


def test_the_plain_text_part_carries_no_control_characters():
    mail = email(title="Título\x1b[31m\x00")

    assert "\x00" not in mail.text and "\x1b" not in mail.text


# --- MIME: cómo viaja el QR ---------------------------------------------------------------------


def test_the_qr_travels_as_a_related_inline_part_with_a_filename():
    """Regresión: con ``data:`` el destinatario solo veía el texto alternativo.

    Se audita el MIME que sale por el cable, no la plantilla: la imagen debe existir como
    parte ``image/png`` con disposición ``inline`` **y nombre de archivo** (Outlook y el
    correo de iOS no la pintan sin él), su ``Content-ID`` debe coincidir con el ``src`` del
    HTML y el PDF debe seguir siendo un adjunto normal.
    """
    mail = email(attachments=(card(),), inline=(qr_inline(),))

    mime = build_mime("no-reply@example.com", mail)
    types = [part.get_content_type() for part in mime.walk()]
    assert "multipart/related" in types  # el HTML y su imagen viajan juntos
    assert "text/plain" in types  # la alternativa en texto sigue ahí

    image = next(part for part in mime.walk() if part.get_content_type() == "image/png")
    assert image["Content-ID"] == CID
    assert image.get_content_disposition() == "inline"
    assert image.get_filename() == "qr.png"

    body = next(
        part
        for part in mime.walk()
        if part.get_content_type() == "text/html" and part.get_content_disposition() != "attachment"
    )
    html = body.get_content()
    assert f'src="cid:{CID[1:-1]}"' in html
    assert "data:image/png;base64," not in html

    pdf = next(part for part in mime.walk() if part.get_content_type() == "application/pdf")
    assert pdf.get_content_disposition() == "attachment"
    assert pdf.get_filename() == "tarjeta-qr-carta.pdf"
    assert pdf.get_payload(decode=True) == b"%PDF-1.7"
    assert types.count("image/png") == 1  # ningún QR suelto como adjunto


def test_the_qr_can_be_a_remote_image_with_no_inline_part():
    """Con API_PUBLIC_URL el QR es una URL https: la vía que todos los clientes muestran."""
    source = "https://api.example.com/api/v1/public/letters/abc/qr.png"
    mail = email(qr_source=source, attachments=(card(),), inline=())

    mime = build_mime("no-reply@example.com", mail)
    types = [part.get_content_type() for part in mime.walk()]
    assert "image/png" not in types and "multipart/related" not in types
    assert f'src="{source}"' in mail.html
    assert source in audit(mail.html).urls


# --- Contenido: lo que el comprador recibe ------------------------------------------------------


def test_the_email_thanks_the_buyer_in_the_theme_colors():
    mail = email(theme="midnight")

    assert THEMES["midnight"].bg in mail.html and THEMES["midnight"].accent in mail.html
    assert "Medianoche Azul" in mail.html
    assert "¡Gracias por tu compra!" in mail.html and "feliz día en pareja" in mail.html
    assert "«Para ti»" in mail.html and PUBLIC_URL in mail.html
    assert mail.text.startswith("¡Gracias por tu compra!")
    assert "feliz día en pareja" in mail.text and PUBLIC_URL in mail.text


def test_an_unknown_theme_paints_the_classic_palette():
    assert THEMES["classic"].bg in email(theme="no-existe").html
    assert THEMES["pastel-pink"].bg in email(theme="pastelPink").html


def test_the_dedication_and_the_song_never_reach_the_email():
    """El correo llega al comprador: la dedicatoria «De X con cariño para Y» vive en el
    PDF, y la canción solo en el visor."""
    mail = email(title="Para ti")

    for absent in ("con cariño para", "De parte de", "youtu", "Escúchala", "qr.png", "Descargar"):
        assert absent not in mail.html and absent not in mail.text
    hrefs = {url for url in audit(mail.html).urls if url.startswith("http")}
    assert hrefs == {PUBLIC_URL.lower()}  # el único enlace es la carta


def test_the_tips_are_embedded_and_the_card_note_depends_on_the_pdf():
    with_card = email(attachments=(card("tarjeta-qr-Para ti.pdf"),))
    assert "Tu tarjeta para imprimir" in with_card.html
    assert "tarjeta-qr-Para ti.pdf" in with_card.html and "tarjeta-qr-Para ti.pdf" in with_card.text

    without = email()
    assert "Tu tarjeta para imprimir" not in without.html and "adjunta" not in without.text
    # Los consejos van siempre, incrustados: no dependen de que el PDF haya salido bien.
    for mail in (with_card, without):
        assert "Para acompañar tu carta" in mail.html
        assert "Flores" in mail.html and "Algo dulce" in mail.html and "Un detalle" in mail.html
        assert HOW_TO_DELIVER[0] in mail.html and "Flores:" in mail.text


# --- Utilidades de escape ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<b>", "&lt;b&gt;"),
        ('"', "&quot;"),
        ("'", "&#x27;"),
        ("&", "&amp;"),
    ],
)
def test_quotes_are_escaped_in_both_contexts(raw, expected):
    """`html.escape` deja fuera las comillas por defecto; aquí nunca."""
    assert escape_text(raw) == expected
    assert escape_attr(raw) == expected


def test_control_characters_and_line_separators_are_removed():
    assert sanitize("a\x00b\x1fc") == "abc"
    assert sanitize("a b c") == "a\nb\nc"
    assert sanitize("conserva\nsaltos\ty tabuladores") == "conserva\nsaltos\ty tabuladores"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "etcpasswd"),
        ('carta"; rm -rf /', "carta rm -rf"),
        ("carta\r\nContent-Type: text/x", "cartaContent-Type textx"),
        ("   ", "carta"),
        ("", "carta"),
    ],
)
def test_attachment_names_are_built_from_an_allow_list(raw, expected):
    assert safe_file_name(raw, fallback="carta") == expected
