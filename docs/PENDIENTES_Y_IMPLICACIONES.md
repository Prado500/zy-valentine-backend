# Pendientes de código e implicaciones de las decisiones

**Fecha:** 2026-09-11
**Contexto:** hallazgos acumulados durante el montaje de Service Bus y el diagnóstico del
fallo de sesión en la pasarela de pago.

Este documento tiene dos partes. La **A** lista lo que toca código y está pendiente, para
tratarlo en sesiones posteriores. La **B** recoge las implicaciones de las decisiones de
infraestructura que ya se tomaron: lo que cada una compra, lo que cuesta, y lo que se
rompe si alguien la olvida.

Nada de lo aquí descrito está implementado. Lo que sí está hecho vive en
[`INFRAESTRUCTURA_Y_CAPACIDAD.md`](INFRAESTRUCTURA_Y_CAPACIDAD.md).

---

# Parte A · Hallazgos que tocan código

Ordenados por impacto. La columna *Coste* es esfuerzo de desarrollo, no de infraestructura.

| # | Hallazgo | Impacto | Coste |
| --- | --- | --- | --- |
| A1 | Conexión de BD retenida durante trabajo lento | **Alto** · techo de 20 req/s | Medio |
| A2 | `CapacityLimiter(2)` del hash, en un B3 de 4 vCPU | **Alto** · duplica el registro | Trivial |
| A3 | Los logs INFO de la API son invisibles en producción | **Medio** · ceguera operativa | Trivial |
| A4 | El webhook es el único camino sin navegador, y no se vigila | **Alto** · pierde pagos | Bajo |
| A5 | `/health/ready` no comprueba la cola | Medio | Bajo |
| A6 | `google_limiter = CapacityLimiter(1)` | Medio · cola de login con Google | Trivial |
| A7 | Hueco de `APP_REPLICAS` en el presupuesto de conexiones | Medio · latente | Trivial |
| A8 | La puerta de Service Bus está duplicada en Python y en shell | Bajo · fragilidad | — |
| A9 | Los logs de `pyamqp` ensucian el Log stream | Bajo | Trivial |
| A10 | `--no-proxy-headers` y la IP real del titular | Bajo · cumplimiento | Medio |
| A11 | `AutoLockRenewer` en el worker | Bajo · YAGNI hoy | Bajo |
| A12 | Cookie `Partitioned` (CHIPS) | **Nulo si se hacen los dominios** | Trivial |

## A1 · Conexión de BD retenida durante trabajo lento

**Qué pasa.** La sesión de SQLAlchemy toma una conexión en la primera consulta y no la
suelta hasta el `commit`. En tres rutas, entre medias ocurre algo lento que no tiene nada
que ver con PostgreSQL, y la conexión se queda ocupada sin hacer nada:

| Dónde | Qué espera con la conexión retenida | Coste |
| --- | --- | --- |
| `app/services/auth.py:31-36` | `by_email` toma conexión → **hash Argon2id** → `flush` | 45–120 ms |
| `purchases.create_intent` | `purchase_by_idempotency` → **`create_preference` HTTP a MP** → `insert` | ~300 ms |
| `commerce.verify_purchase` | `_owned_purchase` → **`fetch_payment` HTTP a MP** → `apply_snapshot` | ~300 ms |

**Por qué importa.** Una conexión que podría despachar 80 transacciones por segundo
despacha 3. Con las 6 conexiones de API de main, el techo son ~20 peticiones por segundo en
las tres rutas más calientes, y **el disco, que aguanta ~100 transacciones/s, trabaja al
20 % esperando a Mercado Pago**. Es el cambio de mayor impacto de toda esta lista.

**Qué se rompe si no se hace.** Nada hoy, con el volumen real. Pero es el techo que hace
que 8 000 compradores en un minuto devuelvan errores 500 a los dos segundos. Detalle
completo en `INFRAESTRUCTURA_Y_CAPACIDAD.md` §8.

**Cómo abordarlo.** Liberar la sesión antes del trabajo lento y volver a tomarla después, o
mover el hash fuera del alcance de la sesión. Requiere cuidado con las transacciones: en
`create_intent` y `verify` hay que preservar la semántica del `SELECT … FOR UPDATE`.

**Detalle que ya está bien y no hay que romper.** En `purchases.verify` la llamada HTTP
ocurre **antes** del `FOR UPDATE`, no dentro. Si fuera al revés, cada comprador mantendría
un candado de fila abierto durante 300 ms de red.

