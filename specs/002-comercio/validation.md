# Validación 002 — resultado

Fecha: 2026-09-06. Rama `develop-julian`, sin commits ni push. Linux, Python 3.12.14.

## Ejecutado

- **Alembic**: `upgrade head` → `check` (sin drift) → `downgrade base` → `upgrade head`,
  correcto sobre PostgreSQL 15 efímero en loopback.
- **Pytest**: **114 passed**, cero fallos. Repetido 6 veces seguidas sin intermitencias.
- **Newman**: carpetas `Automated local` + `Commerce local`, **29 solicitudes, 47
  aserciones, 0 fallos**, contra Uvicorn local y base efímera, sin proveedor de pagos.
- **Ruff**: `check` y `format --check` limpios sobre `app tests scripts alembic`.
- **pip check**: 51 paquetes, sin dependencias rotas.
- **Docker**: `compose config`, `build`, `alembic upgrade head` en contenedor,
  `up -d api`; `/health/live`, `/health/ready` y el flujo registro→login→compra→compra
  repetida verificados contra la imagen. Entorno destruido al terminar (`down -v`).

## Defectos encontrados y corregidos

1. Con el pool de conexiones al límite, una colisión de clave única hacía que el
   `rollback()` no consiguiera conexión y la API respondiera 503 en vez de resolver la
   idempotencia. Corregido con `INSERT … ON CONFLICT DO NOTHING` en compras y cartas.
2. Preexistente en el login de Google: dos primeros inicios simultáneos del mismo `sub`
   hacían que el segundo respondiera `ACCOUNT_LINK_REQUIRED` porque encontraba el correo
   ya creado. Ahora se devuelve la misma identidad solo si el `sub` coincide exactamente;
   la política de no vincular automáticamente por correo se mantiene intacta.

## Límites

- Mercado Pago, Azure Blob Storage, SMTP de Gmail y Google se ejercitan con puertos
  inyectables y respuestas deterministas. **No se usó ninguna credencial real** ni se
  demostró un cobro, una subida, un correo o un login remotos.
- No se ejecutó nada contra Azure: ni migraciones, ni consultas, ni despliegues.
- No hay prueba de carga. El presupuesto de 20 conexiones no demuestra ninguna cifra de
  usuarios concurrentes.
- La firma HMAC del webhook sigue el esquema documentado por Mercado Pago
  (`id` + `request-id` + `ts`); debe confirmarse contra una notificación real antes de
  producción.
