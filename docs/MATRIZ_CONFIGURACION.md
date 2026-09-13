# Matriz de configuración: Azure ↔ `app/core/config.py`

Propuesta verificada localmente. No se ejecutó nada contra Azure ni se modificaron
pipelines, App Service, Key Vault ni bases remotas.

## 1. Advertencia de seguridad sobre `docs/AZURECONF.md`

`docs/AZURECONF.md` **contiene contraseñas de base de datos en texto plano** dentro de
las cadenas de conexión de DEV, STG y MAIN (los campos `MAIL_PASSWORD` y el `SK` de JWT
sí quedaron vacíos). Todo lo que aparece o apareció en ese archivo debe tratarse como
**comprometido**.

Acciones requeridas por el responsable de infraestructura, fuera de este repositorio:

1. Rotar las contraseñas de `zvbdmaster` y del usuario de la base de MAIN.
2. Rotar el token de despliegue SWA, los `WEBHOOK_URL_*` (llevan credenciales de SCM
   embebidas en la URL) y la contraseña de correo.
3. Mover los secretos a Azure Key Vault o a Variable Groups enlazados a Key Vault, y
   dejar en el documento solo el **nombre** de cada secreto, nunca su valor.
4. Confirmar que el archivo no llegó a un remoto: hoy está en `.gitignore` y no
   figura en el índice de git de este repositorio.

En este repositorio **no** hay valores de Azure: ni en código, ni en `.env`, ni en
Postman, ni en Dockerfile, ni en pruebas. `.env` está ignorado por git.

Observación adicional que debe revisar infraestructura: en ese documento, la cadena de
MAIN apunta al servidor y al usuario administrador **del proyecto de referencia**, no al
servidor compartido de Zyvencore que usan DEV y STG. O es deliberado y hay que
documentarlo, o es un error de copiado que conviene corregir antes de desplegar.

## 2. JWT no se activa

`JWT_SECRET_KEY`, `ALGORITHM` y `ACCESS_TOKEN_EXPIRE_MINUTES` existen en la
configuración de Azure porque provienen del proyecto de referencia. **Esta API no las
lee**: `Settings` usa `extra="ignore"` y no define ningún campo equivalente
(`app/core/config.py`, constante `LEGACY_UNUSED_VARIABLES`).

La autenticación sigue siendo por **sesión opaca**: token aleatorio, hash guardado en
`auth_sessions`, cookie `HttpOnly` (`__Host-` fuera de local), rotación al iniciar
sesión, revocación inmediata al cerrar sesión o al borrar la fila, y protección CSRF
por doble envío firmado. La API nunca devuelve un token portador; hay una prueba
específica de ello (`tests/test_config_matrix.py::test_api_never_returns_a_bearer_token`).

Pueden dejarse esas tres variables en Azure sin efecto alguno, o eliminarse. Lo que no
debe hacerse es interpretarlas como que existe un flujo JWT.

## 3. Incompatibilidad de TLS en la cadena de conexión, resuelta

Las cadenas guardadas en Azure llevan el modo TLS en la query: `?sslmode=require` en
DEV/STG y `?ssl=require` en MAIN. La configuración anterior rechazaba cualquier query y
exigía `DB_SSL_MODE`/`DB_SSL_CA_FILE` por separado, de modo que la aplicación no
arrancaba con la cadena tal como está guardada.

Se eligió **adaptar el parser** (`app/core/dsn.py`) en vez de exigir que se editen las
cadenas en Azure, porque tocar los Variable Groups es competencia de infraestructura:

| Valor en la URL | Resultado |
|---|---|
| `sslmode=require`, `sslmode=verify-ca`, `sslmode=verify-full`, `ssl=require`, `ssl=true`, `ssl=on`, `ssl=1` | Se traduce a `DB_SSL_MODE=verify-full` (cadena **y** hostname verificados) |
| `sslmode=disable`, `allow`, `prefer`, `ssl=false`, `off`, `0` | Aceptado **solo** en `APP_ENV=local`; en develop/staging/production aborta el arranque |
| Cualquier otro parámetro (`options`, `sslrootcert`, …) o valor desconocido | Aborta el arranque con un error que no incluye la cadena ni la contraseña |

Notas importantes:

- `sslmode=require` en PostgreSQL cifra **sin** verificar la cadena ni el hostname.
  Aquí se eleva deliberadamente a verificación completa. Nunca se degrada TLS y nunca
  se desactiva la verificación de certificados para "resolver" un error de CA: si el
  certificado no valida, se configura `DB_SSL_CA_FILE` con una CA de confianza.
