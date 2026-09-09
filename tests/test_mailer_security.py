"""Seguridad del correo y del documento adjunto.

El comprador escribe el título, el cuerpo, su firma y el nombre de cada foto, y todo
eso acaba dentro de un HTML que abrirá otra persona. Aquí se comprueba que ese HTML
no puede ejecutar nada, sea cual sea el texto que se le meta.

Las comprobaciones son **estructurales**, no textuales: se interpreta el documento
con un analizador de HTML y se exige que no haya etiquetas ejecutables ni atributos
de evento. Buscar la cadena "<script>" a mano daría falsos positivos (el texto
escapado la contiene, y es inofensivo) y falsos negativos (`<ScRiPt>`, `<svg onload>`).
"""

from html.parser import HTMLParser

import pytest

from app.core.html import escape_attr, escape_text, safe_file_name, sanitize
from app.services.mailer import (
    Attachment,
    build_mime,
    media_type,
    render_letter_document,
    render_letter_email,
)

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
    " <script>alert(1)</script> ",
]

EXECUTABLE = {"script", "iframe", "object", "embed", "svg", "math", "base", "form", "link"}


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


def document(**changes) -> str:
    defaults = {
        "title": "Para ti",
        "recipient_name": "Ana",
        "body": "Primera línea\n\nSegunda línea",
        "public_url": "https://frontend.example.com/carta/abc",
        "qr_source": "data:image/png;base64,AAAA",
        "photos": [("pie", "image/png", b"\x89PNG")],
        "letter_id": "8c1f",
        "version": 1,
        "max_bytes": 24_000_000,
    }
    return render_letter_document(**{**defaults, **changes})


# --- render_letter_document: los cinco casos ------------------------------------------------


def test_happy_path_keeps_the_content_readable():
    html = document(title="Feliz aniversario", body="Te quiero\n\nMucho")

    assert "Feliz aniversario" in html
    assert "Te quiero" in html and "Mucho" in html
    assert "data:image/png;base64," in html
    report = audit(html)
    assert report.tags.count("img") == 2  # la foto y el QR
    assert not report.handlers


@pytest.mark.parametrize("payload", PAYLOADS)
def test_sad_path_no_payload_survives_as_executable_markup(payload):
    """El mismo texto malicioso en los cuatro campos que escribe una persona."""
    report = audit(
        document(
            title=payload,
            recipient_name=payload,
            body=payload,
            photos=[(payload, "image/png", b"x")],
        )
    )

    assert not EXECUTABLE & set(report.tags), f"etiqueta ejecutable con: {payload!r}"
    assert not report.handlers, f"atributo de evento con: {payload!r}"
    assert not [url for url in report.urls if url.startswith("javascript:")]


def test_edge_the_size_limit_drops_photos_instead_of_producing_a_rejected_email():
    """Justo por encima del tope, la última foto se omite con aviso visible.

    Un correo de 30 MB lo rechaza el proveedor y el destinatario no recibe nada: es
    preferible una carta con una foto menos.
    """
    big = b"x" * 60_000  # ~80 KB una vez en Base64
    within = document(photos=[("una", "image/png", big)], max_bytes=200_000)
    beyond = document(
        photos=[("una", "image/png", big), ("dos", "image/png", big)], max_bytes=90_000
    )

    assert within.count("base64,") == 2  # la foto y el QR
    assert len(within.encode()) <= 200_000
    # La invariante que importa: el documento nunca supera el tope que se le dio, y
    # cuando algo se queda fuera, el destinatario se entera.
    assert beyond.count("base64,") == 2  # solo entró una de las dos
    assert len(beyond.encode()) <= 90_000
    assert "no caben en este archivo" in beyond


def test_edge_a_body_of_only_separators_produces_a_valid_document():
    """Frontera vacía: sin párrafos, sin fotos y con textos en blanco."""
    html = document(title="", recipient_name="", body="\n\n\n\n", photos=[])

    report = audit(html)
    assert report.tags.count("img") == 1  # solo el QR
    assert not report.handlers
    assert "<html" in html and "</html>" in html


