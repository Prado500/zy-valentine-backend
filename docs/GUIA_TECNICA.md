# Guía técnica del backend — cómo funciona el código

Documento para el equipo. Explica la arquitectura, qué hace cada archivo, cómo fluye una
petición y por qué se tomó cada decisión. Si vas a tocar el código, lee al menos las
secciones 1 a 4; si vas a desplegar, lee además la 12 y
[`MATRIZ_CONFIGURACION.md`](MATRIZ_CONFIGURACION.md).

Convenciones: las rutas son relativas a la raíz del repositorio. "Remoto" significa
`APP_ENV` distinto de `local`, es decir develop, staging o production.

---

## 1. Panorama general

La API es **FastAPI + SQLAlchemy 2.0 asíncrono + PostgreSQL + Alembic**. No hay ORM
lazy loading en las respuestas, no hay tareas en segundo plano y no hay estado en
memoria compartido entre réplicas (salvo el limitador de intentos, que es por proceso y
está documentado como tal).

### 1.1 Capas

```
app/api/routers/     HTTP: rutas, códigos de estado, cabeceras, cookies
app/api/dependencies dependencias FastAPI: sesión de DB, CSRF, usuario actual, rate limit
app/schemas/         contratos Pydantic de entrada y salida (nunca modelos de DB)
app/services/        reglas de negocio y puertos hacia servicios externos
app/repositories/    consultas SQL; sin reglas de negocio ni HTTP
app/models/          entidades SQLAlchemy y restricciones de base de datos
app/db/              motor, TLS y fábrica de sesiones
app/core/            configuración, seguridad, errores, parser de DSN
alembic/             migraciones
tests/               pruebas
scripts/             validación local reproducible
```

La regla es de una sola dirección: **router → service → repository → model**. Un router
nunca escribe SQL; un service nunca devuelve una `Response`; un repository nunca lanza
un `ApiError` de negocio. Esto no es decoración: permite probar las reglas de negocio
sin levantar HTTP y cambiar el proveedor de pagos sin tocar los endpoints.

### 1.2 Lo que hay que entender antes que nada

Tres ideas explican casi todo el diseño:

1. **La sesión es opaca y revocable, no un JWT.** El servidor puede matar una sesión al
   instante porque el estado vive en la base de datos.
2. **Las reglas críticas viven en la base de datos, no en Python.** "Una compra pagada
   habilita una carta" es un `UNIQUE`, no un `if`. Un `if` se salta con dos peticiones
   simultáneas; un `UNIQUE` no.
3. **Todo servicio externo está detrás de un puerto inyectable.** Mercado Pago, Azure
   Storage, el correo y Google se sustituyen por implementaciones deterministas en las
   pruebas, así que la suite corre sin una sola credencial real.

---

## 2. Configuración: `app/core/config.py` y `app/core/dsn.py`

### 2.1 Cómo se cargan los valores

`Settings` hereda de `BaseSettings` (pydantic-settings). Lee, en este orden de
prioridad: argumentos explícitos → variables de entorno → archivo `.env`. Usa
`extra="ignore"`, lo que significa que **una variable de entorno que no esté declarada
como campo simplemente se ignora**. De ahí sale el tratamiento de las variables
heredadas de JWT: no hay campo `jwt_secret_key`, así que la variable existe en Azure y
no produce ningún efecto. La constante `LEGACY_UNUSED_VARIABLES` está ahí para
documentarlo y para que la prueba lo verifique.

`get_settings()` está decorado con `@lru_cache`: se construye una sola vez por proceso.
En las pruebas no se usa esa caché, se construye una `Settings` explícita por test.

### 2.2 El validador de arranque

`validate_runtime` es un `@model_validator(mode="after")`: se ejecuta cuando todos los
campos ya tienen valor, y **si algo está mal el proceso no arranca**. Es deliberado:
preferimos que el despliegue falle ruidosamente a que arranque con TLS desactivado o
con el pool por encima del presupuesto. Comprueba, en orden:

1. `SESSION_SECRET` de al menos 32 caracteres.
2. La URL de base de datos (ver 2.3).
3. Que `CORS_ORIGINS` no esté vacío y que cada origen sea un origen exacto: esquema
   `http`/`https`, con host, **sin ruta, sin query, sin fragmento y sin usuario**. Esto
   descarta `*`, `https://x.com/api` y trampas parecidas.
4. Si el entorno es remoto: TLS verificado, todos los orígenes en HTTPS y `FRONTEND_URL`
   en HTTPS. Si es local: `SameSite=None` prohibido, porque sin `Secure` el navegador la
   rechazaría igualmente.
5. El presupuesto de conexiones (ver 2.4).
6. Que cada integración activada traiga sus propios secretos: `PAYMENT_PROVIDER=mercadopago`
   exige token **y** secreto de webhook; `STORAGE_BACKEND=azure` exige cadena de
   conexión y contenedor; `MAIL_BACKEND=smtp` exige usuario y contraseña.
7. Que un entorno remoto **no** use almacenamiento local: el disco de App Service no es
   durable y las fotos desaparecerían al reiniciar.