- La URL que finalmente recibe asyncpg queda **sin query**; el TLS se aplica desde
  `DB_SSL_MODE` en `app/db/database.py`.
- Alternativa igualmente válida, si infraestructura prefiere: normalizar las cadenas en
  Azure quitando la query y poniendo `DB_SSL_MODE=verify-full`. Ambas rutas dan el
  mismo resultado; el parser existe para que las dos funcionen.

## 4. Matriz de variables

`Local` es lo que trae `.env.example`. `Remoto` aplica a develop, staging y production;
la rama `main` corresponde a `APP_ENV=production`, y no se infiere desde git.

| Variable | Local | Remoto (develop/staging/production) | Notas |
|---|---|---|---|
| `APP_ENV` | `local` | `develop` \| `staging` \| `production` | Decide cookies seguras, TLS obligatorio y almacenamiento durable |
| `DATABASE_URL` | Base local propia | Secreto por entorno (Key Vault) | `postgresql+asyncpg://`; admite `?sslmode=`/`?ssl=` y los traduce |
| `DB_SSL_MODE` | `disable` | `verify-full` (obligatorio) | Se combina con lo que pida la URL, quedándose con lo más estricto |
| `DB_SSL_CA_FILE` | vacío | Opcional | Sin valor usa las CA del sistema |
| `SESSION_SECRET` | Generado local | Secreto ≥32 caracteres, **igual en todas las réplicas** | Rotarlo invalida CSRF en vuelo y sesiones |
| `CORS_ORIGINS` | `["http://localhost:5173"]` | Orígenes HTTPS exactos | Sin comodines ni rutas |
| `FRONTEND_URL` | `http://localhost:5173` | HTTPS del SWA | Base del visor público y del QR. **Pendiente**: en Azure figura como `PENDING` |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | 2 / 0 | 2 / 0 | Por proceso |
| `DB_POOL_TIMEOUT` | 10 | 10 | Segundos |
| `WEB_CONCURRENCY` / `APP_REPLICAS` | 1 / 1 | Valor **real** del plan | Debe reflejar el escalado configurado |
| `DB_CONNECTION_BUDGET` / `DB_RESERVED_CONNECTIONS` | 20 / 2 | 20 / 2 | Presupuesto declarado en `Iops.md` |
| `PORT` | 8000 | El que exponga App Service | El Dockerfile lo respeta |
| `SESSION_MINUTES` / `CSRF_SECONDS` | 30 / 3600 | igual | |
| `COOKIE_SAMESITE` | `lax` | `none` si el frontend (SWA) y el backend (App Service) están en dominios distintos; `lax` si comparten dominio | `none` exige cookies seguras; Safari bloquea cookies de terceros, así que la solución definitiva es un dominio compartido |
| `AUTH_RATE_LIMIT` / `AUTH_RATE_WINDOW` | 30 / 60 | igual | Límite **por proceso**, no distribuido |
| `GOOGLE_CLIENT_ID` | opcional | Cliente web de Google | Vacío ⇒ `/auth/google` responde 503 `GOOGLE_NOT_CONFIGURED` |
| `PII_HMAC_KEY` | Generada local | Secreto propio | HMAC de la cédula; si falta deriva de `SESSION_SECRET`. Rotarla invalida los documentos ya guardados |
| `PII_ENCRYPTION_KEY` | Generada local | Secreto propio | Clave AES-256 del número de documento (32+ caracteres). Si falta deriva de `SESSION_SECRET`. **Fíjala explícitamente en staging y production:** rotar `SESSION_SECRET` sin haberla fijado antes deja **ilegibles** los números ya cifrados, y sin ellos no se puede facturar ante la DIAN |
| `PAYMENT_PROVIDER` | `none` | `mercadopago` | `none` ⇒ verificación y webhook responden 503 explícito |
| `MERCADOPAGO_ACCESS_TOKEN` | vacío | Secreto | Obligatorio si el proveedor es `mercadopago` |
| `MERCADOPAGO_WEBHOOK_SECRET` | vacío | Secreto | Obligatorio: sin firma válida no se procesa ningún webhook |
| `PURCHASE_AMOUNT_CENTS` / `PURCHASE_CURRENCY` | 0 / `COP` | Precio real | **Pendiente de decisión de negocio** |
| `PURCHASE_PENDING_MINUTES` | 60 | 60 | Vigencia de la intención de pago |
| `STORAGE_BACKEND` | `local` | `azure` (obligatorio) | El disco del App Service no es durable |
| `LOCAL_STORAGE_DIR` | `.local-storage` | — | Solo local |
| `AZURE_STORAGE_CONNECTION_STRING` | vacío | Secreto | Requiere el extra `pip install '.[azure]'` |
| `AZURE_CONTAINER_NAME` | vacío | Contenedor de fotos publicadas | En Azure figura vacío: **pendiente** |
| `AZURE_TEMPORAL_CONTAINER_NAME` | vacío | Contenedor temporal | Reservado; aún no se usa |
| `MAX_PHOTO_BYTES` / `MAX_PHOTOS_PER_LETTER` | 3 MB / 6 | igual | |
| `MAIL_BACKEND` | `console` | `smtp` | `console` no envía nada |
| `MAIL_HOST` / `MAIL_PORT` | `smtp.gmail.com` / 587 | igual | STARTTLS con verificación |
| `MAIL_USERNAME` / `MAIL_PASSWORD` | vacíos | Secretos | Gmail exige contraseña de aplicación |
| `MAIL_FROM` | vacío | Remitente visible | Si falta usa `MAIL_USERNAME` |
| `LETTER_CARD_ENABLED` | `true` | `true` | Tarjeta QR en PDF adjunta al correo y rutas `card.pdf` y `postal.png`; `false` las apaga en caliente si el dibujo pesa en la B1ms. El código del correo (`qr.png`) no depende de esta variable |
| `MAX_LETTER_CARD_BYTES` | 1 MB | 1 MB | Red de seguridad; una tarjeta ronda los 50 kB |
| `API_PUBLIC_URL` | vacío | `https://api-….azurewebsites.net` | Origen público de la API. Con él, el QR del correo es una imagen remota al endpoint público (la vía que todos los clientes muestran); sin él viaja incrustado por Content-ID |
| `FREEZE_LETTER_AFTER_PUBLISH` | `true` | `true` | Decisión documentada: el contenido se congela al publicar |
| `JWT_SECRET_KEY`, `ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES` | — | — | **Heredadas y no usadas**; no activan JWT |

