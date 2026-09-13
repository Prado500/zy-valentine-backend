# Especificación 001: base y autenticación

Estado: autorizada para implementación local el 2026-09-05. Usuario confirma **ambas** opciones: contraseña y Google. Rama existente develop-julian; develop es integración, staging/main son referencias de entorno. No cambiar ramas ni pipelines.

## Evidencia y límites

Iops.md es un aporte del compañero, no modificarlo. Describe Mercado Pago y cartas; no aporta DSN, TLS, variables Azure ni contrato de despliegue. Su presupuesto declarado de 20 conexiones se modela como límite global parametrizable, con reserva para administración/migraciones; no prueba 8190 usuarios ni convierte una conexión en 12 consultas paralelas. IOP #5 queda subordinado a la regla confirmada: varias compras por usuario, una compra pagada por carta.

ecotur-asoprado-api sirve de referencia para FastAPI/SQLAlchemy/capas y nombres DATABASE_URL. No copiar su dominio, roles ni tokens cuyo sub es email. No modificar referencia/frontend. No integrar pagos, cartas, correo, documentos de identidad ni Azure remoto en este corte.

## Requisitos verificables

- B01: Settings por APP_ENV local/develop/staging/production, URL privada, CORS explícito, TLS configurable, pool acotado por workers/réplicas y reserva. Entornos remotos exigen TLS y cookies seguras. No migraciones al arrancar.
- B02: GET / y /health/live vivos sin DB; /health/ready verifica DB y revisión de migración esperada, responde 503 saneado si falla.
- B03: modelo users con UUID estable, email normalizado único, contraseña Argon2 opcional, google_sub único opcional, is_active y timestamps. Sesiones opacas revocables, almacenadas como hash. No cédula como identidad/credencial; se implementará aparte cuando se definan reglas.
- A01: GET /api/v1/auth/csrf emite token firmado y cookie; las escrituras requieren coincidencia cookie/header X-CSRF-Token y token vigente. JSON, CORS/origen explícito; Secure/HttpOnly en sesión, prefijo __Host- en entornos remotos.
- A02: POST /auth/register con email, password (12..128 caracteres), name; 201 sin iniciar sesión, 409 duplicado, 422 inválido. Rechazar campos extra/privilegios. No verificación por correo implementada: emailVerified=false.
- A03: POST /auth/login; contraseña correcta -> cookie de sesión y perfil; incorrecta/desactivado -> 401 genérico. Argon2 fuera del event loop; limitar concurrencia de hashing. Rotar sesión previa al login.
- A04: POST /auth/google con credential; Google verifica firma/aud/iss/exp, email_verified y nonce del CSRF. Nueva identidad -> usuario; existente por sub -> misma identidad. Coincidencia solo por email -> 409 ACCOUNT_LINK_REQUIRED, sin fusión automática. Sin client ID -> 503 GOOGLE_NOT_CONFIGURED. Vinculación explícita queda fuera del corte.
- A05: GET /me solo muestra perfil propio sin hash, cédula ni tokens; 401 sin sesión/vencida/revocada/usuario desactivado. POST /auth/logout revoca sesión actual y borra cookie; repetición segura.
- A06: límites de intentos por IP y proceso, acotados en memoria; no confiar en X-Forwarded-For arbitrario. Protección global entre réplicas queda a coordinación con infraestructura; no afirmar cobertura distribuida.
- Q01: errores JSON code/message/fieldErrors/requestId sin secretos. OpenAPI, pruebas de contratos, CORS, CSRF, auth, migraciones y colección Postman importable.

Las rutas /auth/* y /me llevan /api/v1. CSRF y sesiones evitan cambiar la propuesta previa a JWT bearer. Google usa callback JSON, no POST directo HTML GIS. Frontend futuro debe solicitar CSRF, usarlo como nonce GIS y enviarlo en header con credentials include.

## Criterios de salida

Pruebas unitarias/integración aisladas; upgrade/downgrade/upgrade y revisión de drift en PostgreSQL efímero local; Postman/Newman contra API local. Docker construir/ejecutar si daemon disponible; de lo contrario reportar límite. Ninguna URL remota de la referencia se usa en pruebas. No commits ni push.
