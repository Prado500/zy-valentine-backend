# Especificación 002: compatibilidad de configuración y dominio comercial

Estado: implementada y validada localmente el 2026-09-06. Rama `develop-julian`, sin
commits ni push. Continúa la spec 001 sin rehacerla.

## Decisiones de partida

- **Se conserva la autenticación por sesión opaca. JWT no se activa.** Las variables
  `JWT_SECRET_KEY`, `ALGORITHM` y `ACCESS_TOKEN_EXPIRE_MINUTES` de Azure son heredadas
  del proyecto de referencia y esta API no las lee.
- `docs/AZURECONF.md` contiene contraseñas en texto plano: se tratan como
  comprometidas, no se copian a ningún artefacto y deben rotarse (ver
  `docs/MATRIZ_CONFIGURACION.md`).
- `ecotur-asoprado-api` se usa solo como referencia de organización por capas. No se
  copian su dominio, sus roles ni su modelo de tokens. No se modificó.
- `Iops.md` y los pipelines no se modificaron. No se ejecutó nada contra Azure.

## Requisitos verificables

- C01: `Settings` cubre entorno, base de datos, TLS, CORS/frontend, pool y presupuesto,
  Google, cédula, pagos, almacenamiento y correo, con validación de arranque.
- C02: `DATABASE_URL` admite exclusivamente `sslmode`/`ssl` en la query y los traduce a
  TLS verificado; cualquier otro parámetro aborta el arranque y ningún entorno remoto
  puede degradar TLS.
- C03: develop/staging/production exigen TLS verificado, orígenes HTTPS, cookies
  seguras y almacenamiento durable en Azure.
- C04: `workers × réplicas × (pool + overflow) + reserva ≤ presupuesto`, verificado al
  arrancar. El presupuesto es de conexiones, no de usuarios.
- D01: cédula en tabla aparte, solo HMAC con clave dedicada y últimos 4 dígitos; nunca
  credencial, nunca en enlaces públicos ni en `/me`.
- D02: compras con clave de idempotencia única por usuario; muchas compras por usuario.
- D03: verificación de pago en servidor (consulta al proveedor o webhook firmado). El
  retorno del navegador no confirma nada.
- D04: webhooks firmados, idempotentes por `(provider, event_id)` y monótonos en estado.
- D05: una compra pagada habilita exactamente una carta, garantizado por UNIQUE y
  bloqueo transaccional; el segundo intento devuelve la misma carta con 200.
- D06: fotos en almacenamiento por puerto (local o Azure Blob), validadas por firma
  binaria, con orden estable.
- D07: publicación genera enlace público y QR; el contenido queda congelado.
- D08: correo con botón, QR e identificador de carta y versión; estado de envío
  persistido, reintentos sin duplicar cartas ni compras.
- D09: "Mis cartas" expone estado de pago y entrega, reenvío y borradores.
- Q02: pruebas de configuración, TLS, migraciones, dominio completo y colección Postman
  ejecutable sin secretos.

## Fuera de alcance

Vinculación explícita de cuentas Google, recuperación de contraseña, verificación de
correo, panel de administración, reembolsos iniciados desde la API, limpieza programada
de sesiones y compras vencidas, y cualquier cambio en Azure, pipelines o frontend.