### 2.3 El parser de DSN: `app/core/dsn.py`

Este módulo existe por un problema concreto. Las cadenas guardadas en Azure son del
estilo `postgresql+asyncpg://usuario:clave@servidor:5432/base?sslmode=require`, pero
asyncpg no acepta `sslmode` en la URL y nuestra configuración expresa el TLS en
`DB_SSL_MODE`. Antes, la aplicación rechazaba cualquier query y no arrancaba con la
cadena tal como está guardada.

`split_ssl_query(raw_url, app_env)` devuelve una tupla `(url_sin_query, modo_pedido)`:

```python
clean, requested = split_ssl_query(url, settings.app_env)
```

- Solo acepta las claves `sslmode` y `ssl`. Cualquier otra (`options`, `sslrootcert`, …)
  lanza `DsnError`. **No se ignoran en silencio**: ignorar un `options=-csearch_path=...`
  sería aceptar que alguien cambie el esquema por configuración.
- Los valores que piden cifrado (`require`, `verify-ca`, `verify-full`, `true`, `on`,
  `1`) se traducen a `verify-full`. Ojo con esto: en PostgreSQL, `sslmode=require`
  cifra **pero no verifica** la cadena de certificación ni el hostname, así que es
  vulnerable a un intermediario. Aquí lo elevamos a verificación completa a propósito.
- Los valores que desactivan o hacen opcional TLS (`disable`, `allow`, `prefer`,
  `false`, `off`, `0`) solo se aceptan en `APP_ENV=local`. En remoto lanzan `DsnError` y
  el proceso no arranca.
- Los mensajes de error **nunca incluyen la URL ni la contraseña**, solo el nombre del
  parámetro problemático. Hay una prueba que lo verifica.

Luego `strongest(*modos)` combina lo que pidió la URL con lo que dice `DB_SSL_MODE` y se
queda con lo más estricto. Nunca al revés: no existe forma de bajar el nivel de TLS
desde la URL.

Finalmente el validador reescribe `self.database_url` con la URL ya limpia, de modo que
lo que llega a asyncpg no tiene query, y el TLS se aplica desde `app/db/database.py`.

> Si infraestructura prefiere, la alternativa sigue siendo válida: quitar la query en
> Azure y poner `DB_SSL_MODE=verify-full`. El parser existe para que las dos rutas
> funcionen sin editar los Variable Groups a mano.

### 2.4 El presupuesto de conexiones

```
WEB_CONCURRENCY × APP_REPLICAS × (DB_POOL_SIZE + DB_MAX_OVERFLOW)
    + DB_RESERVED_CONNECTIONS  ≤  DB_CONNECTION_BUDGET
```

La instancia es una B1s con un límite práctico de 20 conexiones (`Iops.md`). La reserva
existe para migraciones, para un despliegue que se solapa con el anterior y para que
alguien pueda abrir un cliente de SQL sin tumbar la API.

**Advertencia que hay que repetir cada vez que salga el tema:** esto son conexiones, no
usuarios. Una conexión atiende consultas en serie. Veinte conexiones no son veinte
usuarios ni ocho mil; la capacidad real depende de cuánto dura cada consulta, de los
IOPS del disco y del único vCPU. No se hizo prueba de carga y nada aquí demuestra la
cifra de 8190 usuarios, que además es un límite del balanceador, no de la base.

### 2.5 Propiedades derivadas

- `secure_cookies` → `True` fuera de local. De ahí salen `Secure` en las cookies y el
  prefijo `__Host-`, que el navegador solo acepta con `Secure`, `Path=/` y sin `Domain`.
- `public_base_url` → base del visor público; usa `FRONTEND_URL` y, si falta, el primer
  origen de CORS. De aquí salen el enlace del correo y el contenido del QR.
- `pii_key` → clave del HMAC de la cédula; usa `PII_HMAC_KEY` y si no está, deriva de
  `SESSION_SECRET`.

---

## 3. Base de datos: `app/db/database.py`

```python
tls = ssl.create_default_context(cafile=settings.db_ssl_ca_file) if verify-full else False
create_async_engine(url, pool_size=…, max_overflow=…, pool_timeout=…,
                    pool_pre_ping=True, hide_parameters=True,
                    connect_args={"ssl": tls, "timeout": 10, "command_timeout": 10})
```

Puntos que importan:

- `ssl.create_default_context()` verifica **cadena y hostname**. No hay ninguna ruta de
  código que ponga `check_hostname=False` o `CERT_NONE`. Si un certificado no valida, la
  solución es `DB_SSL_CA_FILE` con la CA correcta, nunca desactivar la verificación.
- `hide_parameters=True` y `echo=False`: las excepciones de SQLAlchemy no llevan los
  valores de los parámetros, así que un error no filtra un hash ni un correo al log.
- `timeout` y `command_timeout`: ni la conexión ni una consulta pueden colgarse
  indefinidamente ocupando una de las pocas conexiones del presupuesto.
- `pool_pre_ping=True`: Azure corta conexiones ociosas; sin esto la primera consulta tras
  el corte fallaría.

