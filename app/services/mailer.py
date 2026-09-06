"""Puerto de correo, plantilla HTML de entrega y documento autónomo adjunto.

El correo lleva botón al visor público, código QR al mismo visor e identificador
de carta y versión publicada. El estado del envío se persiste en
``letter_deliveries``; un reintento crea otro intento de envío, nunca otra carta.

Además del cuerpo, se adjunta un **documento HTML autónomo** con las fotos incrustadas
en Base64 (``render_letter_document``). Es la copia que el destinatario conserva aunque
el visor deje de estar disponible: no pide nada a la red al abrirse. Construirlo es
trabajo de CPU sobre bytes, así que el worker lo delega a ``anyio.to_thread.run_sync``
en vez de hacerlo en el bucle de eventos.
"""

import base64
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage

from anyio import to_thread

from app.core.config import Settings
from app.core.html import escape_attr, escape_text, safe_header, sanitize

# Tipos que el visor y el correo saben mostrar. Coincide con
# `app.services.storage.ALLOWED_CONTENT_TYPES`, declarado aquí para que el módulo de
# correo no dependa del de almacenamiento.
ALLOWED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


@dataclass(frozen=True)
class Attachment:
    """Adjunto de correo. ``filename`` viaja en una cabecera MIME, así que quien lo
    construya debe pasarlo ya saneado con :func:`app.core.html.safe_file_name`."""

    filename: str
    content: bytes
    maintype: str = "text"
    subtype: str = "html"


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    html: str
    text: str
    attachments: tuple[Attachment, ...] = field(default=())


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


class SmtpMailer(Mailer):
    def __init__(self, settings: Settings):
        self.settings = settings

    def _send(self, message: Mail) -> str:
        settings = self.settings
        email = EmailMessage()
        email["From"] = settings.sender_address
        email["To"] = message.to
        email["Subject"] = message.subject
        email.set_content(message.text)
        email.add_alternative(message.html, subtype="html")
        for item in message.attachments:
            email.add_attachment(
                item.content,
                maintype=item.maintype,
                subtype=item.subtype,
                filename=item.filename,
            )
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
) -> Mail:
    safe_title = escape_text(title)
    safe_name = escape_text(recipient_name)
    safe_url = escape_attr(public_url)
    attached = (
        '<p style="margin:16px 0 0;font-size:13px;color:#7a6a72">'
        "Adjuntamos tu carta como archivo HTML: ábrelo cuando quieras, incluso sin "
        "conexión.</p>"
        if attachments
        else ""
    )
    html = f"""<!doctype html>
<html lang="es"><body style="margin:0;padding:24px;background:#fdf2f6;
 font-family:Segoe UI,Helvetica,Arial,sans-serif;color:#3b2b33">
  <div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:16px;padding:28px">
    <h1 style="font-size:20px;margin:0 0 12px">Tienes una carta, {safe_name}</h1>
    <p style="margin:0 0 20px;font-size:15px;line-height:1.5">{safe_title}</p>
    <p style="margin:0 0 24px">
      <a href="{safe_url}" style="display:inline-block;background:#d6336c;color:#ffffff;
         text-decoration:none;padding:12px 22px;border-radius:999px;font-weight:600">
        Ver mi carta
      </a>
    </p>
    <p style="margin:0 0 8px;font-size:13px">O escanea este código:</p>
    <img src="{escape_attr(qr_source)}" width="150" height="150"
         alt="Código QR hacia la carta" style="display:block;border:0" />
    <p style="margin:12px 0 0;font-size:12px;word-break:break-all">
      <a href="{safe_url}" style="color:#d6336c">{safe_url}</a>
    </p>
    {attached}
    <p style="margin:20px 0 0;font-size:12px;color:#7a6a72">
      Carta {escape_text(letter_id)} · versión publicada {version}
    </p>
  </div>
</body></html>"""
    plain_name = sanitize(recipient_name)
    plain_title = sanitize(title)
    text = (
        f"Tienes una carta, {plain_name}.\n{plain_title}\n\n"
        f"Ábrela aquí: {sanitize(public_url)}\n\n"
        f"Carta {sanitize(letter_id)} · versión publicada {version}\n"
    )
    return Mail(
        to=to,
        # El asunto es una cabecera: un salto de línea aquí sería inyección de
        # cabeceras SMTP, así que se limpia antes de recortarlo.
        subject=safe_header("Tu carta: " + plain_title)[:120],
        html=html,
        text=text,
        attachments=attachments,
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
) -> str:
    """Documento HTML autónomo: fotos en Base64, sin una sola petición a la red.

    ``photos`` son tuplas ``(pie, tipo, bytes)`` en orden de posición. Base64 infla los
    bytes un 33 %, así que se incrustan mientras quepan en ``max_bytes`` y las que no
    caben se omiten con un aviso visible: es preferible una carta sin la última foto a
    un correo que el servidor rechace por tamaño.

    Función síncrona a propósito: quien la llama la ejecuta en un hilo aparte
    (``anyio.to_thread.run_sync``) para no bloquear el bucle de eventos del worker.
    """
    safe_title = escape_text(title)
    safe_name = escape_text(recipient_name)
    safe_url = escape_attr(public_url)
    paragraphs = "".join(
        f'<p style="margin:0 0 14px;font-size:16px;line-height:1.7;white-space:pre-wrap">'
        f"{escape_text(block)}</p>"
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
            f'<figcaption style="margin-top:6px;font-size:13px;color:#7a6a72">'
            f"{escape_text(caption)}</figcaption></figure>"
        )
    if omitted:
        figures.append(
            '<p style="font-size:13px;color:#7a6a72">'
            f"{omitted} foto(s) no caben en este archivo; ábrelas en el enlace de la carta.</p>"
        )

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>{safe_title}</title></head>
<body style="margin:0;padding:24px;background:#fdf2f6;
 font-family:Segoe UI,Helvetica,Arial,sans-serif;color:#3b2b33">
  <main style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:16px;padding:32px">
    <p style="margin:0 0 4px;font-size:13px;color:#7a6a72">Para {safe_name}</p>
    <h1 style="font-size:24px;margin:0 0 24px">{safe_title}</h1>
    {paragraphs}
    <div style="margin:28px 0 0">{"".join(figures)}</div>
    <hr style="border:0;border-top:1px solid #f0dfe6;margin:28px 0" />
    <p style="margin:0 0 8px;font-size:13px">Esta carta también vive en línea:</p>
    <p style="margin:0 0 12px;font-size:13px;word-break:break-all">
      <a href="{safe_url}" style="color:#d6336c">{safe_url}</a>
    </p>
    <img src="{escape_attr(qr_source)}" width="130" height="130"
         alt="Código QR hacia la carta" style="display:block;border:0" />
    <p style="margin:16px 0 0;font-size:12px;color:#7a6a72">
      Carta {escape_text(letter_id)} · versión publicada {version}
    </p>
  </main>
</body></html>"""
