"""Puerto de correo y plantilla HTML del correo al comprador.

El correo llega a quien compró la carta —el formulario pide "tu correo, o el correo
donde quieres recibir el regalo"—, así que le agradece la compra, le da el enlace y el
QR para entregarla, le desea un feliz día en pareja y le sugiere con qué acompañarla.
La dedicatoria «De X con cariño para Y» y la canción **no** van en el correo: viven en
la tarjeta PDF adjunta y en el visor. El estado del envío se persiste en
``letter_deliveries``; un reintento crea otro intento de envío, nunca otra carta.

El QR del cuerpo llega de dos formas. Con ``API_PUBLIC_URL`` es una imagen remota
(``/api/v1/public/letters/{slug}/qr.png``), que todos los clientes muestran. Sin ella
viaja **incrustado por Content-ID** (``cid:``, parte ``multipart/related`` con
``Content-Disposition: inline`` y nombre de archivo, que Outlook exige). Nunca como
``data:`` URI: Gmail, Outlook y Yahoo lo descartan y el destinatario solo veía el texto
alternativo.

El correo se pinta con la **paleta del tema** de la carta, la misma que el comprador vio
en la previsualización.
"""

import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage

from anyio import to_thread

from app.core.config import Settings
from app.core.html import escape_attr, escape_text, safe_header, sanitize
from app.services.gift_tips import GENERAL_MESSAGE, HOW_TO_DELIVER, GiftTips, tips_for
from app.services.themes import Palette, mix, palette_for, text_on


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
    maintype: str = "application"
    subtype: str = "pdf"
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
        # el `src="cid:..."` del cuerpo la encuentra ahí mismo. La disposición se pasa
        # explícita: con `filename` y sin ella, `set_content` pondría `attachment`, y
        # sin `filename` Outlook y el correo de iOS no pintan la imagen.
        html_part.add_related(
            item.content,
            maintype=item.maintype,
            subtype=item.subtype,
            cid=item.cid,
            filename=item.filename,
            disposition="inline",
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
# es el fallback: serif para títulos, serif cursiva para el deseo (la `cursive`
# genérica de Windows es Comic Sans) y la sans del sistema para el resto.
SERIF = "'Playfair Display',Georgia,'Times New Roman',serif"
SCRIPT = "'Great Vibes',Georgia,'Times New Roman',serif"
SANS = "'Be Vietnam Pro','Segoe UI',Helvetica,Arial,sans-serif"

THANKS = "¡Gracias por tu compra!"
WISH = "Les deseamos un feliz día en pareja."


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


def _box_html(colors: Colors, heading: str, inner: str) -> str:
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{colors.bg}" style="background-color:{colors.bg};border:1px solid '
        f'{colors.border};border-radius:12px"><tr><td style="padding:14px 16px">'
        f'<p style="margin:0 0 6px;font-family:{SERIF};font-size:15px;color:{colors.accent}">'
        f"{heading}</p>{inner}</td></tr></table>"
    )