`session_factory` crea un `async_sessionmaker` con `expire_on_commit=False`. Esto último
es importante en async: si los objetos se expiraran al hacer commit, leer un atributo
después dispararía una recarga perezosa síncrona y reventaría con `MissingGreenlet`.

**Una `AsyncSession` por petición, nunca compartida entre tareas concurrentes.** La
dependencia `get_db` lo garantiza.

---

## 4. Ciclo de vida y errores: `app/main.py`

`create_app(settings)` construye la aplicación. Se recibe `settings` como parámetro
(con `get_settings()` por defecto) precisamente para que las pruebas inyecten otra.

### 4.1 `lifespan`

Al arrancar crea y guarda en `app.state`: el motor, la fábrica de sesiones, dos
limitadores de capacidad y los cuatro servicios externos.

```python
app.state.hash_limiter   = CapacityLimiter(2)   # Argon2 simultáneos
app.state.google_limiter = CapacityLimiter(1)
app.state.payments = build_gateway(settings)
app.state.storage  = build_storage(settings)
app.state.mailer   = build_mailer(settings)
```

Los `CapacityLimiter` son de anyio y limitan cuántas tareas entran a la vez al pool de
hilos. Argon2 está calibrado para ser caro en CPU; con un solo vCPU, permitir diez
hashes simultáneos convertiría el login en una forma barata de tumbar el servicio. Dos
es un techo deliberado.

Al apagar cierra el verificador de Google, el cliente HTTP de pagos y el motor.

### 4.2 Middleware y cabeceras

Un middleware asigna un `request_id` (UUID) por petición, lo devuelve en `X-Request-ID`
y añade `Cache-Control: no-store` y `X-Content-Type-Options: nosniff` a **todas** las
respuestas. El mismo `requestId` aparece en el cuerpo del error, así que un usuario
puede reportar un identificador y se localiza la petición sin exponer nada más.

### 4.3 Manejadores de error

Cuatro manejadores producen siempre la misma forma de error:

```json
{ "code": "…", "message": "…", "fieldErrors": [], "requestId": "…" }
```

- `HTTPException` → usa el `code`/`message` que puso `ApiError`.
- `RequestValidationError` → 422. **Solo se devuelven el nombre del campo y el tipo de
  error, nunca el valor recibido.** Pydantic por defecto incluye el input en el error;
  eso significaría devolver la contraseña en la respuesta de un 422. Hay una prueba que
  verifica que la contraseña no aparece nunca en el cuerpo.
- `SQLAlchemyError` → 503 genérico, sin detalles de SQL.
- `Exception` → 500 genérico.

### 4.4 Salud

- `GET /` y `GET /health/live` no tocan la base: responden si el proceso vive. Si la
  liveness dependiera de la base, una caída de la base reiniciaría los contenedores en
  bucle.
- `GET /health/ready` consulta `alembic_version` y exige que sea `EXPECTED_REVISION`
  (hoy `0002_commerce`). Si la migración no está aplicada responde 503 y el balanceador
  no le manda tráfico. **Cuando añadas una migración, actualiza esa constante.**

### 4.5 CORS

`allow_origins` son los orígenes exactos configurados, `allow_credentials=True` (hace
falta para que el navegador mande la cookie) y las cabeceras permitidas se limitan a
`Content-Type` y `X-CSRF-Token`.

---

## 5. Seguridad y sesiones

### 5.1 `app/core/security.py`

- `password_hasher = PasswordHash.recommended()` → Argon2. `DUMMY_HASH` es un hash
  precalculado que se verifica cuando el correo **no existe**, para que el tiempo de
  respuesta sea el mismo exista o no la cuenta. Sin eso, medir el tiempo revela qué
  correos están registrados.
- `digest(valor)` → SHA-256 en hexadecimal. Se usa para el hash del token de sesión.
- `issue_csrf` / `verify_csrf` → token firmado y con marca de tiempo
  (`URLSafeTimedSerializer` de itsdangerous). El token contiene aleatoriedad y va
  firmado con `SESSION_SECRET`, así que no se puede fabricar ni reutilizar pasado
  `CSRF_SECONDS`.
- `RateLimiter` → ventana fija por clave (IP), con un `OrderedDict` acotado a 10 000
  entradas para que no crezca sin límite. **Es por proceso.** No protege entre réplicas;
  eso requiere coordinación con infraestructura y está anotado como pendiente.

### 5.2 Cómo funciona la sesión, paso a paso

1. El cliente pide `GET /api/v1/auth/csrf`. El servidor emite un token firmado, lo pone
   en una cookie **y** lo devuelve en el cuerpo.
2. En cada escritura el cliente manda la cookie (automático) y la cabecera
   `X-CSRF-Token` (manual, con el valor del cuerpo). El servidor exige que coincidan:
   es el patrón *double submit*. Un sitio atacante puede provocar que el navegador
   mande la cookie, pero **no puede leerla** para ponerla en la cabecera.
