"""Servicio de aplicación de cuentas: registro, sesión y cierre de sesión.

Mismo criterio que ``app.services.commerce``: los routers no ven la sesión de base
de datos ni los repositorios. Aquí se orquesta lo que antes hacía el router de
autenticación —incluida la consulta que resolvía la cookie de sesión, que vivía en
``app/api/dependencies.py`` escrita en SQL a mano—.

Lo que **no** hace este servicio es tocar cookies. Emitirlas, marcarlas ``Secure``
o borrarlas es una decisión del protocolo HTTP y se queda en el router: aquí solo
se devuelve el token opaco que el router guardará donde corresponda.
"""

from dataclasses import dataclass

from anyio import CapacityLimiter, to_thread
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.security import digest
from app.models.user import User
from app.repositories import users
from app.schemas.auth import GoogleLogin, Login, Register
from app.services import auth, consents, identity
from app.services.google import GoogleVerifier

# Tope defensivo para la cookie de sesión: el token que emitimos son 43 caracteres
# (`secrets.token_urlsafe(32)`). Cualquier cosa mucho más larga no es nuestra y no
# merece un digest ni una consulta.
MAX_SESSION_TOKEN = 128


@dataclass(frozen=True, slots=True)
class AccountService:
    db: AsyncSession
    settings: Settings
    hash_limiter: CapacityLimiter
    google_limiter: CapacityLimiter
    google_verifier: GoogleVerifier

    async def register(
        self, payload: Register, ip: str | None = None, user_agent: str | None = None
    ) -> User:
        """Usuario, documento y consentimiento en una sola transacción.

        Si esto se partiera en dos commits podría quedar una cuenta sin consentimiento,
        que no es un fallo parcial aceptable: es un incumplimiento de la Ley 1581.

        **Orden deliberado:** el correo se comprueba primero, dentro de ``auth.register``.
        Así un correo repetido sigue devolviendo ``EMAIL_IN_USE``, que es lo que el
        frontend usa para mandar a la persona a iniciar sesión. Si el conflicto fuera del
        documento, el código es ``REGISTRATION_CONFLICT`` y el frontend no intenta entrar.
        """
        user = await auth.register(self.db, payload, self.hash_limiter)
        await identity.attach_document(
            self.db, self.settings, user, payload.documentType, payload.documentNumber
        )
        consents.attach_consent(self.db, user, payload.acceptedTermsVersion, ip, user_agent)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ApiError(409, "REGISTRATION_CONFLICT", identity.CONFLICT_MESSAGE) from None
        # Tras el commit: ``created_at`` lo pone el servidor y la respuesta lo lleva.
        await self.db.refresh(user)
        return user

    async def login(self, payload: Login) -> User:
        return await auth.login(self.db, payload, self.hash_limiter)

    async def google_login(self, payload: GoogleLogin, nonce: str) -> User:
        """Inicio de sesión con Google. Sin cliente configurado, 503 explícito."""
        if not self.settings.google_client_id:
            raise ApiError(503, "GOOGLE_NOT_CONFIGURED", "Google todavía no está configurado.")
        claims = await to_thread.run_sync(
            self.google_verifier.verify,
            payload.credential,
            self.settings.google_client_id,
            nonce,
            limiter=self.google_limiter,
        )
        return await auth.google_user(self.db, claims)

    async def open_session(self, user: User, previous: str | None) -> str:
        """Crea la sesión y devuelve su token opaco; revoca la anterior si la había."""
        return await auth.new_session(self.db, user, previous, self.settings.session_minutes)

    async def close_session(self, token: str | None) -> None:
        if token and len(token) <= MAX_SESSION_TOKEN:
            await users.delete_session(self.db, digest(token))

    async def session_user(self, token: str | None) -> User:
        """Resuelve la cookie de sesión a un usuario activo, o 401.

        La cookie no se compara nunca en claro: se guarda y se busca su digest, así
        que un volcado de ``auth_sessions`` no entrega sesiones utilizables.
        """
        if token and len(token) <= MAX_SESSION_TOKEN:
            user = await users.by_session(self.db, digest(token))
            if user:
                return user
        raise ApiError(401, "UNAUTHENTICATED", "Inicia sesión para continuar.")
