# Bitácora · el webhook de Mercado Pago no entrega notificaciones de pago

**Última sesión:** 2026-09-11 / 12 (madrugada)
**Estado:** 🔴 **abierto, sin resolver**

Documento de traspaso. Recoge qué se probó, qué quedó descartado **con evidencia**, y qué
queda por intentar. Escrito para que la siguiente sesión no repita el camino.

---

## 1. El problema en una frase

**Mercado Pago nunca ha entregado una sola notificación de tipo `payment` al backend.**
Todas las notificaciones registradas son `topic_merchant_order_wh`, y todas fallan con 405.

### Por qué importa

`apply_snapshot` (`app/services/purchases.py`) es el único sitio que escribe en `payments`,
y se llama desde dos caminos:

| Camino | Lo dispara | Depende del navegador |
| --- | --- | --- |
| `POST /api/v1/purchases/{id}/verify` | el navegador del comprador | **sí** |
| `POST /api/v1/webhooks/mercadopago` | Mercado Pago, servidor a servidor | no |

Con el segundo roto, **cualquier fallo del navegador pierde el pago en silencio**: el dinero
sale de la cuenta del comprador y el sistema no se entera. Reproducido hoy tres veces: pagar
y cerrar la ventana antes del redirect deja la compra en `pending` y `payments` vacío.

---

## 2. Lo que SÍ funciona, verificado

| | Evidencia |
| --- | --- |
| El endpoint existe y responde | Simulación desde el panel → **200 OK** |
| La validación de firma funciona | La simulación guardó una fila en `payment_events`. Para eso el HMAC tuvo que cuadrar |
| **`MERCADOPAGO_WEBHOOK_SECRET` es correcto** | Se deduce de lo anterior: sin el secreto correcto la firma no valida |
| El filtro de acciones funciona | La fila quedó con `applied = false` porque `test.created` no empieza por `payment`. Correcto |
| La idempotencia funciona | Simulaciones repetidas → un solo registro. Correcto |
| El arreglo del `data.id` | PR #22, mergeado a develop en `cc1d752` |
| Service Bus, worker y arranque supervisado | PR #21, mergeado. Verificado en dev de punta a punta |

---

## 3. Hipótesis descartadas, con su evidencia

**No volver a investigarlas.**