3. Al iniciar sesión, `auth.new_session` genera `secrets.token_urlsafe(32)`, guarda
   **solo su SHA-256** en `auth_sessions` y devuelve el token en claro en una cookie
   `HttpOnly`. Si alguien roba un volcado de la base, no obtiene sesiones utilizables.
4. Antes de crear la nueva sesión, borra la anterior (rotación): evita fijación de
   sesión.
5. `current_user` busca la sesión por el hash del token, exige que no esté vencida y que
   el usuario esté activo, todo en **una sola consulta con `JOIN`**.
6. `logout` borra la fila y la cookie. La revocación es inmediata porque el estado está
   en la base; con un JWT habría que esperar a que expirara o mantener una lista negra,
   que es exactamente el estado que el JWT pretendía evitar.

### 5.3 `app/api/dependencies.py`

- `get_db` → abre una `AsyncSession` por petición con `async with`; al terminar (incluso
  con excepción) se cierra y devuelve la conexión al pool.
- `csrf_guard` → si viene cabecera `Origin` y no está en la lista permitida, 403 antes
  de nada. Después compara cookie y cabecera con `secrets.compare_digest` (comparación
  en tiempo constante) y verifica la firma y la vigencia. Devuelve el token, que el
  endpoint de Google reutiliza como *nonce*.
- `auth_limit` → aplica el limitador con `request.client.host`. Detrás de un proxy, sin
  cabeceras de confianza configuradas, ese valor puede ser el del proxy y agrupar a
  varios usuarios; el Dockerfile arranca con `--no-proxy-headers` justamente para no
  confiar en un `X-Forwarded-For` que cualquiera puede falsificar.
- `current_user` → 401 genérico ante cualquier fallo, sin distinguir "no existe" de
  "vencida" de "revocada".

### 5.4 Login con Google: `app/services/google.py`

Se recibe el `credential` (un ID token de Google Identity Services) y se verifica **en
el servidor** con `google.oauth2.id_token.verify_oauth2_token`, que valida firma,
`aud`, `iss` y `exp` contra las claves públicas de Google (cacheadas con CacheControl,
con timeout de 5 s). Después se comprueban a mano tres cosas más:

- `email_verified is True` — si no, cualquiera podría registrar un correo ajeno.
- `nonce` igual al token CSRF emitido — ata el ID token a esta sesión del navegador y
  evita que alguien reutilice un token obtenido en otro sitio.
- `sub` presente y acotado en longitud.

La identidad se ata al `sub`, nunca al correo, porque el correo puede cambiar de dueño.
Si llega un `sub` nuevo cuyo correo ya pertenece a otra cuenta, se responde 409
`ACCOUNT_LINK_REQUIRED`: **no se fusionan cuentas automáticamente**. La vinculación
explícita es un flujo aparte que aún no está implementado.

---

## 6. Modelos y restricciones: `app/models/commerce.py`

Aquí está el corazón del diseño. Las reglas de negocio críticas son restricciones de
base de datos, no validaciones en Python.

| Restricción | Regla que sostiene |
|---|---|
| `letters.purchase_id` **UNIQUE** | Una compra pagada habilita exactamente una carta |
| `uq_purchases_user_idempotency (user_id, idempotency_key)` | Un reintento no crea otra compra |
| `uq_payments_provider_payment (provider, provider_payment_id)` | Un pago del proveedor se registra una vez |
| `uq_payment_events_provider_event (provider, event_id)` | Un webhook se procesa una sola vez |
| `uq_letter_photos_position (letter_id, position)` | Orden de fotos estable y sin duplicados |
| `letters.purchase_id` FK `ON DELETE RESTRICT` | No se puede borrar una compra que ya generó una carta |
| `CheckConstraint` de estados | Ningún estado inventado entra en la tabla |

### 6.1 Estados separados

Cada entidad tiene su propio estado, y eso es intencional:

```
users              is_active / email_verified
purchases          pending → paid → cancelled ; expired
payments           pending | in_process → approved | rejected | cancelled → refunded | charged_back
letters            draft → published
letter_deliveries  pending → sent | failed
```

`purchases.status` es el estado **comercial** (¿esta compra habilita una carta?);
`payments.status` es lo que **dice el proveedor**. Mezclarlos daría problemas reales: un
reembolso posterior no debe borrar una carta ya publicada, y un pago rechazado no debe
borrar la compra que el usuario puede reintentar.

### 6.2 `PAYMENT_STATUS_RANK`

```python
{"pending": 1, "in_process": 1, "rejected": 2, "cancelled": 2,
 "approved": 3, "refunded": 4, "charged_back": 4}
```

Los webhooks llegan desordenados: es normal recibir "pending" **después** de "approved".
El rango convierte el estado en algo monótono: solo se aplica un estado cuyo rango sea
mayor o igual al actual. Así una notificación vieja se registra en la bitácora pero no
"despaga" una compra.

### 6.3 `user_identity_documents`

La cédula vive en su propia tabla, con `user_id` como clave primaria (uno a uno). Se
guardan **solo** el HMAC-SHA256 del número (con clave dedicada) y los últimos cuatro
dígitos. El número en claro no se persiste nunca. El `UNIQUE` sobre el hash impide que
dos cuentas registren la misma cédula sin necesidad de almacenar el dato.