## A2 · `CapacityLimiter(2)` del hash

**Qué pasa.** `app/main.py:36` limita a **dos hashes Argon2id concurrentes**. Medido con el
hasher real del proyecto: **45 ms** (mediana de 5, en una máquina de desarrollo); en un vCPU
de App Service se asumen 80–120 ms. Techo de registro: **~20 por segundo**.

**Por qué está así, y por qué ya no aplica.** El límite se puso pensando en el núcleo único
de la B1ms. Pero **el hash corre en el App Service, no en la base de datos**, y main es un
**B3 con 4 vCPU**. El razonamiento era correcto para el sitio equivocado.

**Qué hacer.** Subirlo a 4 en main. Verificar la memoria antes: Argon2 con los parámetros
recomendados de pwdlib usa **64 MiB por hash**, así que 4 concurrentes son 256 MiB. En un
B3 de 7 GB sobra.

**Si con eso no alcanza**, el siguiente paso es ajustar los parámetros de Argon2
(`memory_cost` de 64 a 32 MiB, `time_cost` de 3 a 2, ~20 ms por hash). Eso es una decisión
de seguridad, no solo de rendimiento: hay que documentarla.

## A3 · Los logs INFO de la API son invisibles en producción

**Descubierto el 2026-09-11 diagnosticando el arranque de la cola.**

**Qué pasa.** `build_publisher` escribe `LOG.info("Service Bus activo sobre la cola
configurada")` y **ese mensaje nunca aparece en Azure**. `worker.py` llama a
`logging.basicConfig(level=logging.INFO)` en su `main()`, y por eso sus logs y los del SDK
de AMQP sí se ven. La API, bajo uvicorn, no configura nada: uvicorn solo prepara sus propios
loggers (`uvicorn`, `uvicorn.error`, `uvicorn.access`), así que un `INFO` de `app.*` se
propaga a la raíz, no encuentra handler, cae en `logging.lastResort` y **se descarta**,
porque ese handler solo emite `WARNING` y por encima.

**Qué se pierde.** Todo el diagnóstico de la API en producción. Incluido el que dice si el
publicador de la cola se construyó. Durante la verificación del paso 6 hubo que deducirlo
por los logs AMQP del worker, que es otro proceso.

**Qué hacer.** Configurar el logging en `create_app`, con el mismo formato que usa
`worker.py` para que los dos procesos se lean igual. Una línea, y hay que comprobar que no
duplica los registros de acceso de uvicorn.

## A4 · El webhook es el único camino sin navegador, y no se vigila

**Qué pasa.** `apply_snapshot` (`app/services/purchases.py:113`) es el único sitio que
escribe en `payments`, y se llama desde dos lugares: `purchases.verify` —que dispara el
navegador del comprador— y `webhooks.handle` —que dispara Mercado Pago—. Hasta el
2026-09-11 la URL de notificación de Mercado Pago apuntaba a la raíz del backend y devolvía
**405**, así que **todos los pagos registrados hasta esa fecha entraron por `/verify`**.

**Por qué es grave.** El webhook no es una redundancia cómoda: es el único camino que no
depende del navegador del comprador. Sin él:

| Escenario | Resultado |
| --- | --- |
| El navegador cooperа | `/verify` registra el pago |
| El navegador borra las cookies (Brave, Safari) | **nadie registra nada** |
| El comprador cierra la pestaña en el redirect | **nadie registra nada** |
| Se cae la conexión al volver | **nadie registra nada** |

En los tres últimos el dinero sale y el sistema no se entera.

**Parte de configuración** (sin código): URL completa
`https://<app>/api/v1/webhooks/mercadopago`, evento **Pagos** (`payment`) y el secreto de la
aplicación en `MERCADOPAGO_WEBHOOK_SECRET`. Hay **un secreto para pruebas y otro para
producción**, y no se rota al guardar cambios de configuración: el que ya está en las
variables sigue siendo válido.

**Parte de código, resuelta.** Al arreglar la URL apareció un segundo fallo que la tapaba:
`MercadoPagoGateway.verify_webhook` leía el `data.id` de una cabecera `x-data-id` que Mercado
Pago no envía nunca, así que el manifiesto salía mal y **todas** las notificaciones se
rechazaban con 401. Corregido en la rama `feat/webhook-signature-data-id`, con 13 pruebas
sobre una validación que tenía cobertura cero. Los dos fallos se tapaban entre sí: con la URL
mal, ese código nunca se ejecutaba, así que nadie vio que también estaba mal.

