# Zyvencore Valentine API — 0.3.0

Backend FastAPI con PostgreSQL/SQLAlchemy asíncrono, Alembic y autenticación por
**correo/contraseña y Google mediante sesiones opacas revocables**. Incluye el dominio
comercial: cédula privada, compras, verificación de pago en servidor, cartas, fotos,
enlace público con QR y correo de entrega.

**No se usa JWT.** Las variables `JWT_SECRET_KEY`, `ALGORITHM` y
`ACCESS_TOKEN_EXPIRE_MINUTES` que aparecen en la configuración de Azure son heredadas
del proyecto de referencia y esta API no las lee. Ver
[matriz de configuración](docs/MATRIZ_CONFIGURACION.md).

> `docs/AZURECONF.md` contiene contraseñas de base de datos en texto plano. Trátalas
> como comprometidas: rótalas y muévelas a Key Vault. Este repositorio no contiene
> ningún valor de Azure.

**¿Vas a trabajar sobre este código?** Empieza por la
[guía técnica](docs/GUIA_TECNICA.md): explica la arquitectura, qué hace cada archivo,
cómo fluye una petición y por qué se tomó cada decisión.

Documentos: [guía técnica](docs/GUIA_TECNICA.md),
[spec 001 base+auth](specs/001-base-auth/spec.md),
[spec 002 configuración+comercio](specs/002-comercio/spec.md),
[matriz de configuración](docs/MATRIZ_CONFIGURACION.md),
[dominio comercial](docs/DOMINIO_COMERCIAL.md),
[operación y handoff](docs/BASE_AUTH_OPERACION.md).
`ecotur-asoprado-api` es solo referencia de organización; no se modificó, igual que
`Iops.md` y los pipelines.

## Endpoints

### Base y autenticación

| Método/ruta | Resultado |
|---|---|
| `GET /`, `GET /health/live` | Vida del proceso, sin consultar la base |
| `GET /health/ready` | Base accesible y migración `0002_commerce` aplicada; 503 si no |
| `GET /api/v1/auth/csrf` | Token CSRF y cookie firmada |
| `POST /api/v1/auth/register` | Registro (201, no inicia sesión) |
| `POST /api/v1/auth/login` | Sesión opaca en cookie HttpOnly |
| `POST /api/v1/auth/google` | Verifica el ID token de Google y abre sesión propia |
| `GET /api/v1/me` | Perfil propio |
| `POST /api/v1/auth/logout` | Revoca la sesión actual |

### Dominio comercial

| Método/ruta | Resultado |
|---|---|
| `PUT`/`GET /api/v1/me/identity-document` | Cédula privada; devuelve solo tipo y últimos 4 dígitos |
| `POST /api/v1/purchases` | Intención de pago idempotente (201 nueva, 200 si repite la clave) |
| `GET /api/v1/purchases`, `GET /api/v1/purchases/{id}` | Compras propias con estado y `hasLetter` |
| `POST /api/v1/purchases/{id}/verify` | **Verificación en servidor** del pago |
| `POST /api/v1/webhooks/mercadopago` | Webhook firmado, idempotente y monótono |
| `POST /api/v1/letters` | Crea la carta de una compra pagada (200 si ya existía) |
| `GET /api/v1/letters`, `GET /api/v1/letters/{id}` | "Mis cartas": pago, entrega y borradores |
| `PATCH /api/v1/letters/{id}` | Edita solo mientras sea borrador |
| `POST`/`DELETE /api/v1/letters/{id}/photos…` | Fotos ordenadas, validadas por firma binaria |
| `POST /api/v1/letters/{id}/publish` | Publica, genera enlace y QR, y envía el correo |
| `POST /api/v1/letters/{id}/deliveries` | Reenvío; no consume otra compra |
| `GET /api/v1/letters/{id}/qr.png` | QR de la carta (dueño) |
| `GET /api/v1/public/letters/{slug}` | Visor público: sin usuario, sin correo, sin cédula |
| `GET /api/v1/public/letters/{slug}/photos/{n}`, `/qr.png` | Fotos y QR públicos |
| `GET /docs`, `GET /openapi.json` | Contrato generado por FastAPI |

