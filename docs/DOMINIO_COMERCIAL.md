# Dominio comercial: compras, pagos, cartas, fotos, QR y correo

Implementa el flujo descrito en `Iops.md` (aporte del compañero, no modificado) sobre
la base de autenticación por sesión de la spec 001. Ninguna integración externa se
ejecutó con credenciales reales.

## 1. Reglas de negocio garantizadas

| Regla | Cómo se garantiza |
|---|---|
| Un usuario puede tener muchas compras | `purchases.user_id` sin unicidad |
| Una compra pagada habilita exactamente una carta | `letters.purchase_id` **UNIQUE** + `SELECT … FOR UPDATE` sobre la compra |
| Consultar o reenviar una carta no consume otra compra | Ningún camino de `letters`/`deliveries` crea compras; el reenvío solo inserta en `letter_deliveries` |
| El doble clic, varias pestañas o un reintento no crean una segunda carta | `INSERT … ON CONFLICT DO NOTHING` sobre `purchase_id`; la petición perdedora devuelve **200** con la misma carta |
| Retomar un borrador no crea otra carta | `POST /api/v1/letters` sobre una compra con carta en **borrador** sobrescribe esa fila con el contenido y las fotos nuevas (mismo `id`, mismo slug) y la publica; sobre una carta **publicada** la devuelve tal cual |
| Una compra repetida con la misma clave no se duplica | `uq_purchases_user_idempotency (user_id, idempotency_key)` + `ON CONFLICT DO NOTHING` |
| El pago se verifica en el servidor | `POST /purchases/{id}/verify` consulta al proveedor; el retorno del navegador sin `paymentId` deja la compra en `pending` |
| Un webhook repetido no se procesa dos veces | `uq_payment_events_provider_event (provider, event_id)` |
| Un webhook fuera de orden no revierte un pago | Rango monótono en `PAYMENT_STATUS_RANK`: un estado de rango menor se registra pero no se aplica |
| La cédula no es credencial ni identificador público | Tabla aparte, solo HMAC y últimos 4 dígitos; no aparece en `/me`, ni en el slug, ni en el visor público |

## 2. Estados separados

```
users            is_active / email_verified
purchases        pending → paid → (cancelled por reembolso/contracargo) ; expired
payments         pending | in_process → approved | rejected | cancelled → refunded | charged_back
letters          draft → published            (congelada al publicar)
letter_deliveries pending → sent | failed      (un intento por fila)
```

`purchases.status` es el estado **comercial**; `payments.status` es lo que reporta el
proveedor. Se guardan por separado a propósito: un pago rechazado no borra la compra, y
un reembolso posterior no borra la carta ya publicada.

## 3. Modelo de datos (migración `0002_commerce`)

- `user_identity_documents` — PK `user_id`, `document_hash` (HMAC-SHA256, UNIQUE),
  `document_last4`, `document_type`. El número en claro **no se persiste**.
- `purchases` — `external_reference` UNIQUE, `idempotency_key`,
  `uq_purchases_user_idempotency`, `expires_at`, `checkout_url`.
- `payments` — `uq_payments_provider_payment (provider, provider_payment_id)`.
- `payment_events` — `uq_payment_events_provider_event (provider, event_id)`, `applied`.
- `letters` — `purchase_id` **UNIQUE** (FK `ON DELETE RESTRICT`), `public_slug` UNIQUE
  (aleatorio, 16 bytes), `published_version`.
- `letter_photos` — `uq_letter_photos_position (letter_id, position)`.
- `letter_deliveries` — un intento por fila, con `attempts`, `letter_version` y `last_error`.

## 4. Flujo, mapeado a `Iops.md`

| IOP | Endpoint | Comportamiento |
|---|---|---|
| #1 registro | `POST /api/v1/auth/register` + `/auth/login` | Sesión opaca en cookie |
| #1 intención | `POST /api/v1/purchases` | 201 la primera vez, **200** si repite la clave de idempotencia; devuelve `checkoutUrl` si hay proveedor |
| #2 pago | Checkout de Mercado Pago (fuera de la API) | La API no ve la tarjeta ni datos de pago |
| #3 verificación | `POST /api/v1/purchases/{id}/verify` con `{"paymentId": …}` | Consulta al proveedor y aplica el estado; sin `paymentId` solo informa |
| #3 bis | `POST /api/v1/webhooks/mercadopago` | Firma HMAC obligatoria, idempotente, tolerante al desorden |
| #4 carta | `POST /api/v1/letters` | Exige compra propia y `paid`. Si ya hay carta: en borrador la sobrescribe con lo enviado y responde 200 (retomar desde "Mis dedicatorias"); publicada, la devuelve tal cual con 200 (409 en el camino con cola) |
| #5 antifraude | mismo endpoint | Bloqueo `FOR UPDATE` + UNIQUE: una compra pagada, una carta |
| #6 persistencia | `PATCH /letters/{id}`, `POST /letters/{id}/photos` | Solo mientras la carta sea borrador (`autoPublish=false`) |
| #7 entrega | `POST /api/v1/letters` (por defecto) o `POST /letters/{id}/publish` | Publica, genera slug y QR, y envía el correo dejando el estado en `letter_deliveries`. Con `recipientEmail` y `autoPublish` (por defecto `true`) ocurre en el mismo acto de crear la carta, tanto en el camino síncrono como en el worker |

