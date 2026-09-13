# Plan 001

1. Settings, ciclo de vida, engine/sesión por aplicación, errores y salud (B01/B02/Q01).
2. Entidades users/sessions y migración Alembic inicial (B03).
3. Repositorio y servicio auth, hashing, CSRF, cookies, Google con verificador inyectable, límites (A01..A06).
4. Docker local sin auto-migración, ejemplo de entorno, documentación operativa y Postman.
5. Verificar en PostgreSQL local efímero, ejecutar tests y colección; actualizar evidencia y tareas.

Fuentes consultadas: https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/ (Argon2), https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html (AsyncSession por tarea), https://developers.google.com/identity/gsi/web/guides/verify-google-id-token (verificación servidor). Sesiones opacas y reglas de negocio son decisiones de implementación propias.
