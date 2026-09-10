"""Puerto de correo, plantilla HTML de entrega y documento autónomo adjunto.

El correo lleva botón al visor público, código QR al mismo visor e identificador
de carta y versión publicada. El estado del envío se persiste en
``letter_deliveries``; un reintento crea otro intento de envío, nunca otra carta.

El QR viaja **incrustado por Content-ID** (``cid:``), no como ``data:`` URI. Gmail,
Outlook y Yahoo descartan los ``data:`` dentro de ``<img src>``, así que el destinatario
solo veía el texto alternativo. Como parte ``multipart/related`` la imagen se muestra sin
pedir "mostrar imágenes" y sin depender de que la API sea alcanzable desde el cliente de
correo. El documento adjunto sí conserva el ``data:``: se abre en un navegador y debe
seguir funcionando sin red.

Además del cuerpo, se adjunta un **documento HTML autónomo** con las fotos incrustadas
en Base64 (``render_letter_document``). Es la copia que el destinatario conserva aunque
el visor deje de estar disponible: no pide nada a la red al abrirse. Construirlo es
trabajo de CPU sobre bytes, así que el worker lo delega a ``anyio.to_thread.run_sync``
en vez de hacerlo en el bucle de eventos.

El correo y el documento se pintan con la **paleta del tema** de la carta (la misma que
el comprador vio en la previsualización) y llevan el titular «De X con cariño para Y»,
los consejos para acompañarla y la explicación de los adjuntos: la tarjeta QR en PDF y
el código suelto como ``qr.png``. El correo suele llegar al comprador —el formulario
pide "tu correo, o el correo donde quieres recibir el regalo"—, así que el texto sirve
tanto para quien la escribió como para quien la recibe.
"""

import base64
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage

from anyio import to_thread

from app.core.config import Settings
from app.core.html import escape_attr, escape_text, safe_header, sanitize
from app.services.gift_tips import GENERAL_MESSAGE, HOW_TO_DELIVER, GiftTips, tips_for
from app.services.letter_body import DEFAULT_SENDER, song_url_or_none
from app.services.themes import Palette, mix, palette_for, text_on

# Tipos que el visor y el correo saben mostrar. Coincide con
# `app.services.storage.ALLOWED_CONTENT_TYPES`, declarado aquí para que el módulo de
# correo no dependa del de almacenamiento.
ALLOWED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


@dataclass(frozen=True)
class Attachment:
    """Adjunto de correo. ``filename`` viaja en una cabecera MIME, así que quien lo
    construya debe pasarlo ya saneado con :func:`app.core.html.safe_file_name`.

    Con ``cid`` la parte deja de ser un adjunto y pasa a ser un recurso *relacionado*
    (RFC 2392): viaja dentro del cuerpo y el HTML la referencia con ``src="cid:..."``.
    El identificador se guarda con los ``<>`` que exige la cabecera ``Content-ID``; el
    ``src`` del HTML los lleva pelados.
    """

    filename: str
    content: bytes
    maintype: str = "text"
    subtype: str = "html"
    cid: str | None = None


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    html: str
    text: str
    attachments: tuple[Attachment, ...] = field(default=())
    # Partes incrustadas en el HTML (hoy, el QR). Separadas de ``attachments`` a
    # propósito: no deben aparecer como archivos en la bandeja de entrada.
    inline: tuple[Attachment, ...] = field(default=())


class Mailer:
    async def send(self, message: Mail) -> str:  # pragma: no cover - puerto
        raise NotImplementedError


class ConsoleMailer(Mailer):
    """Backend local: registra el destinatario y el asunto, nunca el cuerpo ni claves."""

    def __init__(self):
        self.sent: list[Mail] = []

    async def send(self, message: Mail) -> str:
        self.sent.append(message)
        return f"console-{len(self.sent)}"