## 5. Contratos principales

Todas las escrituras requieren cookie de sesión, cookie CSRF y cabecera
`X-CSRF-Token` (`GET /api/v1/auth/csrf`). El webhook es la única excepción: no procede
de un navegador y se autentica por firma HMAC.

```jsonc
// POST /api/v1/purchases            201 (nueva) | 200 (misma clave)
{ "idempotencyKey": "pm-1739912345678" }
// →
{ "id": "uuid", "status": "pending", "amountCents": 1500000, "currency": "COP",
  "externalReference": "zv-…", "checkoutUrl": "https://…|null", "hasLetter": false,
  "paidAt": null, "expiresAt": "…", "createdAt": "…" }

// POST /api/v1/purchases/{id}/verify  200
{ "paymentId": "1234567890" }          // opcional; sin él solo consulta el estado
// →
{ "purchase": { … }, "payment": { "status": "approved", "statusDetail": "accredited",
  "providerPaymentId": "1234567890", "verifiedAt": "…" }, "canCreateLetter": true }

// POST /api/v1/letters               201 (nueva) | 200 (ya existía) | 202 (encolada)
{ "purchaseId": "uuid", "title": "Para ti 💌", "recipientName": "Ana",
  "recipientEmail": "ana@example.com", "body": "línea 1\nlínea 2 🌹", "theme": "classic",
  "autoPublish": true }                 // opcional; con true (defecto) y recipientEmail,
                                        // la respuesta ya trae publicUrl, qrUrl y deliveries[]

// POST /api/v1/letters/{id}/photos   multipart: file, caption?, position?   201  (solo borrador)
// POST /api/v1/letters/{id}/publish  200  → publicUrl, qrUrl, publishedVersion, deliveries[]
//                                         (para cartas creadas con autoPublish=false)
// POST /api/v1/letters/{id}/deliveries  202  { "recipientEmail": "…"? }  ← reenvío

// GET /api/v1/me/dedications          200 — "Mis dedicatorias": una fila por compra pagada
[ { "purchaseId": "uuid", "letterId": "uuid|null", "state": "draft|published",
    "title": "…|null", "recipientName": "…|null", "theme": "…|null",
    "publicSlug": "…|null", "publicUrl": "…|null",
    "paidAt": "…", "publishedAt": "…|null", "updatedAt": "…" } ]
                                        // sin cuerpo, fotos ni entregas: el detalle sigue en
                                        // GET /api/v1/letters/{id}

// GET /api/v1/public/letters/{slug}   200 — sin usuario, sin correo del comprador, sin cédula
{ "letterId": "uuid", "publishedVersion": 1, "title": "…", "recipientName": "…",
  "body": "…", "theme": "classic", "photos": [ { "position": 0, "caption": "…", "url": "…" } ],
  "publishedAt": "…" }
```

Errores: `{ "code", "message", "fieldErrors", "requestId" }`.

| Código | HTTP | Cuándo |
|---|---|---|
| `PURCHASE_NOT_FOUND` | 404 | La compra no es del usuario de la sesión |
| `PURCHASE_NOT_PAID` | 409 | Se intenta crear una carta sin pago confirmado |
| `PAYMENT_MISMATCH` | 409 | El pago pertenece a otra compra |
| `PAYMENT_AMOUNT_MISMATCH` | 409 | El monto o la moneda no coinciden |
| `PAYMENTS_NOT_CONFIGURED` | 503 | `PAYMENT_PROVIDER=none` |
| `PAYMENTS_UNAVAILABLE` | 503 | El proveedor no respondió |
| `WEBHOOK_SIGNATURE_INVALID` | 401 | Firma HMAC ausente o incorrecta |
| `LETTER_NOT_FOUND` | 404 | La carta no es del usuario |
| `LETTER_FROZEN` | 409 | Se intenta editar, publicar de nuevo o cambiar fotos de una carta publicada |
| `LETTER_NOT_PUBLISHED` | 409 | Se intenta enviar o pedir QR de un borrador |
| `LETTER_ALREADY_EXISTS` | 409 | Camino con cola: la compra ya tiene una carta **publicada**. Un borrador sí se encola y el worker lo sobrescribe |
| `RECIPIENT_EMAIL_REQUIRED` | 422 | Falta el correo de destino al publicar |
| `PHOTO_TOO_LARGE` / `UNSUPPORTED_MEDIA` | 413 / 415 | Supera `MAX_PHOTO_BYTES`; no es JPEG/PNG/WebP |
| `PHOTO_LIMIT_REACHED` / `PHOTO_POSITION_TAKEN` | 409 | Máximo de fotos; posición ocupada |
| `DOCUMENT_IN_USE` | 409 | La cédula ya está registrada en otra cuenta |

