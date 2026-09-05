# Contrato y plan conjunto de Zyvencore

Fecha: 2026-09-05. Versión documental: propuesta 1.
Alcance autorizado: auditoría y documentos Markdown. Nada aquí afirma que las capacidades propuestas estén implementadas ni autoriza implementarlas, desplegar, cobrar o enviar correos. Sin datos reales de identidad.

Este archivo debe mantenerse idéntico en ambos repositorios. El backend será la fuente de OpenAPI cuando se implemente; de allí se derivarán tipos y cliente frontend. La auditoría específica está en [AUDITORIA.md](AUDITORIA.md).

## Reglas confirmadas y decisiones abiertas

- Un comprador puede realizar varias compras y crear varias cartas para distintas personas.
- **Una compra pagada habilita una carta.** No se limita a una carta por usuario y un pago no habilita cartas ilimitadas.
- Consultar, descargar o reenviar la misma carta no crea otra compra ni consume otro derecho.
- La identidad interna del comprador se vinculará también a su cédula. Google no verifica esa cédula.
- Comprador autenticado; destinatario accede por enlace secreto. Quien tenga el enlace puede leer y compartirlo.
- Pendientes: país/tipo de documento, normalización, verificación y unicidad de cédula; proveedor de pagos/correo, precio, impuestos, retención, reembolsos, privacidad reforzada y política de edición después de publicación.
- Recomendación pendiente de aprobación: fijar contenido y destinatario al publicar, antes de cualquier envío; conservar una versión entregable inmutable. Un nuevo destinatario o nuevo contenido requiere nueva compra. Correcciones excepcionales por soporte, auditadas, sin liberar el derecho original. No presentar esta política como decisión ya tomada.
- Cambiar solo el correo de entrega para corregir una dirección requiere política explícita: el contenido no cambia, hay confirmación del comprador y auditoría. Reenviar no debe convertirse en una función de envío masivo.
- No se ha comprobado funcionamiento en producción.

## Arquitectura y responsabilidades

Mantener React/TypeScript en el frontend y FastAPI en un backend modular único. No hace falta separar más servicios ahora.

Frontend: editor, borradores locales auxiliares, biblioteca Mis cartas, sesión, estados de carga/error y visor. Backend: identidad, propiedad, compras, validación, persistencia, publicación y entregas. PostgreSQL: datos y transacciones. Almacenamiento de objetos: imágenes. Un proceso trabajador consume una outbox persistente para correo y reintentos.

Separar módulos de identidad, compras, cartas, medios y entregas. Routers delgados -> servicios de casos de uso -> repositorios. Compartir transacción entre repositorios cuando una regla abarque compra, carta y outbox. Evitar capas vacías o un repositorio genérico que oculte bloqueos y restricciones.

Publicación y visualización deben conectarse ambas a la API: cambiar solo publishDedication no basta. El QR y el botón del correo usarán exactamente la URL canónica HTTPS del visor /c/{publicToken}, sin cédula, email, token de sesión ni identificador de compra. Configurar alojamiento para resolver rutas del frontend al recargar.

## Identidad y Google

Propuesta: Google Identity Services en frontend; POST /api/v1/auth/google verifica el ID token en servidor mediante google-auth, incluyendo firma, aud, iss y exp. Vincular (provider, subject) usando sub a un userId interno. No confiar en un userId recibido del navegador ni vincular cuentas solo por coincidencia de email. Configurar cliente OAuth web y orígenes autorizados. Credenciales privadas solo en backend.

Crear sesión propia revocable con cookie HttpOnly, Secure y SameSite compatible con el despliegue elegido. Proteger login y operaciones de escritura contra CSRF; para el POST directo de GIS validar su double-submit cookie; si se usa callback JavaScript diseñar la protección explícitamente. Expiración de sesión lleva a reautenticación preservando borrador e intención, sin repetir una compra. Logout revoca sesión y elimina datos privados locales de esa cuenta. GET /me no devuelve cédula completa.

