"""Sobre cifrado del número de documento.

El HMAC (``document_hash``) sirve para saber si un documento ya está en otra cuenta
sin guardarlo; pero de un HMAC no se recupera nada, y para emitir una factura
electrónica ante la DIAN hace falta el número real. Por eso se guarda además un
sobre AES-256-GCM.

La AAD es el ``user_id``: un sobre copiado a otra fila no se abre. Y el nonce es
aleatorio en cada cifrado, así que dos veces el mismo número dan bytes distintos:
el cifrado nunca puede usarse como índice.
"""

import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings

# 96 bits, el tamaño de nonce recomendado para GCM.
NONCE_BYTES = 12


def seal(settings: Settings, user_id: uuid.UUID, number: str) -> bytes:
    """Cifra el número. Devuelve ``nonce || ciphertext+tag``."""
    nonce = os.urandom(NONCE_BYTES)
    sealed = AESGCM(settings.pii_cipher_key).encrypt(nonce, number.encode(), str(user_id).encode())
    return nonce + sealed


def open_sealed(settings: Settings, user_id: uuid.UUID, sealed: bytes | None) -> str | None:
    """Descifra el sobre. Devuelve ``None`` si no es de este usuario o está alterado."""
    if not sealed or len(sealed) <= NONCE_BYTES:
        return None
    try:
        plain = AESGCM(settings.pii_cipher_key).decrypt(
            sealed[:NONCE_BYTES], sealed[NONCE_BYTES:], str(user_id).encode()
        )
    except InvalidTag:
        return None
    return plain.decode()