**Parte de código, pendiente:** no hay ninguna alerta ni métrica sobre compras que quedan en
`pending` con un pago aprobado en Mercado Pago. Una tarea periódica que reconcilie —o al
menos una consulta de auditoría— convertiría este punto ciego en algo observable. Mientras
no exista, el **Panel de notificaciones** del dashboard de Mercado Pago es la única
vigilancia, y es manual.

## A5 · `/health/ready` no comprueba la cola

**Qué pasa.** `/api/v1/health/commerce` ya informa el publicador real y no la simple
presencia de variables (corregido en el PR del arranque supervisado). Pero el cliente de
Azure se construye de forma perezosa, así que `enabled = True` prueba que el constructor
funcionó, **no que la cola responda**. Y `/health/ready` no mira la cola en absoluto.

**Qué dice la documentación de Azure.** El Health check de App Service recomienda
explícitamente lo contrario de lo que hay hoy:

> *"if your application depends on a database and a messaging system, the Health check
> endpoint should connect to those components. If the application can't connect to a
> critical component, the path should return a 500-level response code."*

**Por qué no se hizo ya.** Cambia el comportamiento de reinicio del App Service: con el
Health check activado, un endpoint que devuelve 500 durante una hora hace que la instancia
se reemplace. Hay que decidir umbrales y qué se considera crítico. Con `APP_REPLICAS=1` no
hay a dónde enrutar el tráfico, así que el efecto es un reinicio, no una mitigación.

## A6 · `google_limiter = CapacityLimiter(1)`

**Qué pasa.** `app/main.py:37` serializa las verificaciones de token de Google **a una**.
Con la caché de certificados de `CacheControl` una verificación cuesta ~10 ms, así que da
~100/s y no es un problema de caudal. El riesgo es otro: **si la caché falla y Google
responde lento, el timeout es de 5 s** (`app/services/google.py:24`), y durante esos 5
segundos **toda la ruta de login con Google está detenida**, porque solo cabe una.

**Qué hacer.** Subirlo a 2–4 y revisar el comportamiento en fallo de caché.

## A7 · Hueco de `APP_REPLICAS` en el presupuesto de conexiones

**Qué pasa.** `app/core/config.py:322-325`:

```python
api = self.web_concurrency * self.app_replicas * (self.db_pool_size + self.db_max_overflow)
worker = self.worker_db_pool_size if self.service_bus_enabled else 0
```

`api` se multiplica por `APP_REPLICAS`; `worker` **se suma una sola vez**. Con el worker
dentro del mismo contenedor (que es donde está desde el paso 6), `APP_REPLICAS=2` produce
**dos** workers reales —10 conexiones— y la validación solo cuenta 5.

**Inocuo hoy** con `APP_REPLICAS=1` en los tres entornos. **Hay que arreglarlo antes de
escalar main horizontalmente**, porque la validación dejaría arrancar con el doble de
conexiones de las que declara, contra un servidor que solo da 35.

## A8 · La puerta de Service Bus está duplicada

**Qué pasa.** La condición "las dos variables o ninguna" existe dos veces:

- `Settings.service_bus_enabled` en `app/core/config.py:362` (Python)
- El `if` de `entrypoint.sh` (shell)

**Por qué se hizo así.** El guion de arranque necesita decidir si lanza el worker **antes**
de que exista un intérprete de Python con la configuración cargada. No hay forma de
reutilizar la propiedad.

**El riesgo.** Son dos implementaciones de la misma regla en dos lenguajes, y **ninguna
prueba detecta que divergan**. `test_el_worker_solo_arranca_con_las_dos_variables_de_la_cola`
fija el contrato del lado del shell, pero no lo compara con Python. Si algún día se añade
una tercera condición a `service_bus_enabled` —por ejemplo autenticación por identidad
administrada, que no usaría cadena de conexión— hay que tocar los dos sitios a mano.

**Opción si molesta:** que el guion invoque `python -c` para preguntarle a `Settings`. Cuesta
un intérprete más en el arranque y acopla el guion al import de la aplicación. Hoy no se
paga.

## A9 · Los logs de `pyamqp` ensucian el Log stream

Cada conexión y reconexión del worker escribe una decena de líneas de estado de conexión,
sesión y enlaces en INFO. Es información útil hoy, mientras se verifica cada entorno; deja
de serlo cuando esté estable y hace más difícil encontrar lo propio de la aplicación.

Una línea: `logging.getLogger("azure.servicebus._pyamqp").setLevel(logging.WARNING)`. No
antes de que los tres entornos estén verificados.