Documento del comprador: userId UUID independiente; country, documentType, documentNumberNormalized (texto, preservando ceros donde corresponda), verificationStatus inicialmente unverified. Definir normalización por país/tipo antes de aplicarla; no convertir indiscriminadamente a número ni retirar caracteres potencialmente significativos. Guardar el dato protegido y mostrarlo enmascarado. Excluirlo de URLs, logs y DTO público. Si el negocio decide unicidad, diseñar índice único por país/tipo/valor normalizado o huella HMAC con clave, y un proceso seguro para resolver conflictos; no fusionar identidades automáticamente. El hash simple de una cédula no protege frente a enumeración. La cédula nunca es contraseña ni prueba de propiedad de una carta.

El mecanismo y necesidad de verificación documental quedan pendientes; no se presume verificación real ni se solicita documentación real durante esta auditoría.

## Modelo de datos propuesto

| Entidad | Campos y restricciones principales |
|---|---|
| users | id UUID PK, timestamps; sin límite de cartas por usuario |
| auth_identities | user_id FK, provider, subject; UNIQUE(provider, subject) |
| buyer_documents | user_id FK, país/tipo/número normalizado protegido, verification_status; unicidad de documento pendiente |
| purchases | id UUID PK, user_id FK, precio/unidad monetaria fijados por servidor, currency, state, timestamps; UNIQUE(id,user_id) |
| payment_attempts | purchase_id FK, provider, provider_payment_id único por proveedor, state; máximo un intento activo por compra, identificador de operación duradero |
| payment_events | UNIQUE(provider,event_id), referencia a intento, datos mínimos de verificación, processing_state |
| drafts | id UUID PK, user_id FK, purchase_id NOT NULL UNIQUE, schema_version, revision, contenido, editor_step, created_at, updated_at |
| cards | id UUID PK, user_id FK, purchase_id NOT NULL UNIQUE, draft_id NOT NULL UNIQUE, state, published_at; relación compra inmutable |
| card_versions | id UUID PK, card_id FK, version, contenido validado inmutable; UNIQUE(card_id,version); una versión pública activa según política |
| media | id UUID PK, user_id FK, draft_id FK, object_key, estado de validación, tamaño, tipo, checksum |
| version_media | version_id, media_id, position; UNIQUE(version_id,position), UNIQUE(version_id,media_id) |
| public_links | token_hash UNIQUE, card_id FK, version_id FK, revoked_at; token criptográfico aleatorio de al menos 128 bits |
| deliveries | id UUID PK, card_id, version_id, email destino privado, state, request_key, provider_message_id, intent_id |
| outbox | evento persistente, aggregate_id, intent_id UNIQUE, payload mínimo, attempts, next_attempt_at, locked_until |
| idempotency_records | UNIQUE(user_id,operation,key), request_hash, resource_id, estado y respuesta persistida |

Usar FKs compuestas (purchase_id,user_id) para impedir asociar borrador/carta a compra ajena, y restricción equivalente entre carta y borrador para asegurar que ambos pertenecen a la misma compra. No borrar compras/cartas para liberar UNIQUE(purchase_id): archivar/revocar conservando la asociación. Para borrado legal futuro, diseñar anonimización y evidencia mínima de consumo conforme a política; no reutilizar el derecho por un DELETE.

Base de datos garantiza cardinalidad y pertenencia mediante UNIQUE, NOT NULL y FK. Que una compra esté pagada es una condición entre filas: no se debe expresar como un CHECK que consulta otra tabla. Verificarla en transacción bloqueando la compra; centralizar todos los cambios de pago y publicación en ese protocolo. Si existen otros escritores, exigir una función transaccional o trigger equivalente y permisos restringidos.

## Estados y transacciones

Estados independientes, nunca un único indicador de “completado”:

- Compra: pending -> paid tras confirmación servidor; pending -> cancelled/expired solo tras resolución del proveedor; paid -> refunded/disputed según evento verificado. Una confirmación tardía de compra aparentemente cancelada/expirada exige reconciliación; puede pasar a paid si el cobro es válido o a revisión con devolución. No ignorar dinero recibido.
- Intento de pago: creating -> pending -> succeeded/failed/cancelled; unknown exige consulta/reconciliación, no otro cobro automático.
- Borrador: editable antes de publicación; no acredita pago ni es carta entregable.
- Carta: published -> revoked o archived; esas transiciones no liberan compra. Recuperar el mismo enlace/versión se rige por política; nunca crear otra carta con la compra consumida.
- Entrega: queued -> processing -> provider_accepted -> delivered o bounced; errores transitorios -> retry_wait -> processing; error permanente -> failed. “Aceptado por proveedor” no prueba recepción ni lectura.