Por qué HMAC y no un hash simple: un número de cédula tiene poca entropía y un SHA-256
sin clave se rompe por fuerza bruta en minutos. El HMAC con una clave secreta lo impide
mientras la clave no se filtre.

---

## 7. Servicios de dominio

### 7.1 `app/services/identity.py`

`document_fingerprint(settings, tipo, numero)` normaliza (mayúsculas, sin espacios) y
calcula el HMAC. `set_document` busca si el hash pertenece a otro usuario (409
`DOCUMENT_IN_USE`), y si no, crea o actualiza la fila. La cédula **no autentica nada**:
hay una prueba que intenta iniciar sesión usando el número como contraseña y espera 401.

### 7.2 `app/services/purchases.py`

**`create_intent`** — la parte importante:

```python
statement = (
    pg_insert(Purchase).values(...)
    .on_conflict_do_nothing(constraint="uq_purchases_user_idempotency")
    .returning(Purchase.id)
)
created_id = await db.scalar(statement)
await db.commit()
if created_id is None:            # otra petición ganó la carrera
    return await purchase_by_idempotency(...), False
return await db.get(Purchase, created_id), True
```

Se usa `INSERT … ON CONFLICT DO NOTHING` en lugar de capturar `IntegrityError`. La razón
es concreta y la encontramos probando: cuando el pool está al límite (por defecto son
**dos** conexiones por proceso) y dos peticiones simultáneas chocan contra la clave
única, el `rollback()` de la perdedora necesita reservar una conexión, no la consigue y
SQLAlchemy lanza `MissingGreenlet`, que acaba en un 503. `ON CONFLICT DO NOTHING` no
genera error, así que no hay transacción que deshacer y la petición perdedora
simplemente lee la compra existente y responde 200.

PostgreSQL además **bloquea** la segunda inserción hasta que la primera confirma o
deshace, así que el resultado es determinista: siempre hay exactamente una compra.

**`apply_snapshot`** — el único punto donde cambia el estado de un pago, lo llamen la
verificación manual o el webhook:

1. `lock_purchase` hace `SELECT … FOR UPDATE` sobre la compra. Cualquier otra
   transacción que quiera tocarla espera aquí.
2. Comprueba que el `external_reference` del pago sea el de esta compra (409
   `PAYMENT_MISMATCH`). Sin esto, alguien podría usar el identificador de un pago ajeno.
3. Si el pago viene aprobado, comprueba monto y moneda (409 `PAYMENT_AMOUNT_MISMATCH`).
   Es la defensa contra pagar 1 peso y reclamar la carta.
4. Crea o actualiza la fila de `payments` respetando el rango monótono.
5. Solo si el pago quedó `approved` marca la compra como `paid` y sella `paid_at`. Con
   `refunded`/`charged_back` la pasa a `cancelled`.

### 7.3 `app/services/webhooks.py`

```
firma HMAC → registrar evento (UNIQUE) → consultar el pago al proveedor → apply_snapshot
```

- La firma se valida **antes** de tocar la base. Sin firma válida no se procesa nada.
- El evento se inserta primero. Si ya existía, se devuelve `duplicate` con 200 y no se
  vuelve a consultar al proveedor. Devolver 200 es intencional: un 4xx haría que Mercado
  Pago reintentara eternamente algo que ya está hecho.
- **El cuerpo del webhook no se cree.** Solo se toma de él el identificador del pago; el
  estado real se consulta a la API del proveedor. Un webhook falsificado con firma
  válida (que no debería existir) tampoco podría declarar "approved" por su cuenta.
- El resultado es explícito: `duplicate`, `ignored`, `applied` o `unknown_purchase`.

### 7.4 `app/services/letters.py`

**`create`** es donde se cumple IOP #5:

1. `lock_purchase` → `FOR UPDATE`. Doble clic, dos pestañas y reintentos se serializan.
2. Si la compra no existe o no es del usuario → 404 (el mismo error en ambos casos: no
   se revela que la compra existe pero es de otro).
3. Si no está `paid` → 409 `PURCHASE_NOT_PAID`.
4. Si ya hay carta → se devuelve **esa misma carta con 200**, no un error. Desde el
   frontend, un doble clic no produce un mensaje de error, produce la carta.
5. Si no, `INSERT … ON CONFLICT DO NOTHING` sobre `purchase_id`, por la misma razón que
   en compras.

El `public_slug` es `secrets.token_urlsafe(16)` — 128 bits de aleatoriedad. No es
secuencial ni derivado de datos del usuario, así que no se puede enumerar ni deducir.

**`is_frozen`** — `status == "published" and settings.freeze_letter_after_publish`. La
comprueban `update`, `publish`, `add_photo` y `remove_photo`. La decisión de congelar
está razonada en la sección 10.