## A10 · `--no-proxy-headers` y la IP real del titular

**Qué pasa.** `audit_trail` (`app/api/routers/auth.py:56`) lo documenta él mismo:

> *"uvicorn corre con `--no-proxy-headers`, así que detrás de Azure esta IP es la del
> ingress, no la del titular. Arreglarlo toca el rate limiter y va en un PR aparte."*

**Por qué empeora con Cloudflare.** Si el proxy de Cloudflare queda activo delante de la
API, la IP que ve la aplicación es la de Cloudflare, un salto más lejos del titular.

**Por qué importa.** Es la prueba de autorización del tratamiento de datos personales
(Decreto 1377 de 2013, art. 7). Guardar la IP del ingress es mejor que nada, pero no es lo
que la norma pretende.

**Coste real.** Activar los proxy headers cambia de dónde sale `request.client.host`, y de
eso depende el rate limiter de autenticación. Hay que hacerlo con la lista de proxies de
confianza bien puesta, o se abre la puerta a falsear la IP y saltarse el límite.

## A11 · `AutoLockRenewer` en el worker

El worker no renueva el bloqueo de los mensajes. El lock de la cola está en **5 minutos**,
el máximo que permite Azure, y el peor lote medido cabe dentro. Pero el margen depende de
dos variables que ya se movieron una vez:

```
peor lote = ceil(SERVICE_BUS_MAX_BATCH / WORKER_DB_POOL_SIZE) x MAIL_TIMEOUT
```

Con `MAIL_TIMEOUT=40`: main (lote 12, pool 5) da 120 s; dev y stg con lote 4 y pool 2 dan
80 s. Si alguien sube el lote o el timeout sin hacer esta cuenta, el lock vence a mitad de
lote, Service Bus reentrega, y aparecen correos duplicados o cartas en la dead-letter.

**YAGNI hoy.** Pero si se toca cualquiera de esas tres variables, hay que rehacer la cuenta.

## A12 · Cookie `Partitioned` (CHIPS)

`set_cookie` (`app/api/routers/auth.py:27-36`) no pone el atributo `Partitioned`. Starlette
1.6.0 **ya lo soporta**: `set_cookie(..., partitioned=True)`, una palabra.

**Solo tiene sentido si se mantiene una topología cross-site.** Con los dominios propios de
la Parte B, la cookie deja de ser de tercera parte y este punto **desaparece**. No se hace.

---

# Parte B · Implicaciones de las decisiones tomadas

## B1 · Dominio propio solo en main → dev y stg no pueden reproducir fallos de cookies

**La decisión.** `zyexperience.com` para el SWA y `api.zyexperience.com` para el App Service,
**solo en main**. Dev y stg se quedan en `*.azurestaticapps.net` + `*.azurewebsites.net` y se
prueban en Chrome.

**Lo que compra.** Same-site en producción: la cookie deja de ser de tercera parte y el
problema desaparece para Brave, Safari y cualquier política futura de Chrome. Cero código.

**Lo que cuesta, y es lo importante.** Los entornos dejan de ser equivalentes en la
dimensión que más cuesta depurar:

| | main | dev y stg |
| --- | --- | --- |
| Topología | same-site | cross-site |
| `COOKIE_SAMESITE` | `lax` | `none` |
| Cookie de sesión | primera parte | **tercera parte** |
| Reproduce fallos de cookies de main | — | **no** |

Consecuencia concreta: **un fallo de sesión que solo aparezca en main se diagnostica en
producción, con compradores dentro.** Y al revés: dev y stg seguirán rompiéndose en Brave y
en Safari, así que no sirven para validar el flujo de pago en esos navegadores.

**Corrección a una premisa.** No hace falta comprar otro dominio. **Los subdominios del que
ya existe dan el mismo resultado**, porque same-site se decide por dominio registrable:

```
dev.zyexperience.com      +  api-dev.zyexperience.com     → same-site
stg.zyexperience.com      +  api-stg.zyexperience.com     → same-site
```

Y sale gratis: el plan **Free** de Static Web Apps admite **2 dominios personalizados por
app** con certificado gestionado gratuito, y el App Service Basic también. Es el mismo
trabajo de DNS repetido dos veces.

**Recomendación.** Hacer main primero, como está planeado. Pero cerrar la divergencia con
subdominios en dev y stg en cuanto haya un hueco, **antes de que aparezca el primer fallo
que solo se vea en producción**. El coste es DNS, no dinero.

