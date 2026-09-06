"""Generación de código QR del visor público (segno, sin dependencias binarias)."""

import io

import segno


def qr_png(url: str, scale: int = 6) -> bytes:
    buffer = io.BytesIO()
    segno.make(url, error="m").save(buffer, kind="png", scale=scale, border=2)
    return buffer.getvalue()


def qr_data_uri(url: str, scale: int = 5) -> str:
    """QR embebido en el correo: no depende de que el cliente cargue imágenes remotas."""
    import base64

    return "data:image/png;base64," + base64.b64encode(qr_png(url, scale)).decode()
