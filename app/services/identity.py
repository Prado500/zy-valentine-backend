"""Cédula del comprador como dato privado, nunca como credencial.

No autentica, no aparece en el visor público, no se devuelve completa y no se
usa como identificador interno: la identidad interna sigue siendo ``users.id``.
"""

import hashlib
import hmac

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.models.commerce import UserIdentityDocument
from app.models.user import User
from app.repositories import commerce
from app.schemas.commerce import IdentityDocumentInput


def document_fingerprint(settings: Settings, document_type: str, number: str) -> str:
    """HMAC con clave dedicada; el número en claro nunca se persiste."""
    message = f"{document_type}:{number.strip().upper()}".encode()
    return hmac.new(settings.pii_key, message, hashlib.sha256).hexdigest()


async def set_document(
    db: AsyncSession, settings: Settings, user: User, payload: IdentityDocumentInput
) -> UserIdentityDocument:
    fingerprint = document_fingerprint(settings, payload.documentType, payload.documentNumber)
    owner = await commerce.identity_by_hash(db, fingerprint)
    if owner and owner.user_id != user.id:
        raise ApiError(409, "DOCUMENT_IN_USE", "Ese documento ya está registrado en otra cuenta.")
    record = await commerce.identity_by_user(db, user.id)
    last4 = payload.documentNumber[-4:]
    if record:
        record.document_type = payload.documentType
        record.document_hash = fingerprint
        record.document_last4 = last4
    else:
        record = UserIdentityDocument(
            user_id=user.id,
            document_type=payload.documentType,
            document_hash=fingerprint,
            document_last4=last4,
        )
        db.add(record)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ApiError(
            409, "DOCUMENT_IN_USE", "Ese documento ya está registrado en otra cuenta."
        ) from None
    await db.refresh(record)
    return record