## 5. Presupuesto de conexiones

La validación de arranque exige:

```
WEB_CONCURRENCY × APP_REPLICAS × (DB_POOL_SIZE + DB_MAX_OVERFLOW)
    + DB_RESERVED_CONNECTIONS  ≤  DB_CONNECTION_BUDGET
```

Con los valores por defecto: `1 × 1 × (2 + 0) + 2 = 4 ≤ 20`. Con dos workers en cuatro
réplicas: `2 × 4 × 2 + 2 = 18 ≤ 20`, que ya no deja margen para un despliegue
superpuesto ni para una migración simultánea.

La reserva existe para migraciones, administración y despliegues que se solapan. La
fórmula no descubre otros procesos por su cuenta: si alguien abre un cliente de SQL o
corre `alembic` mientras la aplicación está al límite, el presupuesto se excede.

**El presupuesto es de conexiones, no de usuarios.** Las 20 conexiones no equivalen a
20 ni a 8190 usuarios concurrentes: una conexión atiende consultas en serie y la
capacidad real depende de la duración de cada consulta, de los IOPS del disco y del
único vCPU de la instancia B1s. No se hizo ninguna prueba de carga; nada aquí demuestra
la cifra de 8190 usuarios de `Iops.md`, que además es un límite del balanceador.

## 6. Pendientes de infraestructura

1. Rotar y mover a Key Vault todos los secretos del punto 1.
2. Definir `FRONTEND_URL` real por entorno (hoy `PENDING` en Azure). Sin él, el enlace
   del correo y del QR caen al primer origen de `CORS_ORIGINS`.
3. Crear la cuenta de Storage y los contenedores, y publicar
   `AZURE_STORAGE_CONNECTION_STRING` / `AZURE_CONTAINER_NAME`.
4. Crear la aplicación de Mercado Pago, publicar el token y el secreto de webhook, y
   registrar la URL del webhook (`POST /api/v1/webhooks/mercadopago`).
5. Definir el precio (`PURCHASE_AMOUNT_CENTS`) y la moneda.
6. Configurar `GOOGLE_CLIENT_ID` y los orígenes autorizados en Google.
7. Confirmar el número real de workers y réplicas para ajustar el presupuesto.
8. Ejecutar `alembic upgrade head` una sola vez por entorno, mediante el proceso
   autorizado, tras revisar el estado de la base con su responsable. **Este trabajo no
   ejecutó migraciones contra Azure.**