### Compra y retorno del proveedor

1. Crear una compra con precio/moneda/producto calculados en servidor y clave idempotente. Crear su borrador en la misma transacción. Guardar IDs antes de navegar fuera.
2. Reanudar la misma purchaseId al volver, refrescar o hacer doble clic. Una nueva compra requiere una acción explícita; otra pestaña del mismo flujo recupera la compra existente.
3. Serializar creación de intentos por compra; persistir primero el identificador de operación, luego solicitar checkout fuera del bloqueo de base de datos usando idempotencia del proveedor.
4. Si hay timeout, consultar el mismo intento. No abrir uno nuevo hasta resolver el anterior. Si el proveedor no soporta idempotencia/reconciliación adecuada, el caso queda bloqueado para intervención; no prometer ausencia de dobles cargos.
5. El retorno success/cancel en URL solo indica navegación: consultar GET /purchases/{id}. Nunca habilitar publicación por un parámetro success=true.
6. Webhook: verificar autenticidad según proveedor elegido y validar compra, cuenta comercial, importe y moneda esperados. Registrar evento único y procesarlo transaccionalmente. Un evento duplicado no repite efectos.
7. Para eventos fuera de orden, consultar estado autoritativo del proveedor cuando corresponda; un pending antiguo no degrada paid ni un succeeded antiguo deshace refunded. Reembolsos/contracargos son eventos propios, no simples regresiones.
8. Una anomalía con dos cobros reales para una misma compra se reconcilia y alerta para devolución según política; no entrega una segunda carta automáticamente. No puede garantizarse exactamente un cargo sin conocer el proveedor.

### Publicar exactamente una carta

Dentro de una transacción: bloquear purchase por ID; verificar propietario, state=paid, importe validado y ausencia de bloqueo por disputa; bloquear draft y comprobar revision; validar contenido y media disponibles. Si ya existe card para esa purchase, devolverla para repetición equivalente; si se intenta contenido distinto, responder 409 PURCHASE_ALREADY_CONSUMED.

Crear card + versión + enlace + entrega inicial (si se solicitó) + outbox en la misma transacción y persistir resultado idempotente. UNIQUE(purchase_id) protege incluso solicitudes con claves distintas. Nunca marcar “consumido” en React. La asociación card-purchase es evidencia duradera del consumo.

Actualizaciones concurrentes de borrador usan comparación atómica de revision; si no coincide, 409 REVISION_CONFLICT con revisión actual. No sobrescribir silenciosamente. Bloquear compra antes de borrador de forma consistente, incluidos cambios de pago, evita publicar simultáneamente con un reembolso ya confirmado. Una reversión posterior sigue política de acceso/reembolso sin liberar el derecho.

### Idempotencia y correo

Misma clave + mismo cuerpo: misma compra/carta/entrega y respuesta. Misma clave + cuerpo diferente: 409 IDEMPOTENCY_KEY_REUSED. Vincular claves a usuario y operación. Resolver registros en curso con estado recuperable. No depender únicamente del TTL de la caché: compras, intentos, cartas y eventos conservan restricciones duraderas.

Cada intención de entrega tiene ID estable. La outbox se confirma junto a la carta, y el trabajador toma un arrendamiento, reintenta con espera creciente y registra el resultado. Reutilizar la clave del proveedor cuando exista. Si el proveedor aceptó y el trabajador cayó antes de guardar respuesta, reconciliar antes de reenviar; sin soporte del proveedor, la entrega es al menos una vez y puede duplicarse. No afirmar “exactamente una vez”.

Botón “Ver carta”, enlace en texto y QR del correo apuntan al mismo publicUrl y versión. Persistir deliveryEmail en la intención privada, no en el DTO público. Reenviar crea otra intención explícita, nunca otra carta ni compra; doble clic reutiliza la misma intención. El correo al comprador y un posible envío directo al destinatario son opciones a definir; no deducir destinatarios del campo recipient, que es un nombre.

## Contratos propuestos, no existentes

IDs internos UUID; fechas UTC ISO 8601; JSON UTF-8; camelCase en API y alias explícitos Pydantic. Servidor genera owner, IDs, createdAt, updatedAt y estados. Cliente no decide precio, pago ni propietario.

