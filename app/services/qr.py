"""Generación de código QR del visor público (segno, sin dependencias binarias)."""

import io

import segno


def qr_png(url: str, scale: int = 6) -> bytes:
    buffer = io.BytesIO()
    segno.make(url, error="m").save(buffer, kind="png", scale=scale, border=2)
    return buffer.getvalue()