## 7. Nota sobre el pipeline de CD

El paso de migración del CD ejecuta, dentro de un contenedor efímero:

```bash
FIXED_URL="${MAPPED_DB_URL//sslmode=/ssl=}"
docker run --rm -e DATABASE_URL="$FIXED_URL" imagen:tag alembic upgrade head
```

Funciona con este backend: Alembic usa `MigrationSettings`, que solo exige
`DATABASE_URL` y aplica exactamente las mismas reglas de TLS que la aplicación completa.
La reescritura de `sslmode=` a `ssl=` es innecesaria pero inofensiva: el parser acepta
ambas formas y las eleva a `verify-full`.

Recomendación menor: añadir `-e APP_ENV=develop` (o `staging`/`production`) al
`docker run`. Sin esa variable, `APP_ENV` cae a `local` y la migración aceptaría una URL
sin TLS si alguien la configurara así. Con las cadenas actuales, que llevan `require`, el
TLS se verifica de todos modos.


## 8. Variables del App Service: qué falta respecto al proyecto de referencia

**Incidente del 2026-09-06.** El despliegue a DEV construyó y migró bien, pero el
contenedor no arrancó:

```
pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
session_secret
  Field required [type=missing]
```

Causa: el App Service tiene la lista de variables **del proyecto de referencia**, que
autenticaba con JWT. Esa lista incluye `JWT_SECRET_KEY`, `ALGORITHM` y
`ACCESS_TOKEN_EXPIRE_MINUTES`, pero **no incluye `SESSION_SECRET`**, que es lo que esta
API necesita para firmar y validar sesiones. No es un fallo del código ni del pipeline:
falta configuración en el App Service.

Desde este corte, el arranque falla con un diagnóstico legible que nombra la variable y
explica cómo generarla, en vez de una traza de pydantic.

### Variables a añadir en cada App Service

| Variable | DEV | STG | MAIN |
|---|---|---|---|
| `APP_ENV` | `develop` | `staging` | `production` |
| `SESSION_SECRET` | secreto propio ≥32 caracteres | ídem, distinto | ídem, distinto |
| `CORS_ORIGINS` | `["https://<front-dev>"]` | `["https://<front-stg>"]` | `["https://<front-main>"]` |
| `FRONTEND_URL` | `https://<front-dev>` | `https://<front-stg>` | `https://<front-main>` |
| `STORAGE_BACKEND` | `azure` | `azure` | `azure` |
| `AZURE_STORAGE_CONNECTION_STRING` | secreto | secreto | secreto |
| `AZURE_CONTAINER_NAME` | contenedor de fotos | ídem | ídem |
| `WEB_CONCURRENCY` / `APP_REPLICAS` | valores reales del plan | ídem | ídem |

Opcionales según se activen: `MAIL_BACKEND=smtp` con `MAIL_USERNAME`/`MAIL_PASSWORD`,
`PAYMENT_PROVIDER=mercadopago` con `MERCADOPAGO_ACCESS_TOKEN` y
`MERCADOPAGO_WEBHOOK_SECRET`, `PURCHASE_AMOUNT_CENTS`, `GOOGLE_CLIENT_ID` y
`PII_HMAC_KEY` y `PII_ENCRYPTION_KEY`. Cada una exige sus propios secretos: si se activa a medias, el arranque
falla diciendo cuál falta.

Genera cada `SESSION_SECRET` sin imprimirlo en un chat ni en un ticket:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Debe ser **idéntico en todas las réplicas del mismo entorno** y **distinto entre
entornos**. Rotarlo invalida las sesiones y los tokens CSRF en vuelo.

### Variables que sobran

`JWT_SECRET_KEY`, `ALGORITHM` y `ACCESS_TOKEN_EXPIRE_MINUTES` pueden eliminarse: esta
API no las lee y no sustituyen a `SESSION_SECRET`. Dejarlas no rompe nada, pero induce a
pensar que existe un flujo JWT que no existe.

### Por qué `APP_ENV` es obligatorio ahora

Si `APP_ENV` no está definido, la configuración caería a `local`, y eso en un despliegue
real significa cookies **sin** `Secure` ni prefijo `__Host-`, sin exigir TLS verificado y
sin exigir almacenamiento durable. Como Azure App Service siempre define
`WEBSITE_SITE_NAME`, la aplicación detecta que está en un despliegue real y **se niega a
arrancar** si `APP_ENV` no se declaró de forma explícita. Es preferible un arranque
fallido y ruidoso a un servicio en producción con cookies inseguras.