Payload para guardar borrador (PUT /api/v1/drafts/{id}, con Idempotency-Key):

```json
{
  "schemaVersion": 1,
  "revision": 3,
  "editorStep": 2,
  "data": {
    "recipient": "Persona de ejemplo",
    "title": "Para ti",
    "message": "Primera línea 💌\nSegunda línea",
    "sender": "Remitente de ejemplo",
    "songUrl": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "themeId": "classic",
    "photos": [
      {"mediaId": "11111111-1111-4111-8111-111111111111", "position": 0}
    ]
  }
}
```

Esquema propuesto: schemaVersion literal 1; revision entero >= 0; editorStep entero 1..3; data objeto con campos obligatorios, aceptando textos vacíos durante borrador. Límites iniciales propuestos: recipient/sender hasta 120 caracteres, title hasta 200, message hasta 20.000, songUrl hasta 2.048; revisar con producto. Contar Unicode de manera consistente entre Python/JavaScript y documentar si se usa punto de código; no cortar emojis.

photos: lista de 0..5 objetos, mediaId UUID, position entero 0..4, sin duplicados y posiciones consecutivas desde cero. Temas permitidos: classic, pastelPink, starry, sunset, lavender, emerald, midnight, vintage. Validar propiedad, estado y vínculo de cada medio al borrador. No aceptar blob:, data: ni URL arbitraria como sustituto de mediaId. Subida propuesta de imágenes raster con tamaño/dimensiones máximos configurados, validación real y límites servidor; compresión cliente no es validación.

Publicación exige nombres/título/mensaje no vacíos tras comprobar espacios, tema válido, songUrl vacío o YouTube HTTPS válido y fotos terminadas. Rechazar versión desconocida y campos extra. Usar validación estricta donde corresponda para impedir coerciones sorprendentes; un cast TypeScript no valida JSON. Guardar texto original sin escape HTML; escapar al renderizar/exportar. Preservar saltos y orden, evitando doble escape.

POST /api/v1/purchases/{purchaseId}/publish:

```json
{
  "draftId": "22222222-2222-4222-8222-222222222222",
  "revision": 4,
  "deliveryEmail": "comprador@example.invalid"
}
```

deliveryEmail opcional: si está presente crea intención inicial; validar formato/límites y autorización de envío. El servidor devuelve cardId, purchaseId, versionId, state, publicUrl y deliveryId opcional. Ninguna llamada a este endpoint está implementada.

DTO público de GET /api/v1/public/cards/{token}:

```json
{
  "schemaVersion": 1,
  "data": {
    "recipient": "Persona de ejemplo",
    "title": "Para ti",
    "message": "Primera línea 💌\nSegunda línea",
    "sender": "Remitente de ejemplo",
    "songUrl": "",
    "themeId": "classic",
    "photos": [
      {"url": "https://media.example.invalid/image.webp", "position": 0}
    ]
  }
}
```

La URL de media la autoriza el servidor para esa publicación. Objetos de borradores privados; enlaces temporales deben renovarse al cargar el visor. Revocar carta debe impedir nuevas autorizaciones de medios, aunque no retira copias descargadas. No incluir cédula, email de entrega, userId ni pago en respuesta pública.

| Endpoint propuesto | Uso y permiso |
|---|---|
| POST /auth/google; POST /auth/logout; GET /me | Sesión e identidad |
| PUT /me/document | Documento comprador privado, reglas pendientes; no presume verificación |
| POST /purchases | Crear compra y borrador; sesión, documento requerido según política e idempotencia |
| GET /purchases/{id}; POST /purchases/{id}/checkout | Consultar/reanudar pago propio; operación idempotente |
| GET /drafts/{id}; PUT /drafts/{id} | Recuperar/guardar borrador propio con revision |
| POST /drafts/{id}/media | Subir medio privado y devolver mediaId; publicación espera estado ready |
| POST /purchases/{id}/publish | Pago confirmado y propiedad; una carta por compra |
| GET /library?cursor=... | Mis cartas: compras, borradores, cartas y entrega, solo del usuario |
| POST /cards/{id}/deliveries | Reenvío de versión existente; sesión, límites, idempotencia |
| GET /public/cards/{token} | Visor por enlace secreto válido |
| POST /webhooks/payments/{provider} | Autenticidad del proveedor; sin confiar en sesión navegador |