def build_mime(sender: str, message: Mail) -> EmailMessage:
    """Arma el MIME final: texto, HTML, partes incrustadas y adjuntos.

    Vive fuera de :class:`SmtpMailer` para poder auditar la estructura del mensaje sin
    abrir un socket SMTP: que el QR llegue como parte ``image/png`` con su ``Content-ID``
    es justo lo que se rompió en producción.
    """
    email = EmailMessage()
    email["From"] = sender
    email["To"] = message.to
    email["Subject"] = message.subject
    email.set_content(message.text)
    email.add_alternative(message.html, subtype="html")
    # La parte HTML es la última del multipart/alternative. Se toma AQUÍ, antes de añadir
    # ningún adjunto: `add_attachment` envuelve el mensaje en un multipart/mixed y el
    # índice dejaría de apuntar al HTML.
    html_part = email.get_payload()[-1]
    for item in message.inline:
        # Convierte esa parte en multipart/related y marca la imagen como `inline`;
        # el `src="cid:..."` del cuerpo la encuentra ahí mismo.
        html_part.add_related(
            item.content, maintype=item.maintype, subtype=item.subtype, cid=item.cid
        )
    for item in message.attachments:
        email.add_attachment(
            item.content,
            maintype=item.maintype,
            subtype=item.subtype,
            filename=item.filename,
        )
    return email


class SmtpMailer(Mailer):
    def __init__(self, settings: Settings):
        self.settings = settings

    def _send(self, message: Mail) -> str:
        settings = self.settings
        email = build_mime(settings.sender_address, message)
        context = ssl.create_default_context()
        with smtplib.SMTP(
            settings.mail_host, settings.mail_port, timeout=settings.mail_timeout
        ) as smtp:
            smtp.starttls(context=context)
            smtp.login(settings.mail_username, settings.mail_password.get_secret_value())
            smtp.send_message(email)
        return email.get("Message-ID") or "smtp-sent"

    async def send(self, message: Mail) -> str:
        return await to_thread.run_sync(self._send, message)


def build_mailer(settings: Settings) -> Mailer:
    return SmtpMailer(settings) if settings.mail_backend == "smtp" else ConsoleMailer()


# Pilas de fuentes del correo. Ningún cliente carga Google Fonts, así que lo que se ve
# es el fallback: serif para títulos, serif cursiva para los nombres (la `cursive`
# genérica de Windows es Comic Sans) y la sans del sistema para el resto.
SERIF = "'Playfair Display',Georgia,'Times New Roman',serif"
SCRIPT = "'Great Vibes',Georgia,'Times New Roman',serif"
SANS = "'Be Vietnam Pro','Segoe UI',Helvetica,Arial,sans-serif"


@dataclass(frozen=True)
class Colors:
    """Colores del correo derivados de la paleta. Sin alfa: los clientes lo pierden."""

    bg: str
    card: str
    text: str
    accent: str
    border: str
    muted: str
    head: str
    button_text: str

    @classmethod
    def of(cls, palette: Palette) -> "Colors":
        return cls(
            bg=palette.bg,
            card=palette.card_bg,
            text=palette.text,
            accent=palette.accent,
            border=palette.border,
            muted=mix(palette.text, palette.card_bg, 0.45),
            head=mix(palette.accent, palette.card_bg, 0.9),
            button_text=text_on(palette.accent),
        )


def _lockup_html(colors: Colors, sender: str | None, recipient: str) -> str:
    """«De X con cariño para Y» o «Con cariño para Y» si nadie firmó."""
    label = (
        f'<p style="margin:0;font-family:{SERIF};font-size:13px;letter-spacing:.12em;'
        f'text-transform:uppercase;color:{colors.muted}">'
    )
    name = (
        f'<p style="margin:2px 0 6px;font-family:{SCRIPT};font-style:italic;'
        f'font-weight:normal;line-height:1.1;color:{colors.accent};'
    )
    parts = []
    if sender:
        parts.append(f"{label}De</p>")
        parts.append(f'{name}font-size:30px">{escape_text(sender)}</p>')
        parts.append(f"{label}con cariño para</p>")
    else:
        parts.append(f"{label}Con cariño para</p>")
    parts.append(f'{name}font-size:34px">{escape_text(recipient)}</p>')
    return "".join(parts)


def _box_html(colors: Colors, heading: str, inner: str) -> str:
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{colors.bg}" style="background-color:{colors.bg};border:1px solid '
        f'{colors.border};border-radius:12px"><tr><td style="padding:14px 16px">'
        f'<p style="margin:0 0 6px;font-family:{SERIF};font-size:15px;color:{colors.accent}">'
        f"{heading}</p>{inner}</td></tr></table>"
    )


def _attachments_copy(attachments: tuple[Attachment, ...]) -> tuple[str, str, str]:
    """(nombre del PDF, nombre del PNG, nombre del HTML) presentes, o cadenas vacías."""
    card = next((a.filename for a in attachments if a.subtype == "pdf"), "")
    png = next((a.filename for a in attachments if a.maintype == "image" and a.cid is None), "")
    document = next((a.filename for a in attachments if a.subtype == "html"), "")
    return card, png, document


