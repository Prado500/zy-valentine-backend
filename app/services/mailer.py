"""Puerto de correo y plantilla HTML de entrega.

El correo lleva botón al visor público, código QR al mismo visor e identificador
de carta y versión publicada. El estado del envío se persiste en
``letter_deliveries``; un reintento crea otro intento de envío, nunca otra carta.
"""

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape

from anyio import to_thread

from app.core.config import Settings


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    html: str
    text: str


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
) -> Mail:
    safe_title = escape(title)
    safe_name = escape(recipient_name)
    safe_url = escape(public_url, quote=True)
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
    <img src="{escape(qr_source, quote=True)}" width="150" height="150"
         alt="Código QR hacia la carta" style="display:block;border:0" />
    <p style="margin:20px 0 0;font-size:12px;color:#7a6a72">
      Carta {escape(letter_id)} · versión publicada {version}
    </p>
  </div>
</body></html>"""
    text = (
        f"Tienes una carta, {recipient_name}.\n{title}\n\n"
        f"Ábrela aquí: {public_url}\n\nCarta {letter_id} · versión publicada {version}\n"
    )
    return Mail(to=to, subject=f"Tu carta: {title}"[:120], html=html, text=text)