Todas las rutas de tabla llevan prefijo /api/v1. DTOs adicionales se formalizarán en OpenAPI antes de implementación. Errores coherentes: code, message, fieldErrors, requestId; 401 sesión, 404 recurso ajeno/inexistente, 409 conflictos, 413 tamaño, 422 validación, 429 límites. Evitar filtrar datos de otros compradores.

## Navegación, recuperación y Mis cartas

Evidencia actual estática: EditorPage mantiene step y form en useState; botones de pasos solo hacen setStep. Atrás interno conserva datos mientras el componente siga montado y no añade entradas al historial. Atrás del navegador navega por páginas, no por esos pasos. Salir enlaza a /. No hay autosave ni hidratación del editor desde cartas publicadas. Recarga/reentrada reinicia el formulario. Restauraciones particulares del navegador no son garantía de producto.

No existe checkout: el botón comercial en Pricing carece de onClick/navegación. No se ha probado regreso de proveedor ni existe un pago que pueda duplicarse hoy.

Diseño propuesto:

- Flujo base: login -> documento comprador -> compra/checkout -> confirmación servidor -> editor reanudable -> publicar -> biblioteca. Permitir edición previa al pago es decisión opcional; nunca habilita publicación.
- Crear un draft estable por purchaseId. Persistir contenido y último paso en servidor, con copia auxiliar IndexedDB por userId/purchaseId/draftId, incluyendo blobs todavía sin subir. No guardar cédula ni sesión en esa copia.
- Autoguardado con espera breve y secuencia de revisiones; mostrar “Guardando”, “Guardado” o “Pendiente de sincronizar”. No afirmar guardado servidor hasta confirmación.
- Atrás interno conserva estado; rutas /editor/{draftId}?step=2 permiten restaurar paso si se adopta. Definir si pasos usan replace o push; recomendación replace para que Atrás navegador vuelva a la página anterior sin recorrer cada cambio.
- Antes de checkout, guardar borrador/IDs y resolver fallos; si no se guardó, informar y permitir reintentar. Conservar clave de intento para retorno.
- Al volver, autenticar si hace falta y recuperar compra/borrador del servidor; consultar estado del pago. Cancelar ventana o volver atrás no equivale a pago cancelado.
- Recarga/cierre: recuperar última versión confirmada más cambios locales pendientes. No prometer salvar una pulsación que no llegó a almacenamiento; beforeunload es solo advertencia auxiliar, no mecanismo de persistencia.
- Error de subida: conservar archivo local y permitir reintentar; no publicar fotos blob temporales. Cuota local llena: explicar el fallo sin borrar otras cartas. Fallo de red/422 conserva todos los campos.
- Varias pestañas: control optimista por revision y notificación local opcional; un conflicto conserva ambas propuestas y solicita resolución. No usar last-write-wins silencioso.
- Al expirar sesión, guardar pendiente bajo la cuenta original; solo sincronizar al reautenticar esa misma cuenta. Cambio de cuenta nunca importa borradores ajenos.
- Mis cartas muestra una fila por compra con destino/título, estado pago, borrador o carta, última actualización y estado de entrega. Acciones: continuar pago, retomar editor, abrir/descargar/reenviar carta; “Crear otra” inicia nueva compra explícita.
- Guardar createdAt una sola vez y actualizar updatedAt. No usar el almacenamiento local publicado actual como fuente fiable: validar y migrar de forma explícita; importar una carta local no acredita una compra pagada.

## Matriz de aceptación futura

Ninguno de estos casos se declara probado en ejecución.