| Hipótesis | Descartada porque… |
| --- | --- |
| La URL del webhook está mal | Se corrigió a `/api/v1/webhooks/mercadopago` y el panel la muestra así. La simulación llega |
| El secreto del webhook es incorrecto | La simulación validó la firma. Ver §2 |
| `MERCADOPAGO_WEBHOOK_TOKEN` duplicada rompe algo | No existe en el código. `config.py:152` usa `extra="ignore"`, Pydantic la descarta |
| Falta la `PUBLIC_KEY` en el backend | El backend no la usa ni debe usarla: es del frontend, para inicializar el SDK en el navegador |
| Es el bug del `data.id` en la firma | Real y arreglado (PR #22), pero ese código **nunca se ejecuta** porque las notificaciones no llegan |
| Es un problema de CORS o del App Service | Los 405 los reporta Mercado Pago desde su panel, antes de tocar nada nuestro |
| Apagar IPN desbloquea el guardado de eventos | **Probado el 2026-09-11. No funcionó** |

---

## 4. El estado real de la configuración en Mercado Pago

Aplicación: **`zv-backend`**. Hay **dos mecanismos de notificación en paralelo**, y cada uno
tiene la mitad de lo que hace falta:

### Webhooks (moderno, firmado) — `Notificaciones → Webhooks`

| | |
| --- | --- |
| URL de prueba | `https://api-zv-dev-bebmfzctg9h2hsek.eastus2-01.azurewebsites.net/api/v1/webhooks/mercadopago` ✅ |
| Eventos **guardados** | `Vinculación de aplicaciones, Alertas de…` 🔴 **no incluye Pagos** |
| Notificaciones entregadas | **0 %** |
| Lo único que llega | `topic_merchant_order_wh`, acción `update`, **todas 405** |

🔴 **Marcar "Pagos (legacy)" y guardar NO persiste.** Se intentó varias veces, con recarga
previa, y el resumen sigue mostrando los eventos viejos.

### IPN (antiguo, sin firma validable) — `Notificaciones → IPN`

| | |
| --- | --- |
| Ámbito | **Nivel de cuenta**, aplica a todas las aplicaciones |
| URL | la misma del backend de dev, con la ruta correcta ✅ |
| Evento | **`Pagos (payments)`** ✅ ya seleccionado |
| Botón "Probar" | 🔴 **400 - Error** |

**Por qué falla la prueba de IPN.** Envía `?topic=payment&id=123456`, sin cuerpo JSON y sin
firma validable. El router lee `request.query_params.get("data.id")` → `None`, y
`verify_webhook` no encuentra cabecera de firma. **El backend no habla el dialecto IPN, y no
debería: IPN no permite validar el origen.**

### El resumen de la contradicción

```
Webhooks  →  habla el idioma correcto  →  pero NO tiene el evento de Pagos
IPN       →  tiene el evento de Pagos  →  pero habla un idioma que el backend no entiende
```

---

## 5. Evidencia recogida (para no volver a pedirla)

- **Panel de notificaciones**, filtro `Ambiente: Prueba`, período Hoy: 8 filas, todas
  `topic_merchant_order_wh` / `update` / **405 - Fallida**, entre las 03:56 y las 22:19 UTC
  del 11/09. Ninguna de tipo `payment`.
- **Cuerpo de una notificación fallida:** `{"type": "topic_merchant_order_wh", "action":
  "update", "id": "44401266446", "status": "closed", "live_mode": false, …}`. No trae id de
  pago.
- **Prueba de IPN:** `GET .../api/v1/webhooks/mercadopago?topic=payment&id=123456` → 400.
- **Simulador de Webhooks:** primera ejecución 200 y fila guardada; las siguientes también
  devuelven 200 pero **no guardan nada**.
- **Logs del backend, pagando y cerrando la ventana:** solo `POST /api/v1/purchases 201`.
  Ni `/verify`, ni webhook. `payments` vacío, sin carta en "Mis dedicatorias".

---

## 6. Trampas descubiertas · no volver a caer

**El simulador solo sirve una vez.** Manda siempre `id: "123456"`, y `webhooks.handle`
descarta por `event_id` repetido devolviendo `"duplicate"` con **HTTP 200**. Mercado Pago
muestra "¡Excelente!" y no ha pasado nada. Para reutilizarlo:

```sql
delete from payment_events where event_id = '123456';
```

Y aun así no valida el camino completo: al pasar el filtro llamaría a
`fetch_payment("123456")`, un pago que no existe en Mercado Pago, y devolvería 404.

**El Panel de notificaciones tiene filtro de `Ambiente`.** Por defecto puede venir en
`Productivo` mientras todas las pruebas son en `Prueba`. Una lista vacía puede significar
"filtro equivocado", no "no pasó nada".

**El formulario de Webhooks muestra estado sin guardar.** Una captura del formulario con
"Pagos" marcado no prueba que esté guardado. Hay que **recargar** y mirar el recuadro
`Eventos` del resumen.

**188 pruebas se omiten en local** por falta de PostgreSQL. "Verde en local" no es "verde en
CI": hoy una llamada con la firma antigua en `test_payments_lab.py` estaba omitida en local y
habría roto el pipeline. Antes de dar por bueno un cambio de firma, `grep` de todos los
llamadores.

---

## 7. Qué intentar en la próxima sesión

### Candidato 1 · `notification_url` en la preferencia con `source_news=webhooks` ⭐

**Es el que tiene más respaldo documental y el que resuelve más problemas a la vez.**

```
notification_url = {API_PUBLIC_URL}/api/v1/webhooks/mercadopago?source_news=webhooks
```

Lo que dice la documentación:

> *To receive **exclusively Webhooks and not IPN**, you should add the parameter
> `source_news=webhooks` to the `notification_url`.*

> *The URLs configured during the creation of a payment **will take precedence over** those
> configured through Your integrations.*

> *IPN notifications are set at the **account level** and apply to all linked applications,
> whereas Webhooks allow you to configure a different URL for **each application**.*

Qué resuelve:

| | |
| --- | --- |
| El panel que no guarda | Lo esquiva: la URL de la preferencia tiene precedencia |
| El dialecto equivocado | `source_news=webhooks` fuerza el formato moderno, firmado, con `data.id` |
| IPN pisando la cuenta | Lo excluye explícitamente |
| Tres entornos, dos ranuras | Cada entorno enruta desde su propia `API_PUBLIC_URL` |

**Compatibilidad comprobada con el código actual:** el router lee `data.id` de la query y un
parámetro extra no le molesta; el manifiesto de la firma usa `data.id`, `x-request-id` y
`ts`, que no se ven afectados. Los `merchant_order` que lleguen de propina los descarta el
filtro con `"ignored"` y 200, así que Mercado Pago deja de reintentarlos.

**Alcance:** `create_preference` en `app/services/payments.py` (~5 líneas) más sus pruebas
con la Rule of 10. Exige poner `API_PUBLIC_URL`, que hoy falta en los tres entornos y que
además hace que el QR del correo se cargue como imagen remota.

**Riesgo a verificar antes de escribir:** confirmar que con `source_news=webhooks` el cuerpo
y la firma llegan en formato moderno y no IPN. Si llegara en formato IPN, el endpoint lo
rechazaría igual que hoy.

### Candidato 2 · Aplicación nueva en Mercado Pago

`zv-backend` arrastra historia —el IPN configurado lo demuestra— y puede tener estado
heredado que impida guardar los eventos. Crear una aplicación limpia y configurar solo
Webhooks es barato de probar. Implicaría nuevas credenciales y nuevo secreto.

### Candidato 3 · Soporte de Mercado Pago

Con la evidencia de §5, que es concreta y reproducible: el formulario no persiste la
selección de eventos.

### Comprobación pendiente, barata

Verificar que `MERCADOPAGO_ACCESS_TOKEN` de dev sea el **de prueba de `zv-backend`** y no de
otra aplicación. El de prueba empieza por `TEST-`, el de producción por `APP_USR-`. Si fuera
de otra aplicación, la configuración de webhooks de `zv-backend` no aplicaría a esos pagos.

---

## 8. Estado del resto del plan

| | Tarea | Estado |
| --- | --- | --- |
| 1 | Configurar el webhook en Mercado Pago | ⚠️ hecho a medias · el evento no persiste |
| 1.5 | Arreglo de la firma `data.id` | ✅ PR #22 mergeado |
| 2 | **Verificar el webhook sin dinero** | 🔴 **bloqueado** · es lo de este documento |
| 3 | Dominios propios en main | ⏸️ pendiente |
| 4 | La compra real de 30 000 COP | ⏸️ **no hacer hasta cerrar el paso 2** |

### Otros pendientes abiertos

- **Alertas** de `Active Messages` y `Dead-lettered Messages` en las tres colas. Se crean en
  el **namespace** con `Split by dimensions → EntityName`, no en la cola.
- **Candado `CanNotDelete`** sobre el namespace. Bloqueado: exige rol Owner o User Access
  Administrator, y el disponible es Contributor.
- **`API_PUBLIC_URL`** no está definida en ningún entorno.
- **`MERCADOPAGO_WEBHOOK_TOKEN`**: variable muerta, se puede borrar de los tres App Services.
- **Topología de cookies:** el flujo de pago **no funciona en Brave** porque la cookie de
  sesión es de tercera parte. Se resuelve con los dominios del paso 3. Ver
  [`PENDIENTES_Y_IMPLICACIONES.md`](PENDIENTES_Y_IMPLICACIONES.md) §B1.

---

## 9. Documentos relacionados

- [`INFRAESTRUCTURA_Y_CAPACIDAD.md`](INFRAESTRUCTURA_Y_CAPACIDAD.md) — montaje de Service
  Bus, variables por entorno, costos de East US 2, reparto de las 35 conexiones, y el
  análisis del camino de escritura. La §9 traza la ruta hacia 3 000 concurrentes.
- [`PENDIENTES_Y_IMPLICACIONES.md`](PENDIENTES_Y_IMPLICACIONES.md) — 12 hallazgos de código
  pendientes y 11 implicaciones de las decisiones de infraestructura.
- [`plans/2026-09-11-service-bus-worker-entrypoint.md`](plans/2026-09-11-service-bus-worker-entrypoint.md)
  — el plan del arranque supervisado del worker.

## 10. Nota sobre herramientas

**Context7 falló con `CONNECT_TIMEOUT` en los tres intentos** de la sesión. No estuvo
disponible en ningún momento.

Lección de método más útil que esa: **las dos piezas que destrabaron el diagnóstico
—`source_news=webhooks` y que IPN es de nivel de cuenta— salieron de una búsqueda web
dirigida al síntoma**, no de leer documentación de referencia ni de inspeccionar capturas una
por una. Ante un problema de integración con un proveedor, buscar el síntoma exacto primero.
