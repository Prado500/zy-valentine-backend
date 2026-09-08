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

## Arquitectura por capas

```
app/api/routers/      HTTP y nada más: estado, cabeceras, cookies y CSRF
app/api/dependencies  la frontera: abre la sesión y arma los servicios
app/services/commerce CommerceService  -> orquesta el dominio comercial
app/services/accounts AccountService   -> orquesta registro y sesión
app/services/*        reglas de negocio (letters, purchases, deliveries, identity)
app/repositories/     SQLAlchemy 2.0; ningún SQL vive fuera de aquí
app/models/           modelos declarativos
alembic/              migraciones (nunca `Base.metadata.create_all`)
```

Regla que se aplica al pie de la letra: **un router jamás recibe una `AsyncSession`
ni llama a un repositorio.** Recibe el servicio ya montado y devuelve el esquema
Pydantic que este arma. `worker.py` entra por la misma puerta —`CommerceService`—,
así que la cola y la API ejecutan exactamente la misma orquestación en vez de dos
copias que se van separando con el tiempo.

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
| `POST /api/v1/letters/photos/eager` | Sube una foto al contenedor efímero antes de crear la carta |
| `POST /api/v1/letters` | Crea la carta de una compra pagada (202 con cola; 200 si ya existía: un borrador se sobrescribe y se publica, una publicada se devuelve tal cual) |
| `GET /api/v1/letters`, `GET /api/v1/letters/{id}` | "Mis cartas": pago, entrega y borradores |
| `GET /api/v1/me/dedications` | "Mis dedicatorias": panel posventa, una fila por compra pagada con `state` `draft`/`published`, en una sola consulta |
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

## Pagos en local (`PAYMENT_PROVIDER=fake`)

Para recorrer el flujo comercial completo sin credenciales de Mercado Pago:

```bash
PAYMENT_PROVIDER=fake  # solo con APP_ENV=local
```

Aprueba cualquier `paymentId` numérico por el importe exacto de la compra, así que
`POST /api/v1/purchases/{id}/verify` la deja en `paid` y el editor se desbloquea. El
proveedor real no cambia: se verifica en servidor, igual que en producción.

Es una puerta abierta, y por eso tiene dos candados independientes: la aplicación
**no arranca** si `PAYMENT_PROVIDER=fake` con `APP_ENV` distinto de `local`, y
`build_gateway` lo vuelve a comprobar antes de instanciarlo. Al arrancar deja un
aviso en el log: `Proveedor de pagos de LABORATORIO activo`.

## Cola de cartas (Azure Service Bus)

