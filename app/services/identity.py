"""Documento del comprador como dato privado, nunca como credencial.

No autentica, no aparece en el visor público, no se devuelve completo y no se
usa como identificador interno: la identidad interna sigue siendo ``users.id``.

Del número se guardan tres cosas y ninguna es el número en claro: su HMAC (índice
ciego para la unicidad entre cuentas), sus cuatro últimos dígitos y un sobre
AES-GCM atado al usuario, que es lo único de lo que se recupera para facturar.
"""

import hashlib
import hmac

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto, dian
from app.core.config import Settings
from app.core.errors import ApiError
from app.models.commerce import UserIdentityDocument
from app.models.user import User
from app.repositories import commerce
from app.schemas.commerce import IdentityDocumentInput

# Mensaje deliberadamente vago: confirmar "esa cédula ya está registrada" convierte
# el alta en un oráculo para averiguar qué documentos tienen cuenta. En el panel,
# donde ya hay sesión, sí se puede ser explícito.
CONFLICT_MESSAGE = "No pudimos crear la cuenta con esos datos. Si ya tienes cuenta, inicia sesión."
DOCUMENT_IN_USE = "Ese documento ya está registrado en otra cuenta."


def document_fingerprint(settings: Settings, document_type: int, number: str) -> str:
    """HMAC con clave dedicada; el número en claro nunca se persiste aquí.

    El prefijo es el token del catálogo, no el código, para que las huellas emitidas
    antes de la DIAN sigan valiendo. Ver ``app.core.dian.fingerprint_token``.
    """
    token = dian.fingerprint_token(int(document_type))
    message = f"{token}:{number.strip().upper()}".encode()
    return hmac.new(settings.pii_key, message, hashlib.sha256).hexdigest()


async def attach_document(
    db: AsyncSession, settings: Settings, user: User, document_type: int, number: str
) -> UserIdentityDocument:
    """Añade el documento a la sesión **sin hacer commit**.

    La transacción la cierra quien llama: el alta crea usuario, documento y
    consentimiento de una sola vez, y una cuenta sin consentimiento sería un agujero
    legal, no un fallo parcial aceptable.
    """
    fingerprint = document_fingerprint(settings, document_type, number)
    owner = await commerce.identity_by_hash(db, fingerprint)
    if owner and owner.user_id != user.id:
        raise ApiError(409, "REGISTRATION_CONFLICT", CONFLICT_MESSAGE)
    record = UserIdentityDocument(
        user_id=user.id,
        document_type=int(document_type),
        document_hash=fingerprint,
        document_last4=number[-4:],
        document_cipher=crypto.seal(settings, user.id, number),
    )
    db.add(record)
    return record


async def set_document(
    db: AsyncSession, settings: Settings, user: User, payload: IdentityDocumentInput
) -> UserIdentityDocument:
    """Corrige el documento desde el panel. Cierra su propia transacción."""
    fingerprint = document_fingerprint(settings, payload.documentType, payload.documentNumber)
    owner = await commerce.identity_by_hash(db, fingerprint)
    if owner and owner.user_id != user.id:
        raise ApiError(409, "DOCUMENT_IN_USE", DOCUMENT_IN_USE)
    record = await commerce.identity_by_user(db, user.id)
    last4 = payload.documentNumber[-4:]
    sealed = crypto.seal(settings, user.id, payload.documentNumber)
    if record:
        record.document_type = int(payload.documentType)
        record.document_hash = fingerprint
        record.document_last4 = last4
        record.document_cipher = sealed
    else:
        record = UserIdentityDocument(
            user_id=user.id,
            document_type=int(payload.documentType),
            document_hash=fingerprint,
            document_last4=last4,
            document_cipher=sealed,
        )
        db.add(record)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ApiError(409, "DOCUMENT_IN_USE", DOCUMENT_IN_USE) from None
    await db.refresh(record)
    return record