**`add_photo`** — valida tamaño, detecta el tipo real **por firma binaria** (`sniff_content_type`
mira los primeros bytes: `\xff\xd8\xff` para JPEG, `\x89PNG…`, `RIFF…WEBP`), no por el
`Content-Type` que declara el cliente, que es texto libre. Calcula la posición
siguiente, sube el archivo al almacenamiento y luego inserta la fila. Si la inserción
falla por posición ocupada, **borra el archivo recién subido** para no dejar basura.

### 7.5 `app/services/deliveries.py`

Cada envío crea una fila con `status="pending"` y **se confirma antes de intentar el
envío**. Así, si el proceso muere durante el SMTP, queda constancia del intento. Después
se renderiza el correo, se envía y se marca `sent` o `failed`.

El `except Exception` guarda **solo el tipo de excepción** (`TimeoutError`,
`SMTPAuthenticationError`), nunca el mensaje: un error de SMTP puede contener el usuario
o parte de la configuración.

Un fallo de correo **no rompe la carta ni la compra**: la carta queda publicada, con su
enlace y su QR, y el usuario puede reenviar. El reenvío crea otra fila; nunca otra carta
ni otra compra.

### 7.6 Puertos externos

Los cuatro siguen el mismo patrón: una clase base con la interfaz, una implementación
real y una implementación inofensiva para local/pruebas, más una función `build_*` que
elige según la configuración.

**`payments.py`** — `PaymentGateway` con `fetch_payment`, `create_preference` y
`verify_webhook`. `UnconfiguredGateway` responde 503 `PAYMENTS_NOT_CONFIGURED`: sin
proveedor configurado la API **nunca** dice "pagado". `MercadoPagoGateway` usa un
`httpx.AsyncClient` con timeout de 8 s. `normalize_payment` traduce la respuesta del
proveedor a `PaymentSnapshot`, y un estado desconocido cae a `pending`, no a `approved`.

**`storage.py`** — `LocalStorage` (solo en local; resuelve la ruta y comprueba que no se
escape del directorio raíz, contra *path traversal*) y `AzureBlobStorage`, que importa
el SDK **de forma perezosa** porque es un extra opcional (`pip install '.[azure]'`). Las
operaciones de Azure son síncronas y se ejecutan con `anyio.to_thread.run_sync` para no
bloquear el bucle de eventos.

**`mailer.py`** — `ConsoleMailer` guarda los mensajes en memoria (local y pruebas);
`SmtpMailer` usa `smtplib` con STARTTLS y contexto SSL por defecto (verificado), también
en un hilo. `render_letter_email` genera el HTML; **todo lo que viene del usuario pasa
por `html.escape`**, así que un título con `<script>` no se convierte en HTML.

**`qr.py`** — `segno`, biblioteca en Python puro sin dependencias binarias (no arrastra
Pillow). `qr_png` devuelve la imagen; `qr_data_uri` la embebe en base64 dentro del
correo, para que el QR se vea aunque el cliente bloquee imágenes remotas.

---

## 8. Contratos: `app/schemas/`

Los esquemas de entrada heredan de `Input`, que usa `extra="forbid"`. Es una defensa
concreta contra *mass assignment*: si alguien manda `{"email": …, "role": "admin"}`, la
petición se rechaza con 422 en vez de ignorar el campo en silencio.

En `commerce.py`, `_clean_text` normaliza `\r\n` a `\n` y **rechaza caracteres de
control** salvo salto de línea y tabulador. Emojis, acentos y saltos de línea son
contenido legítimo de una carta y se conservan tal cual; hay pruebas de ello.

Las respuestas son esquemas aparte. `PublicLetterResponse` es el ejemplo de por qué:
contiene título, destinatario, cuerpo, tema y fotos, y **no** contiene `userId`,
`purchaseId`, correo del comprador ni nada relacionado con la cédula. Al ser un modelo
distinto, no hay forma de que un campo nuevo del modelo de base de datos se filtre al
visor público por descuido.

---

## 9. Endpoints: `app/api/routers/commerce.py`

Tres routers separados por su modelo de autenticación:

- `router` (`/api/v1`) — requiere sesión; las escrituras además CSRF.
- `public_router` (`/api/v1/public`) — sin sesión y sin CSRF, solo lectura.
- `webhook_router` (`/api/v1/webhooks`) — sin sesión y sin CSRF, autenticado por firma.

Detalles a tener en cuenta:

- `create_purchase` y `create_letter` ajustan `response.status_code` a 201 o 200 según
  si crearon o encontraron. El frontend puede tratar ambos como éxito.
- `owned_letter` y `owned_purchase` filtran **siempre por `user_id`** en la propia
  consulta. No hay ningún endpoint que cargue por id y compruebe el dueño después, que
  es como se cuelan los IDOR.
- Las fotos se sirven por endpoints proxy (`/letters/{id}/photos/{photo}/content` y
  `/public/letters/{slug}/photos/{n}`) en lugar de exponer URLs de Azure. Así el control
  de acceso lo hace la API y no queda un enlace de blob permanente circulando.
- `letter_payload` arma la respuesta consultando fotos y entregas explícitamente. No hay
  relaciones perezosas de SQLAlchemy en las respuestas, que en async fallarían.

### 9.1 Flujo completo, de principio a fin

