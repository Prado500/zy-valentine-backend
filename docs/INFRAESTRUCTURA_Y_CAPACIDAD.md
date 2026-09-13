# Infraestructura, costos y capacidad

**Fecha del levantamiento:** 2026-09-11
**Rama:** `feat/registro-documento-dian-consentimiento`
**Región de todo:** East US 2 (`eastus2`)

Este documento recoge el montaje de Azure Service Bus, el inventario de infraestructura
con costos reales, y el análisis de capacidad del camino de escritura. Se escribió como
insumo para una sesión posterior cuyo objetivo es **optimizar el código para sostener
~3 000 usuarios concurrentes** (hoy el techo son ~200). La hoja de ruta para eso está en
la sección [9](#9-hoja-de-ruta-hacia-3-000-concurrentes).

Nada de lo que hay aquí es una prueba de carga. Las mediciones marcadas como tales lo
son; el resto son modelos de cuellos en serie, y están señalados.

---

## 1. Inventario de infraestructura

| Recurso | SKU | Grupo de recursos | Sirve a |
| --- | --- | --- | --- |
| Service Bus | Standard | `rg-zv-shared` | dev, stg, main |
| App Service Plan | Basic B3 Linux | (prod) | main |
| App Service Plan | Basic B1 Linux | (dev) | dev y stg |
| PostgreSQL Flexible Server | Burstable B1ms · 64 GiB · 240 IOPS | (dev) | **las tres bases** |
| Static Web Apps | Standard | — | main |
| Static Web Apps | Free | — | dev y stg |
| Blob Storage | GPv2 Hot LRS | — | fotos y tarjetas |
| Communication Services | Email | — | envío desde el dominio |

Dos hechos estructurales que condicionan todo lo demás:

1. **Un solo servidor PostgreSQL aloja las tres bases de datos.** `max_connections` es una
   propiedad del *servidor*, no de cada base. Los tres entornos comparten el mismo cupo.
2. **Un solo namespace de Service Bus sirve a los tres entornos**, con una cola por
   entorno. Decisión consciente, documentada en la sección 3.

---

## 2. Service Bus · configuración tal como quedó montada

### 2.1 Namespace

| Campo | Valor |
| --- | --- |
| Nombre | `sb-zv-shared` |
| Grupo de recursos | `rg-zv-shared` |
| Región | East US 2 |
| Plan de tarifa | **Standard** |
| Availability Zones | Enabled (automático, sin coste extra, todos los tiers) |
| Connectivity method | Public access (único posible en Standard) |
| Network Protocol | IPv4 only |
| Minimum TLS version | 1.2 |
| **Local Authentication** | **Enabled** |

> **`Local Authentication: Enabled` es obligatorio y no debe tocarse.** El código se
> autentica con cadena SAS (`ServiceBusClient.from_connection_string`,
> `app/services/service_bus.py:121`). Ponerlo en `Disabled` obliga a Microsoft Entra ID y
> **rompe en silencio**: `build_publisher` captura la excepción, devuelve un
> `MockPublisher` y los tres entornos vuelven al modo síncrono sin un solo error visible.
> Desactivarlo exige cambiar código antes.

### 2.2 Las tres colas

Una por entorno: `letters-dev`, `letters-stg`, `letters-main`. Ajustes idénticos en las tres:

| Ajuste | Valor | Modificable después |
| --- | --- | --- |
| Max queue size | 1 GB | sí |
| **Lock duration** | **5 minutes** | sí |
| Max delivery count | 10 | sí |
| Message time to live | 14 days | sí |
| Dead lettering on message expiration | ✅ activado | sí |
| **Enable duplicate detection** | ✅ **activado** | **NO** |
| Duplicate detection window | 10 minutes | sí |
| **Enable sessions** | ❌ desactivado | **NO** |
| **Enable partitioning** | ❌ desactivado | **NO** |
| Forward messages to | Disabled | sí |

Razones de los cuatro valores que no son el default:

- **Lock duration 5 min** (el máximo que permite Azure). El default de 1 minuto no alcanza:
  el worker recibe lotes de 12 con `prefetch_count=12`, los procesa con un semáforo de 5, y
  cada mensaje hace INSERT + mover 6 fotos en Blob + **envío SMTP** (`MAIL_TIMEOUT=10`).
  El worker **no renueva el bloqueo** — no usa `AutoLockRenewer`. Si el lock vence a mitad
  del lote, Service Bus reentrega, sube `delivery_count`, y aparecen correos duplicados o
  cartas en la DLQ.
- **Duplicate detection activado.** Es de lo que depende `message_id = f"letter-{purchaseId}"`
  (`app/services/letters.py:167`) para que un doble clic del comprador no genere dos cartas
  de la misma compra. **Solo se puede activar al crear la cola**; el tier Basic no lo soporta,
  y por eso el namespace es Standard.
- **Sessions desactivado.** El código no manda `session_id`. Activarlo hace que Service Bus
  rechace *todos* los mensajes con el motivo `Session ID is null` y los mande a la DLQ.
- **Partitioning desactivado.** Con particiones la deduplicación pasa a usar
  `MessageId + PartitionKey`, y el `message_id` deja de bastar sin que nada avise.

### 2.3 Directivas de acceso

Una directiva SAS **por cola** (no del namespace), llamada `api-worker`, con **Send + Listen**
y sin `Manage`.

La cadena resultante incluye `EntityPath=<nombre de la cola>`, y ahí está el aislamiento real
entre entornos. El SDK lo valida
(`.venv/Lib/site-packages/azure/servicebus/aio/_servicebus_client_async.py:314`):

```python
if self._entity_name and queue_name != self._entity_name:
    raise ValueError("The queue name provided does not match the EntityPath in "
                     "the connection string used to construct the ServiceBusClient.")
```

Consecuencia útil: aunque alguien ponga `SERVICE_BUS_QUEUE_NAME=letters-main` en el App
Service de dev, **dev es incapaz de tocar la cola de producción**.

Consecuencia peligrosa: si `EntityPath` y `SERVICE_BUS_QUEUE_NAME` no coinciden, **no explota
de forma visible**. La API cae al camino síncrono en silencio y el worker entra en bucle de
reconexión (`El consumidor cayó`).

### 2.4 Pendiente: candado de borrado

No se pudo aplicar el `CanNotDelete` sobre el namespace. Crear candados exige
`Microsoft.Authorization/locks/*`, presente solo en los roles **Owner** y
**User Access Administrator**; el rol disponible es Contributor.

Opciones, en orden de preferencia:

1. Que alguien con Owner ejecute:
   ```bash
   az lock create --name no-borrar-cola-compartida --lock-type CanNotDelete \
     --resource-group rg-zv-shared --resource-name sb-zv-shared \
     --resource-type Microsoft.ServiceBus/namespaces \
     --notes "Sirve a dev, stg y main."
   ```
2. Asignar `User Access Administrator` **solo sobre `rg-zv-shared`**.
3. Alternativa que Contributor sí puede hacer: alerta del log de actividad sobre
   `Microsoft.ServiceBus/namespaces/delete`. Detección en vez de prevención.

Si el namespace se borrara: las variables siguen puestas, `service_bus_enabled` sigue
siendo `True`, la API intenta publicar, falla y **cae al camino síncrono**. Se pierde solo
lo que estuviera dentro de las colas en ese instante.

---

## 3. Por qué un namespace compartido y tres colas

El Well-Architected Framework recomienda **namespaces separados por entorno**:

> *Namespace-Level Isolation: Prevent cross-application failures by using separate Service
> Bus namespaces for different environments or workload components.*

Y deja la puerta abierta en el pilar de costos:

> *Implement Service Bus consolidation strategies for cost reduction: Share Service Bus
> resources across applications **when security and operational boundaries permit**.*

Se optó por compartir porque a este volumen el riesgo de *noisy neighbor* es teórico
(27 de 1 000 créditos/s en el pico) y el ahorro es de ~20 USD/mes. Las mitigaciones que
hacen aceptable la decisión son las directivas SAS por cola (sección 2.3) y las colas
separadas.

**Lo que nunca es aceptable es una cola compartida entre entornos.** El worker es un
*competing consumer*: si los tres apuntaran a la misma cola, un mensaje de producción
recogido por el worker de dev llega a `app/services/commerce.py:464`:

```python
user = await self.db.get(User, user_id)   # el userId de main no existe en dev
if user is None or not user.is_active:
    raise ApiError(404, "USER_UNAVAILABLE", ...)
```

`404` está en `PERMANENT_STATUS` (`worker.py:56`) → dead-letter inmediato. **No se escribe
nada en la base de dev** (la excepción ocurre antes de `letters.create`), pero el mensaje se
destruye y el entorno correcto nunca lo ve: el comprador pagó y no recibe nada.

**Un mensaje nunca cambia de cola.** El único mecanismo que lo permitiría es el
auto-forwarding, y está en `Disabled`. El aislamiento es estructural, no estadístico: no
depende de que haya poco tráfico.

---

## 4. Variables de entorno por entorno

Valores efectivos tras este montaje. Los secretos no se transcriben.

### Comunes a los tres

```
SERVICE_BUS_MAX_BATCH    = 12
SERVICE_BUS_MAX_WAIT     = 5
SERVICE_BUS_MAX_ATTEMPTS = 5
SERVICE_BUS_SEND_TIMEOUT = 3.0   (default del código)
APP_REPLICAS             = 1
```

### Por entorno

| Variable | dev | stg | main |
| --- | --- | --- | --- |
| `SERVICE_BUS_QUEUE_NAME` | `letters-dev` | `letters-stg` | `letters-main` |
| `AZURE_SERVICE_BUS_CONNECTION_STRING` | SAS de su cola | SAS de su cola | SAS de su cola |
| `WEB_CONCURRENCY` | 1 | 1 | 1 |
| `DB_POOL_SIZE` + `DB_MAX_OVERFLOW` | 2 + 0 | 2 + 0 | 6 (4+2 o 6+0) |
| `WORKER_DB_POOL_SIZE` | 2 | 2 | 5 |
| `DB_RESERVED_CONNECTIONS` | 2 | 2 | 2 |
| `DB_CONNECTION_BUDGET` | 8 | 8 | 14 |
| **Conexiones reales** | **4** | **4** | **11** |

`WEB_CONCURRENCY` en dev bajó de 3 a 1: tres procesos uvicorn sobre el vCPU único del plan
B1 compartido con stg no dan concurrencia real, y cada proceso carga su propia copia de
FastAPI, SQLAlchemy y la pila de render del QR (~150 MB). Con el worker añadido eran cinco
procesos en 1.75 GB.

---

## 5. El presupuesto de conexiones

### 5.1 La fórmula, que es código propio

`app/core/config.py:322-333` — no viene de ninguna documentación externa:

```python
api = self.web_concurrency * self.app_replicas * (self.db_pool_size + self.db_max_overflow)
worker = self.worker_db_pool_size if self.service_bus_enabled else 0
used = api + worker
if used + self.db_reserved_connections > self.db_connection_budget:
    raise ValueError(...)   # → ConfigurationError → el contenedor no arranca
```

Se multiplica porque **cada proceso de uvicorn crea su propio pool**: el *lifespan*
(`app/main.py:35`) llama a `create_engine(settings)` y corre una vez por proceso. Los pools
no se comparten entre procesos.

```
api = APP_REPLICAS  x  WEB_CONCURRENCY  x  (DB_POOL_SIZE + DB_MAX_OVERFLOW)
      cuántas           cuántos procesos    cuántas conexiones
      máquinas          por máquina         por proceso
```

`DB_RESERVED_CONNECTIONS` no son conexiones que alguien abra: es un margen reservado en papel
dentro del propio presupuesto, para `alembic upgrade head` del pipeline de CD, `psql` y pgAdmin.

### 5.2 El techo real y su reparto

| | |
| --- | --- |
| `max_connections` de la B1ms (2 GiB) | 50 |
| Reservadas por PostgreSQL (replicación y monitoreo) | 15 |
| **Disponibles para la aplicación** | **35** |
| PgBouncer | **no disponible en tiers burstable** |

> `DB_CONNECTION_BUDGET` es **una promesa que cada entorno se hace a sí mismo**. Ningún
> entorno sabe que los otros dos existen; no hay coordinación. Nada impide que los tres
> declaren 20 y entre los tres pidan 60 de un servidor que tiene 35. **La suma la vigilan
> las personas, no el código.**

Reparto vigente: `8 + 8 + 14 = 30 ≤ 35`, con 19 conexiones reales en uso y 5 libres para
operación. **Regla para el equipo: la suma de los tres `DB_CONNECTION_BUDGET` no puede
pasar de 30.**

### 5.3 Hueco conocido en la fórmula

`worker` se suma **una sola vez** y no se multiplica por `APP_REPLICAS`. Con el worker dentro
del mismo contenedor, `APP_REPLICAS=2` produce dos workers reales (10 conexiones) y la
validación solo cuenta 5. Inocuo con `APP_REPLICAS=1`; hay que arreglarlo antes de escalar
main horizontalmente.

---

## 6. Costos mensuales · East US 2

Precios obtenidos el 2026-09-11 de la API pública de precios minoristas de Azure
(`https://prices.azure.com/api/retail/prices`, filtrando `armRegionName eq 'eastus2'`),
tarifa retail sin descuentos. Base de cálculo: 730 h/mes.

### 6.1 Precios unitarios

| Servicio | Medidor | Precio |
| --- | --- | --- |
| Service Bus Standard | Standard Base Unit | 10.00 /mes (12.5 M operaciones incluidas) |
| Service Bus Standard | Messaging Operations | 0.80 /millón por encima de 12.5 M |
| Service Bus Basic | Messaging Operations | 0.05 /millón · **sin cargo base** |
| Service Bus Premium | Messaging Unit | 0.9275 /hora = **677.08 /mes** |
| App Service Basic Linux | B1 / B2 / B3 | 0.017 / 0.034 / 0.067 por hora |
| PostgreSQL Flexible | B1MS | 0.017 /hora |
| PostgreSQL Flexible | B2S | 0.068 /hora |
| PostgreSQL Flexible | Storage | 0.115 /GB/mes |
| PostgreSQL Flexible | Backup LRS | 0.095 /GB/mes (gratis hasta el tamaño aprovisionado) |
| PostgreSQL Flexible | Premium SSD v2 IOPS | **0.02 /IOPS aprovisionado/mes** |
| Blob Hot LRS | Data Stored | 0.0184 /GB/mes |
| Blob Hot LRS | Write / Read Operations | 0.05 / 0.004 por 10 K |
| Static Web Apps | Standard App | 9.00 /mes (100 GB de tránsito incluidos) |
| Static Web Apps | Bandwidth | 0.20 /GB por encima de 100 GB |
| Communication Services | Email · envío | 0.00025 por correo |
| Communication Services | Email · datos | 0.00012 /MB |

### 6.2 Factura mensual

| Partida | Mes normal (500 cartas) | Mes pico (8 000 cartas) |
| --- | --- | --- |
| Service Bus Standard · 1 namespace, 3 colas | 10.00 | 10.00 |
| App Service Plan B3 Linux · main | 48.91 | 48.91 |
| App Service Plan B1 Linux · dev + stg | 12.41 | 12.41 |
| PostgreSQL B1ms · cómputo | 12.41 | 12.41 |
| PostgreSQL · 64 GiB de disco | 7.36 | 7.36 |
| PostgreSQL · backup (dentro de la cuota) | 0.00 | 0.00 |
| Blob Storage Hot LRS | 0.09 | 3.15 |
| Static Web Apps Standard · main | 9.00 | 9.00 |
| Static Web Apps Free · dev + stg | 0.00 | 0.00 |
| Communication Services · Email | 0.20 | 3.15 |
| **Total** | **100.38** | **106.39** |

**Entre el mes más tranquilo y el mejor mes de ventas hay 6 USD de diferencia.** El 94 % de la
factura es cómputo encendido las 24 horas. Optimizar el consumo no ahorra nada: la única
palanca es el tamaño de las instancias.

### 6.3 Qué se puede bajar

| Partida | Decisión | Ahorro /mes |
| --- | --- | --- |
| ASP main B3 → **B2** (2 vCPU / 3.5 GB) | **bajar** · con `WEB_CONCURRENCY=1` sobra | −24.09 |
| ASP main B3 → B1 | no · 1.75 GB con uvicorn + worker + render PDF es apurado | −36.50 |
| Service Bus Standard → Basic | **no** · se pierde la detección de duplicados | −9.99 |
| SWA main Standard → Free | no · sin SLA ni registros de autenticación propios | −9.00 |
| PostgreSQL B1ms | piso · es el SKU más pequeño, y el disco no se puede encoger | 0.00 |
| Blob y Email | irrelevante · juntos no llegan a 7 USD ni en el pico | 0.00 |

Con B3 → B2: **82.30 USD/mes** en el mes pico.

### 6.4 Nota sobre Static Web Apps

**No existe un plan «Production».** Los planes son **Free** y **Standard**; el plan Dedicated
se retiró el 31 de octubre de 2025. La confusión viene de que la tabla de Microsoft rotula la
columna Standard como *«For production apps»*. Lo que sí es un concepto aparte son los
**entornos** dentro de una misma SWA: producción más varios de staging creados a partir de
los pull requests. Free da 3 entornos de staging y 2 dominios personalizados; Standard da 10 y 5.

### 6.5 Corrección al estimado de fotos

La estimación de 293 GB partía de «5 fotos de hasta 6 GB cada una». La configuración real
(`MAX_PHOTOS_PER_LETTER=6`, `MAX_PHOTO_BYTES=3000000`) da **18 MB máximo por carta**:

```
Peor caso absoluto   8 000 x 18 MB = 144 GB  →  2.65 /mes
Escenario realista   8 000 x  8 MB =  64 GB  →  1.18 /mes
Estimación original                  293 GB  →  5.39 /mes
```

Irrelevante para las decisiones (2.5 USD de diferencia), pero el número a vigilar es otro: el
contenedor efímero del *eager upload* guarda una copia temporal antes de moverla al
permanente. **Si esa limpieza fallara, el consumo se duplicaría** — y ahí 293 GB deja de ser
una sobreestimación y pasa a ser una alerta.

---

## 7. El flujo real, transacción por transacción

Lo que hace el código hoy, no el modelo de `Iops.md`. Escrituras = transacciones que hacen
`commit`; son las que cuestan IOPS.

| Etapa | Qué hace en el código | Escrituras | Lecturas |
| --- | --- | --- | --- |
| 1 · Registro | `auth.register` — usuario, documento y consentimiento en **una** transacción, más `new_session` | 2 | 1 |
| 2 · Crear compra | `purchases.create_intent` — `pg_insert(Purchase)`; antes llama a Mercado Pago por HTTP | 1 | 2 |
| 3 · Pago en Mercado Pago | fuera de la infraestructura propia | 0 | 0 |
| 4 · Verificar pago | `apply_snapshot` — `SELECT … FOR UPDATE`, alta del pago, compra a `paid` | 1 | 7 |
| 4b · Webhook | `webhooks.handle` — registra el evento y reaplica el estado; corre en paralelo al 4 | 1–2 | 2 |
| 5 · Subir 6 fotos | `letters.eager_upload` — **no recibe `db`**; van directas a Blob | **0** | 6 |
| 6 · `POST /letters` | `letters.enqueue` — valida y publica; responde **202 sin escribir** | **0** | 2 |
| 7 · Worker | carta, 6 fotos en un lote, publicar, alta de entrega, marcar enviada | 5 | 4 |
| **Total por usuario** | | **10–11** | **~24** |

Dos aciertos de diseño que conviene no romper al optimizar:

- **Las seis fotos no cuestan ni una transacción.** `eager_upload` no tiene parámetro `db`.
- **`POST /letters` no escribe.** Dos `SELECT` por índice, publica, 202. De las 10–11
  escrituras del recorrido, la cola saca 5 del camino de la petición.

`verify` y el webhook compiten por la misma compra; `lock_purchase` (`FOR UPDATE`) los
serializa a nivel de fila, y el *rank* de estados evita que un webhook antiguo revierta el
estado vigente. Como cada candado es sobre una fila distinta, no hay contención entre
compradores.

---

## 8. Análisis de capacidad · 8 000 usuarios en un minuto

### 8.1 Los cuatro cuellos, en orden de quién se rompe primero

| # | Límite | De dónde sale | Techo |
| --- | --- | --- | --- |
| 1 | **Hash de contraseña** | `CapacityLimiter(2)` en `app/main.py:36`. Argon2id **medido en 45 ms** (`PasswordHash.recommended()` de pwdlib, mediana de 5); en un vCPU de App Service se asumen 80–120 ms | **~20 /s** |
| 2 | **Conexiones retenidas** | 6 conexiones de API, cada una ocupada ~300 ms durante las llamadas HTTP a Mercado Pago | ~20 /s |
| 3 | Disco de la base | 240 IOPS ÷ 2–3 operaciones de disco por transacción pequeña *(estimación)* | ~100 tx/s |
| 4 | Worker de la cola | semáforo de 5; cada carta paga 6 movimientos en Blob más un envío SMTP | ~5 cartas/s |

**No** son cuellos: Service Bus (27 de 1 000 créditos/s en el pico) ni las 35 conexiones de la
B1ms (main abre 11). Ninguno de los dos es el problema.

### 8.2 El patrón de fondo · una conexión retenida por algo que no es la base

La sesión de SQLAlchemy toma una conexión en la primera consulta y no la suelta hasta el
`commit`. Si entre medias ocurre algo lento ajeno a PostgreSQL, la conexión se queda ocupada
sin hacer nada:

| Dónde | Qué se retiene esperando | Coste |
| --- | --- | --- |
| `app/services/auth.py:31-36` | `by_email` toma conexión → **Argon2id** → `flush` | 45–120 ms |
| `purchases.create_intent` | `purchase_by_idempotency` → **`create_preference` HTTP a MP** → `insert` | ~300 ms |
| `commerce.verify_purchase` | `_owned_purchase` → **`fetch_payment` HTTP a MP** → `apply_snapshot` | ~300 ms |

Una conexión que podría despachar 80 transacciones por segundo despacha 3. **El disco, que
aguanta 100 tx/s, trabaja al 20 % de su capacidad esperando a Mercado Pago.**

Un detalle que sí está bien resuelto: en `purchases.verify` la llamada HTTP ocurre *antes* del
`SELECT … FOR UPDATE`, no dentro. Si fuera al revés, cada comprador mantendría un candado de
fila abierto durante 300 ms de red.

### 8.3 Dónde falla y en qué segundo

```
Llegada exigida   133 registros/s   (8 000 / 60 s)
Atención          ~20 registros/s   (cuello 1)
Acumulación       113/s

t = 1 s   →   113 en espera   →   espera de  5.6 s   →  aún responde
t = 2 s   →   226 en espera   →   espera de 11.3 s   →  500 TimeoutError
```

**Se cae a los ~2 segundos, en `/auth/register`, por `DB_POOL_TIMEOUT=10`.** No es la base de
datos ni Service Bus: es el hash, agravado por la conexión retenida.

Lo que **no** se pierde: los webhooks que fallen los reintenta Mercado Pago, y toda carta que
alcanzó su 202 acaba enviada. Se pierden compradores que nunca llegaron a registrarse.

### 8.4 Tiempo hasta el correo del usuario 8 000

Modelo de cuellos en serie, suponiendo clientes infinitamente pacientes que reintentan sin
rendirse. Mejor caso posible, no realista.

| Etapa | Tiempo | Naturaleza |
| --- | --- | --- |
| 1 · Registro | 6 m 40 s | cuello de CPU (hash) |
| 2 · Crear compra | 6 m 40 s | conexión retenida por HTTP externo |
| 4 · Verificar pago | 6 m 40 s | conexión retenida por HTTP externo |
| 6 · `POST /letters` | 2 m 13 s | publicación en la cola |
| 7 · Worker vacía la cola | 26 m 40 s | **asíncrono · nadie espera en una petición** |
| **Total** | **~49 min** | |

Los 27 minutos del worker son el 55 % del total y son la **única parte sana**: esos usuarios ya
recibieron su 202 y su carta está garantizada en una cola durable. Los otros 22 minutos sí son
gente esperando frente a una pantalla.

### 8.5 Capacidad real

| | |
| --- | --- |
| Concurrentes activos antes de los primeros 500 | **~200** |
| Tasa sostenida de recorridos completos | **~1 200 /minuto** |
| Escenario real (8 000 cartas en 15 días) | 0.006 /s — tres órdenes de magnitud por debajo |

La respuesta a «miles o cientos» es **cientos**.

### 8.6 Correcciones a `Iops.md`

| Lo que dice el documento | Lo que es |
| --- | --- |
| 7 IOPs por recorrido completo | **10–11 transacciones de escritura**, y cada una cuesta 2–3 operaciones de disco. El coste real ronda 25–30 IOPs: el modelo es optimista por un factor de 3 a 4 |
| «20 conexiones, cada una procesando de a 12 peticiones, para trabajar a la par con los 240 IOPS» | Conexiones e IOPS son dimensiones independientes; `20 × 12 = 240` coincide por casualidad. Para saturar 240 IOPS con escrituras pequeñas bastan **2 o 3 conexiones** |
| «para evitar context switching ya que tiene 1 vCore» | **Correcto, y es el razonamiento válido del documento.** El 20 estaba bien justificado — por esto, no por los IOPS |
| «universo máximo de concurrencia: 8 190 usuarios (límite del load balancer)» | No se pudo verificar el origen de la cifra. Es irrelevante: el límite real está 40 veces más abajo, en el hash |
| IOP #7: «genera QR, consulta la carta y envía el correo» | Son **3 transacciones**, no 1: publicar la carta, dar de alta la entrega, y marcarla enviada tras el SMTP |

---

## 9. Hoja de ruta hacia 3 000 concurrentes

Objetivo de la sesión siguiente. Hoy el techo son ~200 concurrentes activos; hacen falta ~15×.

### 9.1 El reencuadre que hay que hacer primero

**3 000 usuarios «concurrentes» repartidos por un recorrido realista ya caben en el disco
actual.** Si cada usuario consume 5.5 transacciones de escritura sincrónicas a lo largo de un
recorrido de ~5 minutos:

```
3 000 x 5.5 / 300 s = 55 transacciones de escritura/s   <   ~100 tx/s disponibles
```

Lo que **no** cabe es que 3 000 lleguen en el mismo segundo. Conviene decidir con el negocio
cuál de los dos escenarios hay que soportar, porque las soluciones son distintas: el primero
es casi gratis, el segundo exige cambiar código y subir de SKU.

### 9.2 Cambios de código, por rentabilidad

1. **No retener la conexión durante el hash ni durante las llamadas a Mercado Pago.**
   El de mayor impacto de toda la lista. Liberar la sesión antes del trabajo lento y volver a
   tomarla después (o mover el hash fuera del alcance de la sesión) sube el techo de 20/s en
   las tres rutas más calientes y deja al disco trabajar a su capacidad real. Sitios exactos
   en la sección 8.2.
2. **`CapacityLimiter(2)` → 4 en main.** El límite se puso pensando en el núcleo único de la
   B1ms, pero **el hash corre en el App Service**, no en la base, y main es un B3 con 4 vCPU.
   Duplica el techo de registro sin tocar nada más. Verificar la memoria: Argon2 con los
   parámetros recomendados usa 64 MiB por hash.
3. **`google_limiter = CapacityLimiter(1)`.** Con la caché de certificados de `CacheControl`
   una verificación cuesta ~10 ms, así que da ~100/s. Pero al estar serializado a 1, un fallo
   de caché con Google lento (timeout de 5 s) detiene **toda** la ruta de Google. Subirlo a
   2–4 y revisar el comportamiento en fallo de caché.
4. **`DB_POOL_TIMEOUT` 10 → 30 en main.** No añade capacidad; convierte errores 500 en
   esperas. Bajo ráfaga, un comprador que espera 20 s y completa vale más que uno que ve un
   error a los 10.
5. **Ajustar los parámetros de Argon2** solo si 1–3 no alcanzan. Bajar `memory_cost` de 64 a
   32 MiB y `time_cost` de 3 a 2 sigue siendo seguro y recorta el hash a ~20 ms. Es una
   decisión de seguridad, no solo de rendimiento: documentarla.
6. **Arreglar el hueco de `APP_REPLICAS` en la fórmula** (sección 5.3) antes de escalar main
   horizontalmente.

### 9.3 Cambios de infraestructura

| Cambio | Efecto | Coste /mes |
| --- | --- | --- |
| **PostgreSQL B1ms → B2s** (2 vCore / 4 GiB) | `max_connections` **50 → 429** (414 de usuario). Desaparece el techo de 35 y el problema de context switching del núcleo único | **+37.23** |
| **Disco: más IOPS** | Premium SSD v2 a 0.02 /IOPS/mes. 3 000 IOPS ≈ 60 /mes. Solo tiene sentido **después** de 9.2.1: hoy el disco está al 20 % esperando a Mercado Pago | +60 aprox. |
| ASP main B3 → B2 | ahorro, no capacidad. Revisar **después** de subir el `CapacityLimiter`, porque el hash consume CPU del App Service | −24.09 |
| Más conexiones sin subir de SKU | **ninguno.** Para saturar 240 IOPS bastan 2–3 conexiones; main ya tiene 11 | 0 |

El orden importa: **primero 9.2.1 y 9.2.2, que son gratis**, y medir. Pagar más IOPS o más
vCores antes de quitar las conexiones retenidas es comprar capacidad que el código no puede
usar.

### 9.4 Qué medir para tener números firmes

Todo lo de la sección 8 es un modelo de cuellos en serie, no una prueba de carga. Antes de
decidir gastos conviene:

- Lanzar la ráfaga contra **stg** (mismo código, SKU distinto: anotar el sesgo) y medir el
  percentil 95 por endpoint.
- Medir el coste real en operaciones de disco por transacción con `pg_stat_bgwriter` y
  `pg_stat_wal`, en vez de la estimación de 2–3.
- Medir Argon2 **en el App Service**, no en la máquina de desarrollo. El 45 ms de aquí es un
  suelo optimista.
- Vigilar `Throttled Requests` de Service Bus y el `Active message count` de la cola durante
  la ráfaga, para confirmar que la cola no es el cuello.

---

## 10. Fuentes

- [Limits in Azure Database for PostgreSQL flexible server](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-limits) — `max_connections` por SKU, 15 conexiones reservadas, PgBouncer no disponible en burstable
- [Azure Service Bus throttling](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-throttling) — 1 000 créditos/s por namespace en Standard
- [Service Bus quotas and limits](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-quotas) — 256 KB por mensaje, 1 000 operaciones/s
- [Message transfers, locks and settlement](https://learn.microsoft.com/en-us/azure/service-bus-messaging/message-transfers-locks-settlement) — lock por defecto 1 min, máximo 5 min
- [Service Bus dead-letter queues](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-dead-letter-queues) — `MaxDeliveryCount` por defecto 10, ruta `<queue>/$deadletterqueue`
- [Duplicate detection](https://learn.microsoft.com/en-us/azure/service-bus-messaging/duplicate-detection) — no disponible en Basic; ventana por defecto 10 min
- [Reliability in Azure Service Bus](https://learn.microsoft.com/en-us/azure/reliability/reliability-service-bus) — zone redundancy automática y gratuita en todos los tiers
- [Architecture Best Practices for Azure Service Bus (WAF)](https://learn.microsoft.com/en-us/azure/well-architected/service-guides/azure-service-bus) — namespaces separados por entorno; consolidación por costo
- [Service Bus considerations for multitenancy](https://learn.microsoft.com/en-us/azure/architecture/guide/multitenant/service/service-bus) — modelos de aislamiento y *noisy neighbor*
- [Azure Static Web Apps hosting plans](https://learn.microsoft.com/en-us/azure/static-web-apps/plans) — solo Free y Standard; Dedicated retirado el 2025-10-31
- [Lock your Azure resources](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/lock-resources) — los candados exigen Owner o User Access Administrator
- [API de precios minoristas de Azure](https://prices.azure.com/api/retail/prices) — todos los precios de la sección 6