## B2 · Un namespace de Service Bus compartido por los tres entornos

**Lo que compra.** ~20 USD/mes frente a tres namespaces, y menos superficie que administrar.
A este volumen el *noisy neighbor* es teórico: 27 de los 1 000 créditos por segundo en el
pico previsto.

**Lo que cuesta.** Las llaves de los tres entornos viven en el mismo namespace, y cualquier
operación a nivel de namespace afecta a los tres. Lo mitigan las **directivas SAS por cola**:
la cadena de cada entorno lleva `EntityPath=<su cola>` y el SDK lo valida, así que dev es
incapaz de tocar la cola de producción.

**Pendiente y sin resolver.** El candado `CanNotDelete` sobre el namespace. Crear candados
exige `Microsoft.Authorization/locks/*`, que solo traen los roles **Owner** y **User Access
Administrator**; el rol disponible es Contributor. Detalle y comandos en
`INFRAESTRUCTURA_Y_CAPACIDAD.md` §2.4. Mientras no exista, la alternativa es una alerta del
log de actividad sobre `Microsoft.ServiceBus/namespaces/delete`.

**Qué pasa si alguien lo borra.** Las variables siguen puestas, la API intenta publicar,
falla y **cae al camino síncrono**. Se pierde solo lo que estuviera dentro de las colas en
ese instante. Degradación, no caída.

## B3 · Un solo servidor PostgreSQL para las tres bases

**El techo.** `max_connections = 50` en la B1ms, 15 reservadas por el motor, **35 para la
aplicación**, y **los tiers burstable no tienen PgBouncer**.

**La implicación que no verifica nadie.** `DB_CONNECTION_BUDGET` es una promesa que cada
entorno se hace a sí mismo: ningún entorno sabe que los otros dos existen. Nada impide que
los tres declaren 20 y entre los tres pidan 60 de un servidor que tiene 35. **La suma la
vigilan las personas.**

**Regla operativa.** La suma de los tres `DB_CONNECTION_BUDGET` no puede pasar de **30**.
Reparto vigente: dev 8 + stg 8 + main 14 = 30, con 19 conexiones reales en uso y 5 libres
para el `alembic upgrade head` del CD, `psql` y pgAdmin.

**Si algún día hace falta más.** Subir a **B2s** cambia el techo de 50 a **429 conexiones**
(414 de usuario) y añade un segundo vCore, lo que además desactiva la preocupación por
*context switching* que justificaba el límite conservador. Cuesta **+37,23 USD/mes**.

## B4 · Si el worker muere, el contenedor se cae

**La decisión.** `entrypoint.sh` vigila a `worker.py` y, si desaparece, manda `SIGTERM` al
proceso principal.

**Lo que compra.** El fallo pasa de invisible a observable. Antes, un worker muerto dejaba
la API respondiendo 202 sin que nadie vaciara la cola, sin error, sin log y sin métrica.
Ahora produce un bucle de reinicios que se ve en el Log stream.

**Lo que cuesta.** Un error de configuración en main **tumba el sitio** en lugar de
degradarlo a modo síncrono. Se aceptó a propósito: un 202 que miente pierde pedidos pagados
en silencio, y eso es peor que un reinicio visible.

**Por qué no dispara en falso.** `worker.py` no termina por caídas de Service Bus: su bucle
captura la excepción, registra `El consumidor cayó` y reconecta indefinidamente. Solo sale
por señal, por `ConfigurationError` o por falta del paquete `azure-servicebus`. Cuando el
vigilante dispara, algo está roto de verdad.

**Mitigación obligatoria.** Verificar dev y stg de punta a punta **antes** de tocar main.

## B5 · El Startup Command sobrescribe el `CMD` de la imagen

Mientras el campo *Startup Command* del App Service tenga algo, **sobrescribe el `CMD`** y el
`entrypoint.sh` no se ejecuta. Hay que **vaciarlo** en cada entorno después de desplegar.

No hay ventana de caída: el comando viejo sigue levantando el worker hasta que se borra el
campo. Pero si se olvida, el despliegue parece no haber hecho nada y se pierde media tarde
buscando en el sitio equivocado. La primera línea del Log stream
(`entrypoint: cola configurada...`) es la que delata si sigue puesto.

## B6 · Cloudflare delante del dominio