```
GET  /api/v1/auth/csrf                     → token CSRF
POST /api/v1/auth/register  → 201
POST /api/v1/auth/login     → 200 + cookie de sesión
POST /api/v1/purchases      {idempotencyKey}          → 201 (repetir: 200, misma compra)
   … el usuario paga en el checkout de Mercado Pago …
POST /api/v1/purchases/{id}/verify {paymentId}        → 200, purchase.status = "paid"
POST /api/v1/letters        {purchaseId, title, …}    → 201 (repetir: 200, misma carta)
POST /api/v1/letters/{id}/photos   (multipart)        → 201
POST /api/v1/letters/{id}/publish                     → 200 + publicUrl + qrUrl + correo enviado
GET  /api/v1/public/letters/{slug}                    → visor público, sin sesión
POST /api/v1/letters/{id}/deliveries                  → 202, reenvío sin consumir compra
```

---

## 10. Decisiones de diseño y su porqué

**Sesiones opacas en vez de JWT.** Revocación inmediata, sin token que siga siendo
válido después de cerrar sesión y sin necesidad de una lista negra. El coste es una
consulta por petición, que en este volumen es irrelevante. Las variables de JWT en Azure
quedan sin efecto.

**Restricciones en la base en vez de validaciones en Python.** Un `if not existe_carta`
seguido de un `insert` es una condición de carrera clásica: dos peticiones pasan el `if`
a la vez. Un `UNIQUE` no se puede saltar por concurrencia.

**`ON CONFLICT DO NOTHING` en vez de capturar `IntegrityError`.** Encontramos que con el
pool al límite, el `rollback()` tras el error no conseguía conexión y devolvía 503.
Evitar la excepción es más robusto y además más rápido.

**El segundo intento devuelve 200 con el recurso existente, no un error.** Es lo que el
usuario quiere: hizo doble clic, quiere su carta.

**Congelar el contenido al publicar** (`FREEZE_LETTER_AFTER_PUBLISH=true`). El
destinatario ya recibió un enlace y quizá un QR impreso; que el contenido cambie después
rompe esa promesa. Se permite consultar, reenviar y mandar a otra dirección. El campo
`published_version` existe para que, si negocio decide lo contrario, baste con poner la
variable en `false` y cada republicación incremente la versión.

**La cédula no es credencial ni identificador.** La identidad interna es `users.id`
(UUID). La cédula es un dato privado guardado como HMAC, que no aparece en el perfil, ni
en el slug, ni en el visor público.

**El pago se verifica siempre en el servidor.** El retorno del navegador solo aporta un
identificador; cualquiera puede fabricar esa redirección. La verdad viene de la API del
proveedor.

---

## 11. Pruebas: `tests/`

114 pruebas, todas contra un PostgreSQL real y efímero, ninguna con credenciales reales.

- `conftest.py` — se niega a arrancar si `TEST_DATABASE_URL` no apunta a loopback y a la
  base `zy_auth_validation`, para que nadie ejecute la suite contra una base buena. Trunca
  las tablas entre pruebas. Define `FakeGateway`, un proveedor de pagos determinista.
- `test_base.py` — salud, CORS, OpenAPI, configuración rechazada, CSRF, Google.
- `test_auth.py` — registro, login, perfil, logout, sesiones vencidas y revocadas,
  concurrencia, Google.
- `test_commerce.py` — idempotencia, pago no confirmado, montos que no cuadran, webhooks
  repetidos y fuera de orden, una compra ⇒ una carta, doble clic y pestañas, permisos
  entre usuarios, cédula privada, borradores, emojis y saltos de línea, orden de fotos,
  congelado, visor público, QR, fallo y reintento de correo.
- `test_config_matrix.py` — traducción de TLS, entornos, presupuesto de conexiones,
  integraciones que exigen sus secretos, y que la API nunca devuelve un token portador.
- `test_google_crypto.py` — firmas RSA reales generadas localmente, con el transporte de
  certificados simulado. Rechaza firma, audiencia, emisor, expiración y nonce erróneos.

Cómo ejecutarlo todo:

```bash
.venv/bin/python scripts/validate_local.py --docker --postman
```

Ese script levanta su propio PostgreSQL (contenedor o binarios locales), corre
`upgrade → check → downgrade → upgrade`, pytest y Newman, y destruye el entorno al
terminar, incluso si algo falla. **No lee `.env` y nunca usa una base existente.**

---

## 12. Operación

### 12.1 Migraciones

```bash
alembic upgrade head
```

Nunca se ejecuta al arrancar la aplicación, y hay que lanzarla **una sola vez por
entorno**, no una por worker.

**`MigrationSettings` (`app/core/config.py`).** Alembic no usa `Settings`, sino una
configuración reducida que solo pide `DATABASE_URL`. El motivo es operativo: el pipeline
de CD ejecuta la migración en un contenedor efímero con

```bash
docker run --rm -e DATABASE_URL="$FIXED_URL" imagen:tag alembic upgrade head
```