Reglas: un usuario puede tener muchas compras; **una compra pagada habilita exactamente
una carta**; consultar o reenviar no consume otra compra; el doble clic, varias pestañas
o los webhooks repetidos no crean una segunda carta. Detalle, estados, límites de
validación y códigos de error en [dominio comercial](docs/DOMINIO_COMERCIAL.md).

Todas las escrituras requieren la cookie CSRF y la cabecera `X-CSRF-Token`; el
navegador debe usar `credentials: include`. El webhook es la única excepción y se
autentica por firma HMAC. Los errores devuelven `code`, `message`, `fieldErrors` y
`requestId`, sin contraseñas, credenciales ni detalles de SQL.

## Configuración local

Python 3.11+ (validado en 3.12.14), PostgreSQL local y un entorno virtual propio:

```bash
python -m venv .venv
.venv/bin/python -m pip install -c requirements.lock -e ".[test]"
cp .env.example .env
```

`.env` ya está en `.gitignore`. Genera los secretos locales sin imprimirlos:

```bash
.venv/bin/python -c "import secrets; print('SESSION_SECRET=' + secrets.token_urlsafe(48))" >> .env
.venv/bin/python -c "import secrets; print('PII_HMAC_KEY=' + secrets.token_urlsafe(48))" >> .env
```

`DATABASE_URL` usa `postgresql+asyncpg`. Si la cadena viene de Azure con
`?sslmode=require` o `?ssl=require`, la aplicación la acepta y la traduce a
`DB_SSL_MODE=verify-full`; cualquier otro parámetro aborta el arranque y ningún entorno
remoto puede degradar TLS. Las contraseñas con caracteres reservados van
percent-encoded.

```bash
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

No hay `create_all` ni migraciones al arrancar; readiness responde 503 hasta migrar.
Una base con tablas ajenas debe revisarse con su responsable antes de migrar.

Integraciones opcionales: `PAYMENT_PROVIDER=mercadopago` (token y secreto de webhook),
`STORAGE_BACKEND=azure` (requiere `pip install '.[azure]'`), `MAIL_BACKEND=smtp`,
`GOOGLE_CLIENT_ID`. Sin ellas, la API responde 503 explícito en los puntos que las
necesitan, y el resto del flujo funciona.

## Docker local

```bash
docker compose up -d db
docker compose build api
docker compose run --rm api alembic upgrade head
docker compose up -d api
```

Compose crea su propia base, no publica el puerto de PostgreSQL y expone la API solo en
`127.0.0.1:8000`. Las fotos van a un volumen propio. No pongas un `DATABASE_URL` remoto
en Compose. Verificado en Linux con PostgreSQL 15 y la imagen construida.

## Pruebas y Postman

El runner crea su propio PostgreSQL efímero (contenedor Docker o binarios locales),
ejecuta `upgrade/check/downgrade/upgrade` y pytest, y lo destruye al terminar. Nunca usa
una base existente ni lee `.env`.

```bash
.venv/bin/python scripts/validate_local.py            # binarios locales si los hay
.venv/bin/python scripts/validate_local.py --docker   # fuerza el contenedor efímero
npm install --prefix .local-validation/tools newman@6
.venv/bin/python scripts/validate_local.py --docker --postman
.venv/bin/python -m ruff check app tests scripts alembic
```

[Colección Postman](postman/zyvalentine.postman_collection.json) y
[entorno local sin secretos](postman/local.postman_environment.json), con el cookie jar
habilitado. Las carpetas `Automated local` y `Commerce local` se ejecutan sin Google,
Mercado Pago, Azure ni correo reales. `Google manual` y `Commerce manual` requieren esas
integraciones configuradas y no se ejecutan automáticamente.

[Resultados y límites](specs/002-comercio/validation.md).

## Coordinación con infraestructura

Pendientes externos, rotación de secretos y presupuesto de conexiones en
[la matriz de configuración](docs/MATRIZ_CONFIGURACION.md). No se ejecutaron migraciones
ni consultas contra Azure, y no se modificaron pipelines ni recursos remotos.
