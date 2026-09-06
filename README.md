# Zyvencore Valentine API — base 0.2.0

Backend FastAPI con PostgreSQL/SQLAlchemy asíncrono, Alembic y autenticación por **correo/contraseña y Google**. Implementación local de [spec 001](specs/001-base-auth/spec.md); no incluye pagos, cartas, correo, cédulas ni cambios de infraestructura.

La [auditoría inicial](docs/AUDITORIA.md) y el [plan comercial](docs/INTEGRACION_Y_PLAN.md) son antecedentes. Para lo implementado, prevalecen spec 001 y este README. La referencia ecotur-asoprado-api y el aporte Iops.md no se modificaron.

## Endpoints implementados

| Método/ruta | Resultado |
|---|---|
| GET /; GET /health/live | Vida del proceso, sin consultar DB |
| GET /health/ready | DB accesible y migración 0001_base_auth aplicada; 503 si no |
| GET /api/v1/auth/csrf | Token CSRF y cookie firmada, no cacheables |
| POST /api/v1/auth/register | Registro con email, password y name; 201 |
| POST /api/v1/auth/login | Sesión opaca en cookie HttpOnly y perfil |
| POST /api/v1/auth/google | Verifica ID token Google y abre sesión |
| GET /api/v1/me | Perfil propio autenticado |
| POST /api/v1/auth/logout | Revoca sesión actual y elimina cookie |
| GET /docs; GET /openapi.json | Contrato generado por FastAPI |

Registro no inicia sesión. Contraseña de 12..128 caracteres con Argon2, nombre no vacío hasta 120; email normalizado y único. No se envían correos de verificación: emailVerified=false para registro con contraseña. La cédula futura será dato privado aparte, nunca credencial ni ID interno.

Todas las escrituras necesitan cookie CSRF y header X-CSRF-Token obtenidos con GET /auth/csrf. El navegador debe usar credentials: include. Cookie de sesión revocable: hash persistido, vencimiento configurable, rotación al login, revocación al logout. En develop/staging/production usa Secure y prefijo __Host-. No se devuelven tokens bearer/JWT al cliente.

Errores devuelven code, message, fieldErrors y requestId, sin contraseña/credential ni detalles de SQL. Duplicado: 409; inválido: 422; sin sesión o credenciales incorrectas: 401; CSRF: 403; límite: 429; DB/Google no disponibles: 503.

## Configuración local

Python 3.11+; versión validada 3.12. PostgreSQL local y un entorno virtual exclusivo. Instalar desde la raíz:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c requirements.lock -e ".[test]"
Copy-Item .env.example .env
```

Completar .env con una DB **local propia** y SESSION_SECRET aleatorio de al menos 32 caracteres. Ejemplo de generación para la sesión actual, sin imprimir el valor:

```powershell
$env:SESSION_SECRET = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

No usar credenciales de la referencia. DATABASE_URL usa postgresql+asyncpg, sin parámetros de consulta; TLS se configura mediante DB_SSL_MODE=verify-full y DB_SSL_CA_FILE opcional. Contraseñas con caracteres reservados requieren percent-encoding en la URL. En Compose usar contraseña local URL-safe.

Después de verificar que DATABASE_URL apunta a la base local deseada:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

No hay create_all ni migraciones al inicio. Readiness permanece 503 hasta migrar. Una base existente con tablas ajenas debe revisarse con su dueño antes de cualquier migración; no usar stamp para ocultar conflictos.

requirements.lock fija el conjunto resuelto en Windows/Python 3.12, usado como restricciones (-c); contiene también herramientas de test, pero Docker instala solo dependencias del proyecto. Linux/contenedor queda pendiente de validación real.

## Docker local

Compose crea su propia base interna sin publicar el puerto de PostgreSQL. API solo expuesta en 127.0.0.1:8000. Definir POSTGRES_PASSWORD local y SESSION_SECRET en .env; no poner DATABASE_URL remoto en Compose.

```powershell
docker compose up -d db
docker compose build api
docker compose run --rm api alembic upgrade head
docker compose up -d api
```

No ejecutar downgrade en datos que se deban conservar. Dockerfile usa usuario sin privilegios, excluye secretos, respeta PORT (por defecto 8000) y WEB_CONCURRENCY. No inicia migraciones ni modifica pipelines. Configuración Compose validada; construcción/ejecución pendientes por motor Docker no disponible en esta sesión.

## Google

Configurar GOOGLE_CLIENT_ID de cliente OAuth web y orígenes autorizados en Google. Flujo callback JSON: obtener csrfToken, usar ese valor como nonce en GIS, obtener credential y enviarlo a POST /auth/google junto a X-CSRF-Token y cookies. El POST HTML directo de GIS no es la interfaz de esta API.

Verifica firma, audiencia, emisor, vencimiento, nonce y email_verified. Se identifica por sub. Si el email coincide con otra cuenta sin esa identidad Google, responde ACCOUNT_LINK_REQUIRED: nunca fusiona automáticamente. Vinculación explícita de cuentas, recuperación/cambio de contraseña y verificación por correo quedan fuera de este corte. Un usuario Google no puede iniciar sesión con una contraseña arbitraria.

Sin GOOGLE_CLIENT_ID, responde GOOGLE_NOT_CONFIGURED. No se configuró un cliente real ni se accedió a una cuenta Google. Pruebas criptográficas usan claves efímeras propias y simulan únicamente el transporte de certificados; no son un login real con Google.

## Pruebas reproducibles y Postman

El runner crea una base PostgreSQL nueva en loopback, aleatoria y exclusiva; ejecuta upgrade/check/downgrade/upgrade y pytest; luego detiene su propia instancia incluso ante fallo. No utiliza una base existente. Requiere PostgreSQL instalado; LOCAL_POSTGRES_BIN permite indicar su directorio bin (por defecto PostgreSQL 18 en Windows).

```powershell
.\.venv\Scripts\python.exe scripts/validate_local.py
npm.cmd install --prefix .local-validation/tools newman@6
.\.venv\Scripts\python.exe scripts/validate_local.py --postman
.\.venv\Scripts\python.exe -m ruff check app tests scripts alembic
```

[Importar colección](postman/zyvalentine.postman_collection.json) y [entorno local vacío](postman/local.postman_environment.json). Mantener cookie jar habilitado. Carpeta Automated local: requiere Google sin configurar y una base de pruebas descartable; genera email/contraseña ficticios en memoria y realiza 14 solicitudes. Para repetir manualmente, vaciar email/password del entorno y no exportar valores de ejecución.

La carpeta Google manual requiere cliente configurado y credential fresca obtenida con nonce correcto. Sus pruebas de éxito no se ejecutan automáticamente ni deben sustituirse por tokens ficticios de producción.

[Resultados y límites](specs/001-base-auth/validation.md).

## Coordinación con infraestructura

[Contrato operativo propuesto](docs/BASE_AUTH_OPERACION.md): variables, pool, migración y pendientes del compañero. APP_ENV usa local/develop/staging/production (main corresponde a production); rama actual develop-julian, sin cambio/commit/push.

No se implementó la infraestructura remota ni se confirmó capacidad de Azure. La regla futura continúa siendo varias compras por usuario y una compra pagada por carta, sin implementaciones comerciales en esta base.