| Caso | Resultado exigido |
|---|---|
| Atrás interno y cambio directo de paso | Conserva textos/fotos; restaura paso persistido |
| Atrás navegador, Salir y volver | Recupera draft correcto; informa cambios pendientes |
| Recargar/cerrar/reabrir | Recupera confirmados y pendientes locales disponibles, sin mezclar cuentas |
| Error de red, 422 o cuota local | Mantiene datos; no purga cartas ni muestra guardado falso |
| Foto falla al convertir/subir | Conserva original local, permite reintento, bloquea publicar medio temporal |
| Emojis, saltos, comillas, < y & | Ida/vuelta API y exportación conserva texto/orden; render seguro sin doble escape |
| schemaVersion desconocida/JSON inválido | Error controlado, sin cast ni pérdida del borrador original |
| Login cancelado o sesión vencida | Retoma intención tras login; no crea nueva compra |
| Cédula con formatos equivalentes | Normalización según reglas aprobadas; sin asumir identidad verificada |
| Mismo usuario, dos compras pagadas | Dos cartas distintas para personas distintas |
| Pago pendiente/falso retorno success | No publica; consulta estado servidor |
| Doble clic compra o checkout | Mismos IDs e intento, sin segundo cobro provocado por reintento |
| Webhooks duplicados/fuera de orden | Un efecto; no degrada paid ni revierte reembolso por evento antiguo |
| Timeout checkout y retorno tardío | Reconciliación antes de crear otro intento |
| Doble publicación, distintas claves y pestañas | Una sola card por purchase, respuesta equivalente o 409 |
| Dos editores guardan misma revision | Uno gana; el otro conserva cambios y recibe conflicto |
| Reembolso simultáneo con publicación | Orden transaccional definido; asociación no reutilizable |
| Editar destino/contenido tras publicar | Política aprobada aplicada servidor; no elude nueva compra |
| Abrir/descargar/reenvío y doble clic | Misma carta/versión; sin nuevo consumo, intención idempotente |
| Caída trabajador antes/después de aceptación | Outbox recuperable, reconciliación, sin promesa infundada de entrega única |
| Correo con botón y QR desde otro dispositivo | Ambos abren mismo visor/versión y muestran fotos |
| Usuario B pide compra/carta/media de A | Acceso rechazado, sin filtrar cédula/email |
| Enlace secreto revocado o desconocido | No devuelve contenido; no revela información privada |
| Reinicio servidor o vaciado de caché idempotente | UNIQUE y relaciones persistentes siguen impidiendo segunda carta |

## Plan con dependencias y criterios de salida

1. **Decisiones y contratos:** confirmar documento, proveedores, límites, privacidad y edición. Formalizar OpenAPI/DTO y estados. Salida: frontend/backend comparten ejemplos y reglas; decisiones pendientes explícitas.
2. **Base backend:** reparar migraciones/importaciones, configuración validada, sesión DB y manejo de errores; dependencias reproducibles. Salida: migraciones desde base vacía y pruebas aisladas.
3. **Identidad y borradores:** Google, documento, propiedad, autosave y biblioteca. Salida: reautenticación, recarga, varias pestañas y cuenta ajena probados.
4. **Compras:** integrar proveedor en sandbox, intentos y eventos idempotentes. Salida: duplicados, timeouts y retorno verificados; no pagos reales.
5. **Cartas/media:** subida, publicación transaccional, versión/enlace y visor. Salida: una compra/una carta bajo concurrencia y QR en otro navegador.
6. **Entregas:** outbox, proveedor sandbox, botón/QR, reenvío y rebotes. Salida: fallos/reinicio recuperables y observabilidad sin datos sensibles.
7. **Validación integral:** matriz completa, compilación/lint, contratos y pruebas de acceso; política de retención/copias y operación. Salida: evidencia revisable antes de solicitar autorización separada para producción.

No realizar commits, push, cambios funcionales ni despliegues con base en este documento.

## Fuentes técnicas verificadas

- [Google: verificación de ID token](https://developers.google.com/identity/gsi/web/guides/verify-google-id-token) y [configuración del cliente](https://developers.google.com/identity/gsi/web/guides/get-google-api-clientid): validación servidor y configuración GIS.
- [FastAPI: CORS](https://fastapi.tiangolo.com/tutorial/cors/): usar orígenes explícitos cuando se habilitan credenciales.
- [PostgreSQL: restricciones](https://www.postgresql.org/docs/current/ddl-constraints.html): UNIQUE/FK y límites de CHECK entre tablas.
- [PostgreSQL: bloqueos](https://www.postgresql.org/docs/current/explicit-locking.html): coordinación transaccional de filas.
- [Pydantic: validación estricta](https://pydantic.dev/docs/validation/latest/concepts/strict_mode/): control de coerciones.

Consulta 2026-09-05. Las páginas “current” de PostgreSQL describen la versión vigente; el Compose actual declara PostgreSQL 15 y debe validarse contra su versión al implementar. Los diseños de negocio, outbox y endpoints son propuestas propias, no capacidades afirmadas por estas fuentes.