| Trampa | Qué hacer |
| --- | --- |
| La validación de dominio y el certificado gestionado de Azure fallan con el proxy activo | Crear los registros en **DNS only** (nube gris), validar, emitir el certificado, y **solo entonces** decidir si se enciende el proxy |
| Modo SSL *Flexible* termina el TLS en Cloudflare y habla HTTP con Azure | **Full (Strict)**, o se rompen las expectativas de `secure_cookies` y aparecen bucles de redirección |
| El WAF y la protección anti-bots pueden bloquear los POST del webhook de Mercado Pago | Dejar **`api.zyexperience.com` en nube gris**. El frontend sí puede ir en naranja |
| El proxy oculta la IP del titular | Ver A10. Ya era una limitación conocida; el proxy la empeora un salto |

## B7 · Claves PII distintas por entorno, y no se pueden rotar

`PII_ENCRYPTION_KEY` y `PII_HMAC_KEY` deben ser **un par distinto por entorno**: las de dev
pasan por más manos, y si se filtran no pueden descifrar cédulas de compradores reales.

**Lo que no se puede deshacer.** Si están vacías se derivan de `SESSION_SECRET`
(`config.py:402`). Al fijarlas, la clave cambia y **AES-GCM falla la autenticación de todo
lo cifrado antes**: los números de documento quedan irrecuperables, y son los que exige la
factura electrónica de la DIAN. Igual con el HMAC: cambiarlo hace que `identity_by_hash` no
encuentre los documentos ya registrados y la comprobación de unicidad deje de funcionar.

**Regla.** Fijarlas **antes del primer registro real** de cada entorno, comprobando primero
que la tabla de documentos esté vacía. Después, no se tocan nunca sin un plan de migración.

## B8 · Tres versiones de Python distintas

| Dónde | Versión |
| --- | --- |
| `.python-version` y el pipeline de CI | **3.11** |
| `Dockerfile` (`python:3.12-slim`) | **3.12** |
| El venv local de desarrollo | **3.14** |
| `ruff` (`target-version`) | **py311** |

Se prueba en una versión, se despliega en otra y se desarrolla en una tercera. Riesgo bajo
—el código no usa nada exótico— pero real: un comportamiento que cambie entre versiones
menores pasaría el CI y fallaría en producción. Merece su propio ticket, no mezclarlo con
otra cosa.

## B9 · `MAIL_TIMEOUT=40` y el margen del lock

Ver A11. El peor lote es
`ceil(SERVICE_BUS_MAX_BATCH / WORKER_DB_POOL_SIZE) × MAIL_TIMEOUT`, y el lock son 300 s.
Cualquier cambio en esas tres variables obliga a rehacer la cuenta.

Y en el **camino síncrono** —cuando la cola no está disponible— el envío SMTP ocurre dentro
de la petición HTTP, así que `POST /api/v1/letters` puede tardar 40 segundos largos. No
rompe nada (App Service corta a los 230 s), pero el comprador espera.

## B10 · Backend enlazado al SWA: descartado, y por qué

Static Web Apps permite enlazar un App Service como backend propio, de modo que el frontend
llame a `/api/...` **en su propio origen** y la cookie sea de primera parte sin necesidad de
dominios. Se descartó por tres razones documentadas:

- Solo está disponible en el plan **Standard**; dev y stg están en **Free** (+9 USD/mes cada uno).
- El tope de duración por petición es de **45 segundos**, incómodo con `MAIL_TIMEOUT=40` en
  el camino síncrono.
- **No se puede enlazar a entornos de pull request** de Static Web Apps.

Los dominios propios consiguen lo mismo sin ninguna de las tres restricciones.

## B11 · `.claude/` sin seguimiento en la raíz del repositorio

Decidir si se versiona o entra en el `.gitignore`, antes de que aparezca en un commit por
accidente.

---

# Parte C · Orden de trabajo acordado

1. **Webhook de Mercado Pago** — URL completa, evento Pagos, secreto. Configuración, sin
   código. Cierra A4 en su parte de infraestructura.
2. **Verificación sin dinero** — compra en dev o stg con Chrome, y confirmar la entrega en
   el Panel de notificaciones de Mercado Pago.
3. **Dominios propios en main** — Cloudflare en gris, validar, certificados, y las cuatro
   variables. Cierra el problema de cookies en producción y hace innecesario A12.
4. **La compra real de 30 000 COP** — solo después de 1 y 3, para que un fallo del navegador
   no pierda un pago real.
5. **A revisar después**, por orden de rentabilidad: A2 y A3 (triviales), A6, A1 (el de mayor
   impacto), B1 con subdominios para cerrar la divergencia entre entornos, A7 antes de
   cualquier escalado horizontal.