def render_letter_email(
    *,
    to: str,
    recipient_name: str,
    title: str,
    public_url: str,
    qr_source: str,
    letter_id: str,
    version: int,
    attachments: tuple[Attachment, ...] = (),
    inline: tuple[Attachment, ...] = (),
    sender_name: str | None = None,
    theme: str | None = None,
    song_url: str | None = None,
    tips: GiftTips | None = None,
) -> Mail:
    palette = palette_for(theme)
    colors = Colors.of(palette)
    tips = tips or tips_for(theme)
    safe_title = escape_text(title)
    safe_url = escape_attr(public_url)
    plain_name = sanitize(recipient_name)
    plain_title = sanitize(title)
    plain_sender = sanitize(sender_name).strip() if sender_name else ""
    song = song_url_or_none(song_url) if song_url else None
    card_name, png_name, document_name = _attachments_copy(attachments)

    paragraph = f'margin:0 0 10px;font-family:{SANS};font-size:13px;line-height:1.55;color:{colors.text}'
    small = f'margin:0;font-family:{SANS};font-size:12px;line-height:1.5;color:{colors.muted}'

    song_html = ""
    if song:
        song_html = (
            f'<tr><td align="center" style="padding:14px 28px 0"><p style="{paragraph}">'
            f'Escúchala con esta canción: <a href="{escape_attr(song)}" '
            f'style="color:{colors.accent}">{escape_text(song)}</a></p></td></tr>'
        )

    deliver_lines = []
    if card_name:
        deliver_lines.append(
            f"Adjuntamos la tarjeta con el código en PDF (<b>{escape_text(card_name)}</b>): "
            "imprímela, recórtala y ponla con el regalo."
        )
    if png_name:
        deliver_lines.append(
            f"El código también va suelto como <b>{escape_text(png_name)}</b>, por si "
            "prefieres imprimirlo o compartirlo tal cual."
        )
    deliver_html = ""
    if deliver_lines:
        inner = "".join(f'<p style="{paragraph}">{line}</p>' for line in deliver_lines)
        deliver_html = (
            f'<tr><td style="padding:20px 28px 0">'
            f'{_box_html(colors, "Para imprimir y entregar", inner)}</td></tr>'
        )

    tip_rows = "".join(
        f'<tr><td style="padding:4px 0;font-family:{SANS};font-size:13px;line-height:1.5;'
        f'color:{colors.text}"><b style="color:{colors.accent}">{label}</b> · '
        f"{escape_text(text)}</td></tr>"
        for label, text in (
            ("Flores", tips.flowers),
            ("Algo dulce", tips.sweets),
            ("Un detalle", tips.touch),
        )
    )
    tips_html = _box_html(
        colors,
        "Para acompañar tu carta",
        f'<p style="{paragraph}">{escape_text(GENERAL_MESSAGE)}</p>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f"{tip_rows}</table>"
        + "".join(
            f'<p style="{small};margin-top:8px">{escape_text(line)}</p>' for line in HOW_TO_DELIVER
        ),
    )

    document_html = ""
    if document_name:
        document_html = (
            f'<tr><td style="padding:16px 28px 0"><p style="{small}">También va la carta '
            f"completa como archivo HTML (<b>{escape_text(document_name)}</b>): ábrelo "
            "cuando quieras, incluso sin conexión.</p></td></tr>"
        )

    html = f"""<!doctype html>
<html lang="es"><body style="margin:0;padding:0;background-color:{colors.bg}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
 bgcolor="{colors.bg}" style="background-color:{colors.bg}">
<tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"
 bgcolor="{colors.card}" style="max-width:560px;width:100%;background-color:{colors.card};
 border:1px solid {colors.border};border-radius:16px">
  <tr><td align="center" style="padding:28px 28px 10px;text-align:center;
   background-color:{colors.head};border-radius:16px 16px 0 0">
    <p style="margin:0 0 14px;font-family:{SANS};font-size:11px;letter-spacing:.18em;
     text-transform:uppercase;color:{colors.muted}">{escape_text(palette.name)} · Tu carta está lista</p>
    {_lockup_html(colors, plain_sender or None, plain_name)}
  </td></tr>
  <tr><td align="center" style="padding:8px 28px 0;text-align:center">
    <p style="margin:0;font-family:{SERIF};font-size:19px;line-height:1.35;color:{colors.text}">{safe_title}</p>
  </td></tr>
  <tr><td align="center" style="padding:20px 28px 0">
    <a href="{safe_url}" style="display:inline-block;background-color:{colors.accent};
     color:{colors.button_text};font-family:{SANS};font-size:15px;font-weight:600;
     text-decoration:none;padding:12px 26px;border-radius:999px">Ver la carta</a>
  </td></tr>
  <tr><td align="center" style="padding:22px 28px 0;text-align:center">
    <p style="{small};margin-bottom:8px">Escanea el código con la cámara o abre el enlace</p>
    <img src="{escape_attr(qr_source)}" width="150" height="150" alt="Código QR hacia la carta"
     style="display:block;margin:0 auto;border:1px solid {colors.border};border-radius:12px;
     padding:6px;background-color:#ffffff" />
    <p style="margin:10px 0 0;font-family:{SANS};font-size:12px;word-break:break-all">
      <a href="{safe_url}" style="color:{colors.accent}">{safe_url}</a>
    </p>
  </td></tr>
  {song_html}
  {deliver_html}
  <tr><td style="padding:16px 28px 0">{tips_html}</td></tr>
  {document_html}
  <tr><td align="center" style="padding:22px 28px 28px;text-align:center">
    <p style="{small}">Carta {escape_text(letter_id)} · versión publicada {version}</p>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""

    headline = (
        f"De {plain_sender} con cariño para {plain_name}."
        if plain_sender
        else f"Con cariño para {plain_name}."
    )
    text_lines = [headline, plain_title, "", f"Ábrela aquí: {sanitize(public_url)}"]
    if song:
        text_lines.append(f"Escúchala con esta canción: {song}")
    if deliver_lines:
        text_lines += ["", "Para imprimir y entregar:"]
        if card_name:
            text_lines.append(f"- La tarjeta con el código en PDF: {sanitize(card_name)}")
        if png_name:
            text_lines.append(f"- El código suelto: {sanitize(png_name)}")
    text_lines += [
        "",
        "Para acompañar tu carta:",
        GENERAL_MESSAGE,
        f"- Flores: {tips.flowers}",
        f"- Algo dulce: {tips.sweets}",
        f"- Un detalle: {tips.touch}",
        *HOW_TO_DELIVER,
        "",
        f"Carta {sanitize(letter_id)} · versión publicada {version}",
    ]
    return Mail(
        to=to,
        # El asunto es una cabecera: un salto de línea aquí sería inyección de
        # cabeceras SMTP, así que se limpia antes de recortarlo.
        subject=safe_header(f"Tu carta para {plain_name}: {plain_title}")[:120],
        html=html,
        text="\n".join(text_lines) + "\n",
        attachments=attachments,
        inline=inline,
    )


def media_type(declared: str) -> str:
    """Tipo MIME permitido para un ``data:`` URI, elegido por lista blanca.

    El valor llega de ``letter_photos.content_type``. Aunque hoy solo lo escribe el
    servidor, escaparlo no bastaría: dentro de un ``data:`` URI un tipo inventado
    cambiaría cómo interpreta el navegador los bytes que siguen. Lo que no está en
    la lista se sirve como imagen genérica.
    """
    return declared if declared in ALLOWED_IMAGE_TYPES else "application/octet-stream"


def render_letter_document(
    *,
    title: str,
    recipient_name: str,
    body: str,
    public_url: str,
    qr_source: str,
    photos: list[tuple[str, str, bytes]],
    letter_id: str,
    version: int,
    max_bytes: int,
    sender_name: str | None = None,
    song_url: str | None = None,
    theme: str | None = None,
) -> str:
    """Documento HTML autónomo: fotos en Base64, sin una sola petición a la red.

    ``photos`` son tuplas ``(pie, tipo, bytes)`` en orden de posición. Base64 infla los
    bytes un 33 %, así que se incrustan mientras quepan en ``max_bytes`` y las que no
    caben se omiten con un aviso visible: es preferible una carta sin la última foto a
    un correo que el servidor rechace por tamaño.

    ``body`` es el mensaje ya separado de la firma y la canción
    (:func:`app.services.letter_body.parse_body`); aquí no se vuelve a interpretar.
    La firma lleva ``sender_name`` o, si nadie firmó, la misma frase del visor.

    Función síncrona a propósito: quien la llama la ejecuta en un hilo aparte
    (``anyio.to_thread.run_sync``) para no bloquear el bucle de eventos del worker.
    """
    palette = palette_for(theme)
    colors = Colors.of(palette)
    safe_title = escape_text(title)
    safe_name = escape_text(recipient_name)
    safe_url = escape_attr(public_url)
    paragraphs = "".join(
        f'<p style="margin:0 0 14px;font-family:{SERIF};font-size:16px;line-height:1.7;'
        f'white-space:pre-wrap;color:{colors.text}">{escape_text(block)}</p>'
        for block in body.split("\n\n")
        if block.strip()
    )

    figures: list[str] = []
    omitted = 0
    # Presupuesto aproximado: cabecera, cuerpo y QR ya escritos, más lo que ocupe cada
    # foto en Base64. Se mide antes de añadir para no construir un documento inválido.
    used = len(paragraphs.encode()) + len(qr_source) + 4096
    for caption, content_type, data in photos:
        encoded_size = (len(data) + 2) // 3 * 4
        if used + encoded_size > max_bytes:
            omitted += 1
            continue
        used += encoded_size
        encoded = base64.b64encode(data).decode()
        figures.append(
            '<figure style="margin:0 0 20px">'
            f'<img src="data:{escape_attr(media_type(content_type))};base64,{encoded}" '
            'alt="" style="display:block;width:100%;border-radius:12px" />'
            f'<figcaption style="margin-top:6px;font-family:{SANS};font-size:13px;'
            f'color:{colors.muted}">{escape_text(caption)}</figcaption></figure>'
        )
    if omitted:
        figures.append(
            f'<p style="font-family:{SANS};font-size:13px;color:{colors.muted}">'
            f"{omitted} foto(s) no caben en este archivo; ábrelas en el enlace de la carta.</p>"
        )

    sender = sanitize(sender_name).strip() if sender_name else ""
    signature = (
        f'<p style="margin:26px 0 0;font-family:{SERIF};font-size:12px;letter-spacing:.16em;'
        f'text-transform:uppercase;color:{colors.muted}">De parte de</p>'
        f'<p style="margin:2px 0 0;font-family:{SCRIPT};font-style:italic;font-size:28px;'
        f'line-height:1.1;color:{colors.accent}">{escape_text(sender or DEFAULT_SENDER)}</p>'
    )
    song = song_url_or_none(song_url) if song_url else None
    song_html = (
        f'<p style="margin:18px 0 0;font-family:{SANS};font-size:14px">'
        f'<a href="{escape_attr(song)}" style="color:{colors.accent}">'
        "Escuchar la canción que acompaña esta carta</a></p>"
        if song
        else ""
    )

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>{safe_title}</title></head>
<body style="margin:0;padding:24px;background-color:{colors.bg};
 font-family:{SANS};color:{colors.text}">
  <main style="max-width:640px;margin:0 auto;background-color:{colors.card};
   border:1px solid {colors.border};border-radius:16px;padding:32px">
    <p style="margin:0 0 4px;font-family:{SERIF};font-size:12px;letter-spacing:.16em;
     text-transform:uppercase;color:{colors.muted}">Para</p>
    <p style="margin:0 0 18px;font-family:{SCRIPT};font-style:italic;font-size:34px;
     line-height:1.1;color:{colors.accent}">{safe_name}</p>
    <h1 style="font-family:{SERIF};font-size:24px;margin:0 0 24px;color:{colors.text}">{safe_title}</h1>
    {paragraphs}
    <div style="margin:28px 0 0">{"".join(figures)}</div>
    {signature}
    {song_html}
    <hr style="border:0;border-top:1px solid {colors.border};margin:28px 0" />
    <p style="margin:0 0 8px;font-size:13px;color:{colors.muted}">Esta carta también vive en línea:</p>
    <p style="margin:0 0 12px;font-size:13px;word-break:break-all">
      <a href="{safe_url}" style="color:{colors.accent}">{safe_url}</a>
    </p>
    <img src="{escape_attr(qr_source)}" width="130" height="130"
         alt="Código QR hacia la carta" style="display:block;border:0;background-color:#ffffff;
         border-radius:10px;padding:5px;border:1px solid {colors.border}" />
    <p style="margin:16px 0 0;font-size:12px;color:{colors.muted}">
      Carta {escape_text(letter_id)} · versión publicada {version}
    </p>
  </main>
</body></html>"""