Escribir la carta dentro de la petición ata la latencia del comprador a los 240 IOPS
del disco de la B1ms. Con la cola configurada, `POST /api/v1/letters` valida la compra
(IOP #5), publica el mensaje y responde **202** sin escribir; `worker.py` hace el INSERT
y dispara el correo (IOP #6 y #7).

| Variable | Efecto |
| --- | --- |
| `AZURE_SERVICE_BUS_CONNECTION_STRING` | Cadena de la política de la cola. **Opcional.** |
| `SERVICE_BUS_QUEUE_NAME` | Cola de cartas. **Opcional.** |
| `SERVICE_BUS_MAX_BATCH` | Mensajes por lote; tope 12 (240 IOPS del disco). |
| `SERVICE_BUS_MAX_ATTEMPTS` | Entregas antes de la dead-letter queue. |
| `WORKER_DB_POOL_SIZE` | Pool del worker; tope 5 de las 20 conexiones. |

**Degradación elegante.** Las dos primeras variables son estrictamente opcionales: si
falta cualquiera de ellas, la API arranca igual, `build_publisher` devuelve un
`MockPublisher` inofensivo y la carta se escribe de forma síncrona, exactamente como
antes. Si la cola está configurada pero falla en caliente, el endpoint también cae al
camino síncrono en vez de perder la carta. `GET /api/v1/health/commerce` muestra el modo
activo en `letterQueue` (`service-bus` o `sync`).

Reparto de conexiones: la validación de arranque suma el pool del worker al presupuesto
**solo cuando la cola está configurada**, así que activarla no cambia el cálculo de un
despliegue que ya está en marcha.

Ejecutar el consumidor:

```bash
python worker.py     # sin cola configurada informa y termina con código 0
```

### Recorrido completo de una carta

1. **Eager upload.** Mientras el comprador elige fotos, el frontend las sube una a una a
   `POST /api/v1/letters/photos/eager`. Van al contenedor efímero y no gastan ni una
   escritura en PostgreSQL. La respuesta trae `tempId`, que es lo que hay que devolver.
2. **Envío del formulario (IOP #4 y #5).** `POST /api/v1/letters` recibe la carta con
   `temp_photos: [{tempId, fileName}]`, comprueba en la base que la compra existe, es de
   esta cuenta, está pagada y **no tiene carta publicada**, y publica el mensaje: **202**.
   Si la compra no está pagada o ya tiene carta publicada responde **409** sin encolar
   nada, así que retroceder en el navegador no consigue una segunda carta. Un borrador sí
   se encola: es el retome desde "Mis dedicatorias" y el worker lo sobrescribe.
3. **Worker (IOP #6).** Escribe la carta, traslada cada foto del contenedor efímero al
   permanente con `move_blob` y guarda el nombre original del archivo en `caption`.
4. **Correo (IOP #7).** Publica la carta y envía el correo: enlace y QR en el cuerpo, y
   adjunto un documento HTML autónomo con las fotos incrustadas en Base64 (tope
   `MAX_LETTER_DOCUMENT_BYTES`, por debajo de los 25 MB). La descarga y el Base64 se
   ejecutan fuera del bucle de eventos con `anyio.to_thread.run_sync`.

El contenedor efímero es `AZURE_TEMPORAL_CONTAINER_NAME` con `STORAGE_BACKEND=azure` y
la carpeta `<LOCAL_STORAGE_DIR>/_temporal` con `STORAGE_BACKEND=local`: un desarrollador
prueba el flujo entero sin credenciales de nube. Si no se declara el contenedor
temporal, el permanente hace de los dos bajo el prefijo `temporal/`.

El worker recibe lotes de 12 mensajes como máximo, los procesa con como mucho 5
operaciones simultáneas y no empieza el siguiente lote hasta terminar el anterior. Un
mensaje que falla nunca rompe el bucle: los errores permanentes (JSON inválido, compra
inexistente o sin pagar, comprador inactivo) van a la dead-letter queue, y los
transitorios se abandonan para su reentrega hasta `SERVICE_BUS_MAX_ATTEMPTS`.

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


### Cobertura y qué se ejecuta sin PostgreSQL

Cada funcionalidad nueva lleva cinco casos —camino feliz, camino triste, dos
fronteras y manejo de excepciones—, agrupados por archivo:

| Archivo | Cubre |
|---|---|
| `test_eager_upload.py` | subida anticipada: tipos falsificados, tamaño exacto, concurrencia, Azure caído |
| `test_photo_transfer.py` | traslado al permanente: reentrega, blob borrado, límite de fotos |
| `test_service_bus.py` | publicación: sobre JSON, un solo *sender*, tiempo de espera, degradación |
| `test_worker.py` | ciclo del mensaje: completar, reintentar, dead-letter, lote de 12 |
| `test_mailer_security.py` | XSS en el documento y en el correo, inyección de cabeceras |
| `test_storage.py` | rutas fuera de la raíz, traslado idempotente, backend de Azure |
| `test_payments_lab.py` | proveedor de laboratorio y sus dos candados |

Sin `TEST_DATABASE_URL` se ejecutan 163 pruebas y se omiten las de integración; con
la base levantada son 268. **El pipeline levanta PostgreSQL como contenedor de
servicio**, así que en el PR corren las 268.

## Coordinación con infraestructura

Pendientes externos, rotación de secretos y presupuesto de conexiones en
[la matriz de configuración](docs/MATRIZ_CONFIGURACION.md). No se ejecutaron migraciones
ni consultas contra Azure, y no se modificaron pipelines ni recursos remotos.
