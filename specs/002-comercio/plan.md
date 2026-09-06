# Plan 002

1. `app/core/dsn.py` y `Settings`: matriz de variables, traducción segura de TLS,
   presupuesto de conexiones y validación de integraciones (C01..C04).
2. Modelos y migración `0002_commerce` con las restricciones que sostienen las reglas
   de negocio (D01..D08).
3. Puertos de integración inyectables: pagos (Mercado Pago), almacenamiento (local y
   Azure Blob), correo (consola y SMTP) y QR (segno, sin dependencias binarias).
4. Servicios: identidad privada, compras y aplicación de estados de pago, webhooks,
   cartas con bloqueo e idempotencia, fotos y entregas.
5. Routers `/api/v1` para compras, cartas, fotos, QR, visor público y webhook.
6. Pruebas de configuración y de dominio; runner local con PostgreSQL efímero en Docker
   o binarios locales; colección Postman ampliada; documentación.

Decisiones de implementación propias: `INSERT … ON CONFLICT DO NOTHING` en lugar de
capturar `IntegrityError` en los caminos de idempotencia (evita quedarse sin conexión
del pool al deshacer la transacción), HMAC de cédula con clave dedicada, y congelado
del contenido al publicar.