def render_letter_email(
    *,
    to: str,
    title: str,
    public_url: str,
    qr_source: str,
    letter_id: str,
    version: int,
    attachments: tuple[Attachment, ...] = (),
    inline: tuple[Attachment, ...] = (),
    theme: str | None = None,
    tips: GiftTips | None = None,
) -> Mail:
    """Correo al comprador: gracias, enlace y QR, deseo, consejos y la tarjeta adjunta.

    ``qr_source`` es lo que va en el ``src`` del ``<img>``: una URL ``https`` o un
    ``cid:`` cuya parte viaja en ``inline``. La nota sobre la tarjeta solo aparece si el
    PDF llegó en ``attachments``: si el render falló, no se promete nada.
    """
    palette = palette_for(theme)
    colors = Colors.of(palette)
    tips = tips or tips_for(theme)
    safe_title = escape_text(title)
    safe_url = escape_attr(public_url)
    plain_title = sanitize(title)
    card_name = next((item.filename for item in attachments if item.subtype == "pdf"), "")

    paragraph = (
        f"margin:0 0 10px;font-family:{SANS};font-size:13px;line-height:1.55;color:{colors.text}"
    )
    small = f"margin:0;font-family:{SANS};font-size:12px;line-height:1.5;color:{colors.muted}"

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

    card_html = ""
    if card_name:
        card_html = (
            '<tr><td style="padding:16px 28px 0">'
            + _box_html(
                colors,
                "Tu tarjeta para imprimir",
                f'<p style="{paragraph}">Adjuntamos la tarjeta con el código en PDF '
                f"(<b>{escape_text(card_name)}</b>). Imprímela, recórtala por el borde y "
                "ponla con el regalo.</p>",
            )
            + "</td></tr>"
        )

    html = f"""<!doctype html>
<html lang="es"><body style="margin:0;padding:0;background-color:{colors.bg}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
 bgcolor="{colors.bg}" style="background-color:{colors.bg}">
<tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"
 bgcolor="{colors.card}" style="max-width:560px;width:100%;background-color:{colors.card};
 border:1px solid {colors.border};border-radius:16px">
  <tr><td align="center" style="padding:28px 28px 16px;text-align:center;
   background-color:{colors.head};border-radius:16px 16px 0 0">
    <p style="margin:0 0 12px;font-family:{SANS};font-size:11px;letter-spacing:.18em;
     text-transform:uppercase;color:{colors.muted}">{escape_text(palette.name)} · Tu carta está lista</p>
    <h1 style="margin:0;font-family:{SERIF};font-size:26px;line-height:1.2;font-weight:600;
     color:{colors.text}">{THANKS}</h1>
  </td></tr>
  <tr><td align="center" style="padding:16px 28px 0;text-align:center">
    <p style="{paragraph}">Tu carta «{safe_title}» ya está publicada y lista para entregar.</p>
    <p style="{small}">Escanea el código con la cámara o abre el enlace para verla.</p>
  </td></tr>
  <tr><td align="center" style="padding:18px 28px 0">
    <a href="{safe_url}" style="display:inline-block;background-color:{colors.accent};
     color:{colors.button_text};font-family:{SANS};font-size:15px;font-weight:600;
     text-decoration:none;padding:12px 26px;border-radius:999px">Ver la carta</a>
  </td></tr>
  <tr><td align="center" style="padding:20px 28px 0;text-align:center">
    <img src="{escape_attr(qr_source)}" width="150" height="150" alt="Código QR hacia la carta"
     style="display:block;margin:0 auto;border:1px solid {colors.border};border-radius:12px;
     padding:6px;background-color:#ffffff" />
    <p style="margin:10px 0 0;font-family:{SANS};font-size:12px;word-break:break-all">
      <a href="{safe_url}" style="color:{colors.accent}">{safe_url}</a>
    </p>
  </td></tr>
  <tr><td align="center" style="padding:22px 28px 0;text-align:center">
    <p style="margin:0;font-family:{SCRIPT};font-style:italic;font-size:24px;line-height:1.2;
     color:{colors.accent}">{WISH}</p>
  </td></tr>
  <tr><td style="padding:20px 28px 0">{tips_html}</td></tr>
  {card_html}
  <tr><td align="center" style="padding:22px 28px 28px;text-align:center">
    <p style="{small}">Carta {escape_text(letter_id)} · versión publicada {version}</p>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""

    text_lines = [
        THANKS,
        f"Tu carta «{plain_title}» ya está publicada y lista para entregar.",
        "",
        f"Ábrela aquí: {sanitize(public_url)}",
        "",
        WISH,
        "",
        "Para acompañar tu carta:",
        GENERAL_MESSAGE,
        f"- Flores: {tips.flowers}",
        f"- Algo dulce: {tips.sweets}",
        f"- Un detalle: {tips.touch}",
        *HOW_TO_DELIVER,
    ]
    if card_name:
        text_lines += [
            "",
            f"Tu tarjeta para imprimir va adjunta en PDF ({sanitize(card_name)}): imprímela, "
            "recórtala por el borde y ponla con el regalo.",
        ]
    text_lines += ["", f"Carta {sanitize(letter_id)} · versión publicada {version}"]
    return Mail(
        to=to,
        # El asunto es una cabecera: un salto de línea aquí sería inyección de
        # cabeceras SMTP, así que se limpia antes de recortarlo.
        subject=safe_header(f"Gracias por tu compra: tu carta «{plain_title}» ya está lista")[:120],
        html=html,
        text="\n".join(text_lines) + "\n",
        attachments=attachments,
        inline=inline,
    )