## 6. Límites de validación

- Título y nombre del destinatario: 1–120 caracteres, no en blanco.
- Cuerpo: 1–5000 caracteres. Se aceptan **emojis, acentos y saltos de línea**; los
  `\r\n` se normalizan a `\n` y se rechazan caracteres de control.
- Tema: `[a-z0-9-]`, hasta 32 caracteres.
- Clave de idempotencia: 8–64 caracteres `[A-Za-z0-9._-]`.
- Cédula: 5–20 caracteres alfanuméricos; tipos `CC`, `CE`, `PA`, `NIT`.
- Fotos: JPEG, PNG o WebP verificados **por firma binaria**, no por el `Content-Type`
  que declara el cliente. Hasta `MAX_PHOTOS_PER_LETTER` por carta, orden por `position`.
- Webhook: cuerpo máximo de 64 kB.

## 7. Decisión: el contenido se congela al publicar

`FREEZE_LETTER_AFTER_PUBLISH=true` (recomendación adoptada). Publicada la carta, no se
puede editar el texto ni cambiar las fotos: el enlace que ya recibió el destinatario
sigue mostrando lo mismo que se anunció por correo, y el QR impreso no queda obsoleto.

Lo que sí se permite después de publicar: consultarla, reenviarla y enviarla a otra
dirección. Cada envío queda registrado con la `letterVersion` que entregó.

`published_version` existe para el caso en que negocio decida permitir ediciones
posteriores: bastaría con poner la variable en `false` y cada republicación
incrementaría la versión. Mientras tanto queda en 1.

## 8. Correo y QR

El correo lleva: botón al visor público, código QR **embebido como `data:` URI**
apuntando al mismo visor (no depende de que el cliente cargue imágenes remotas), y el
identificador de la carta con su versión publicada. El estado del envío se persiste; un
fallo de SMTP deja la entrega en `failed` con el tipo de error, sin romper la carta ni
la compra, y el reenvío crea un nuevo intento.

También hay QR como imagen: `GET /api/v1/letters/{id}/qr.png` (dueño) y
`GET /api/v1/public/letters/{slug}/qr.png` (público).

## 9. "Mis cartas" y "Mis dedicatorias"

`GET /api/v1/letters` devuelve, por cada carta del comprador: estado, borrador
recuperable con su contenido y fotos, compra asociada, enlace público y QR cuando está
publicada, y el historial de entregas con su estado. `GET /api/v1/purchases` añade el
estado de pago y `hasLetter`. Con eso el frontend arma la biblioteca: ver, consultar
estado de pago y entrega, reenviar y continuar borradores.

`GET /api/v1/me/dedications` es el panel posventa: una fila por compra **pagada**, con su
carta si existe, en una sola consulta (`LEFT JOIN`) y sin cuerpo, fotos ni entregas. El
estado se deriva al leer y no se guarda (ninguna migración):

- `draft`: compra pagada sin carta, o carta en borrador. No hay autoguardado en el
  editor: quien pagó y cerró la pestaña no dejó fila de carta, y su borrador **es** la
  compra pagada.
- `published`: carta publicada. Se conserva aunque la compra se anule después por un
  reembolso: el enlace que recibió el destinatario sigue vivo.

Las compras sin pagar no aparecen. Para retomar un borrador el frontend abre el editor
con el `purchaseId` y envía `POST /api/v1/letters` como siempre: si había carta en
borrador se sobrescribe con el contenido y las fotos nuevas y se publica; una publicada
no se toca.

## 10. Qué se probó localmente y qué depende de terceros

Probado en local, sin credenciales reales (114 pruebas, PostgreSQL 15 efímero):
idempotencia de compras, concurrencia, pago no confirmado, monto que no cuadra, webhook
repetido y fuera de orden, una compra ⇒ una carta, doble clic y varias pestañas,
permisos entre usuarios, cédula privada, borradores, emojis y saltos de línea, orden de
fotos, congelado, visor público, QR, fallo y reintento de correo.

**Requiere configuración externa y no se ejecutó aquí:** cobro real en Mercado Pago y
firma real de sus webhooks; subida real a Azure Blob Storage (el backend Azure está
implementado y se selecciona por configuración, pero no se ejecutó contra una cuenta);
envío real por SMTP de Gmail; login real de Google. Las tres integraciones están detrás
de puertos inyectables, de modo que activarlas es cambiar variables de entorno.