y una migración no atiende peticiones, no emite cookies, no sube fotos ni envía correo.
Exigirle `SESSION_SECRET`, `CORS_ORIGINS` o el almacenamiento de Azure obligaría a
inyectar secretos que no usa. **Las reglas de TLS son idénticas**: ambas clases llaman a
`resolve_database_url`, así que una URL con `?sslmode=disable` en un entorno remoto sigue
abortando también en la migración.

El CD reescribe `sslmode=` a `ssl=` antes de pasar la URL; da igual, el parser acepta las
dos formas y las eleva a `verify-full`. Conviene añadir `-e APP_ENV=develop` (o el
entorno que toque) al `docker run`: sin él, `APP_ENV` cae a `local` y la migración
aceptaría una URL sin TLS si alguien la configurara así. Con las cadenas actuales de
Azure, que traen `require`, el TLS queda verificado igualmente. Antes de aplicarla sobre una base existente, revisa el
esquema con su responsable: `0001` crea `users` y `auth_sessions`, y `0002` las siete
tablas del dominio comercial. No uses `stamp` para tapar un conflicto.

Al añadir una migración nueva, actualiza `EXPECTED_REVISION` en `app/main.py` o la
readiness quedará en 503.

### 12.2 Qué revisar al desplegar

1. `APP_ENV` correcto (`main` → `production`).
2. `DB_SSL_MODE=verify-full` o una URL con `sslmode=require` que el parser eleve.
3. `FRONTEND_URL` real, en HTTPS. Hoy figura como `PENDING` en Azure y de él dependen el
   enlace del correo y el QR.
4. `STORAGE_BACKEND=azure` con contenedor creado, e instalar el extra:
   `pip install '.[azure]'`.
5. `WEB_CONCURRENCY` y `APP_REPLICAS` con los valores **reales**, o el presupuesto de
   conexiones no significa nada.
6. `SESSION_SECRET` idéntico en todas las réplicas y distinto entre entornos.

La lista exacta de variables que hay que dar de alta en cada App Service, y las que
sobran por venir del proyecto de referencia, está en
[`MATRIZ_CONFIGURACION.md`](MATRIZ_CONFIGURACION.md) §8.

### 12.3 Cuando la aplicación no arranca

`get_settings()` captura el `ValidationError` de pydantic y lanza `ConfigurationError`
con un diagnóstico legible: qué variable falla, por qué y cómo obtenerla. En el log del
App Service se ve un bloque como este, en lugar de cincuenta líneas de traza:

```
========================================================================
 La aplicación no puede arrancar: configuración inválida (aplicación)
========================================================================
  SESSION_SECRET: Field required
      -> Secreto aleatorio de al menos 32 caracteres, idéntico en todas las
         réplicas. Genera uno con: python -c "import secrets; ..."
         Esta API no usa JWT: JWT_SECRET_KEY no lo sustituye.
========================================================================
```

Nunca incluye valores: `hide_input_in_errors=True` los omite y los mensajes solo nombran
variables. Cada comprobación del validador dice qué arreglar (`CORS_ORIGINS`,
`FRONTEND_URL`, `STORAGE_BACKEND`, el cálculo exacto del presupuesto de conexiones), no
solo que algo está mal.

Además, si existe `WEBSITE_SITE_NAME` —que Azure App Service define siempre— y `APP_ENV`
no se declaró, la aplicación se niega a arrancar: sin esa variable caería a `local` y
serviría cookies sin `Secure` en un despliegue real.

### 12.4 Diagnóstico

`GET /api/v1/health/commerce` devuelve qué integraciones están activas (proveedor de
pagos, almacenamiento, correo, congelado) **sin revelar ningún secreto**. Es lo primero
que hay que mirar cuando algo responde 503 en un entorno.

### 12.5 Pendientes conocidos

- Limitador de intentos distribuido entre réplicas.
- Limpieza programada de sesiones vencidas y compras caducadas (los índices ya existen).
- Vinculación explícita de cuentas Google, recuperación de contraseña y verificación de
  correo.
- Confirmar el esquema de firma del webhook contra una notificación real de Mercado Pago.
- Rotar los secretos expuestos en `docs/AZURECONF.md` y moverlos a Key Vault.

---

## 13. Cómo añadir algo nuevo

Ejemplo, un endpoint que liste las entregas de una carta:

1. **Esquema** en `app/schemas/commerce.py` si el contrato es nuevo.
2. **Consulta** en `app/repositories/commerce.py`, filtrando por `user_id`.
3. **Regla de negocio** en `app/services/…` si hay alguna; si es solo leer, no hace falta.
4. **Endpoint** en `app/api/routers/commerce.py`, con `Depends(current_user)` y, si
   escribe, `Depends(csrf_guard)`.
5. **Prueba** en `tests/test_commerce.py`, incluyendo el caso de otro usuario (404).
6. Si tocaste modelos: migración nueva, `alembic check` para confirmar que no queda
   *drift*, y actualizar `EXPECTED_REVISION`.
7. Añade la petición a la colección de Postman si es parte de un flujo.

Antes de dar por terminado: `ruff check`, `ruff format` y
`scripts/validate_local.py --docker --postman`.