def test_exception_an_unknown_media_type_is_not_trusted_inside_a_data_uri():
    """Un tipo inventado cambiaría cómo interpreta el navegador los bytes que siguen."""
    html = document(photos=[("pie", "text/html", b"<script>alert(1)</script>")])

    assert "data:text/html;base64" not in html
    assert "data:application/octet-stream;base64" in html
    assert not audit(html).handlers
    assert media_type("image/png") == "image/png"
    assert media_type("text/html; charset=utf-8") == "application/octet-stream"


# --- Cuerpo del correo ------------------------------------------------------------------------


@pytest.mark.parametrize("payload", PAYLOADS)
def test_the_email_body_is_as_strict_as_the_attachment(payload):
    mail = render_letter_email(
        to="ana@example.com",
        recipient_name=payload,
        title=payload,
        public_url="https://frontend.example.com/carta/abc",
        qr_source="data:image/png;base64,AAAA",
        letter_id="8c1f",
        version=1,
        attachments=(Attachment(filename="carta.html", content=b"<p>x</p>"),),
    )

    report = audit(mail.html)
    assert not EXECUTABLE & set(report.tags)
    assert not report.handlers


def test_the_subject_cannot_inject_email_headers():
    """Un salto de línea en el asunto partiría la cabecera y añadiría destinatarios."""
    mail = render_letter_email(
        to="ana@example.com",
        recipient_name="Ana",
        title="Hola\r\nBcc: intruso@example.com",
        public_url="https://frontend.example.com/carta/abc",
        qr_source="data:image/png;base64,AAAA",
        letter_id="8c1f",
        version=1,
    )

    assert "\n" not in mail.subject and "\r" not in mail.subject
    assert "Bcc:" in mail.subject  # se conserva como texto, ya no como cabecera


def test_the_plain_text_part_carries_no_control_characters():
    mail = render_letter_email(
        to="ana@example.com",
        recipient_name="Ana\x00\x07",
        title="Título\x1b[31m",
        public_url="https://frontend.example.com/carta/abc",
        qr_source="data:image/png;base64,AAAA",
        letter_id="8c1f",
        version=1,
    )

    assert "\x00" not in mail.text and "\x1b" not in mail.text


def test_the_qr_travels_as_a_related_part_and_not_as_a_data_uri():
    """Regresión: con ``data:`` el destinatario solo veía el texto alternativo.

    Se audita el MIME que sale por el cable, no la plantilla: la imagen debe existir como
    parte ``image/png`` con disposición ``inline``, su ``Content-ID`` debe coincidir con
    el ``src`` del HTML y el adjunto normal debe seguir siendo un adjunto.
    """
    cid = "<20260909.qr@zy-valentine.invalid>"
    mail = render_letter_email(
        to="ana@example.com",
        recipient_name="Ana",
        title="Para ti",
        public_url="https://frontend.example.com/carta/abc",
        qr_source=f"cid:{cid[1:-1]}",
        letter_id="8c1f",
        version=1,
        attachments=(Attachment(filename="carta.html", content=b"<p>x</p>"),),
        inline=(
            Attachment(
                filename="qr.png",
                content=b"\x89PNG",
                maintype="image",
                subtype="png",
                cid=cid,
            ),
        ),
    )

    mime = build_mime("no-reply@example.com", mail)
    types = [part.get_content_type() for part in mime.walk()]

    assert "multipart/related" in types  # el HTML y su imagen viajan juntos
    assert "text/plain" in types  # la alternativa en texto sigue ahí

    image = next(part for part in mime.walk() if part.get_content_type() == "image/png")
    assert image["Content-ID"] == cid
    assert image.get_content_disposition() == "inline"

    # El cuerpo es el text/html que NO es el documento adjunto (ambos son text/html).
    body = next(
        part
        for part in mime.walk()
        if part.get_content_type() == "text/html"
        and part.get_content_disposition() != "attachment"
    )
    html = body.get_content()
    assert f'src="cid:{cid[1:-1]}"' in html
    assert "data:image/png;base64," not in html

    document = next(part for part in mime.walk() if part.get_filename() == "carta.html")
    assert document.get_content_disposition() == "attachment"


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
