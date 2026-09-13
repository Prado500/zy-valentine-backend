"""Registro de la autorización de tratamiento de datos.

No hay ``update``: una autorización no se modifica, se vuelve a otorgar. Cuando cambie
la política, sube su versión y esto insertará otra fila. Lo que se guarda es qué texto
exacto aceptó la persona (su checksum), cuándo, desde dónde y con qué navegador.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app import legal
from app.core.errors import ApiError
from app.models.commerce import UserConsent
from app.models.user import User

# Tope de la columna: un agente de navegador larguísimo no puede tumbar un alta.
MAX_USER_AGENT = 255


def attach_consent(
    db: AsyncSession, user: User, version: str, ip: str | None, user_agent: str | None
) -> UserConsent:
    """Añade el consentimiento a la sesión **sin hacer commit**.

    La versión se comprueba contra la vigente: aceptar una política que ya no es la
    nuestra no es consentimiento informado, es un cliente desactualizado.
    """
    if version != legal.TERMS_VERSION:
        raise ApiError(
            422,
            "TERMS_VERSION_MISMATCH",
            "Los términos cambiaron. Recarga la página y vuelve a intentarlo.",
        )
    record = UserConsent(
        user_id=user.id,
        kind=legal.TERMS_KIND,
        document_version=legal.TERMS_VERSION,
        document_checksum=legal.TERMS_CHECKSUM,
        ip_address=ip or None,
        user_agent=user_agent[:MAX_USER_AGENT] if user_agent else None,
    )
    db.add(record)
    return record
