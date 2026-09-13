# Operación base/auth y coordinación Azure

Propuesta de configuración, no despliegue verificado. No modifica pipelines ni el archivo Iops.md del compañero.

> **Actualizado el 2026-09-06.** Este documento describe el corte base+auth (spec 001).
> Las secciones de variables y de exclusiones quedaron superadas por el corte 002:
> la matriz completa de variables está en [MATRIZ_CONFIGURACION.md](MATRIZ_CONFIGURACION.md)
> y el dominio comercial (compras, pagos, cartas, fotos, QR y correo), que aquí figura
> como excluido, está implementado y descrito en [DOMINIO_COMERCIAL.md](DOMINIO_COMERCIAL.md).
> Lo que sigue vigente sin cambios: sesiones opacas sin JWT, presupuesto de conexiones,
> migración ejecutada una sola vez por proceso autorizado y los puntos de handoff.

## Datos conocidos y lo que falta

Iops.md declara presupuesto de 20 conexiones en una instancia limitada, 240 IOPS y un máximo de usuarios impuesto por balanceador. No aporta nombres de servidores, DSN, certificados, secretos, número de réplicas ni variables del pipeline. Un paso de negocio no equivale a una operación física de disco; no se validó ese dimensionamiento ni “12 peticiones” simultáneas por conexión.

La referencia usa FastAPI/SQLAlchemy y DATABASE_URL. Esta implementación adopta esos componentes, pero no sus roles, tablas, logs SQL ni JWT por email. Usuario eligió contraseña y Google; sesiones opacas revocables conservan la propuesta documental previa.

## Variables

| Variable | Contrato |
|---|---|
| APP_ENV | local, develop, staging, production; main se configura production, sin inferir desde Git |
| DATABASE_URL | Secreto postgresql+asyncpg://...; sin query; usuario DB del entorno |
| SESSION_SECRET | Secreto aleatorio >=32 caracteres, igual en réplicas; rotarlo invalida CSRF pendiente |
| CORS_ORIGINS | Array JSON de orígenes exactos, sin comodines/rutas; HTTPS obligatorio fuera de local |
| DB_SSL_MODE | disable solo local; verify-full fuera de local |
| DB_SSL_CA_FILE | Archivo CA confiable opcional; sin él utiliza CA del sistema |
| DB_POOL_SIZE / DB_MAX_OVERFLOW | Por proceso, 2 y 0 por defecto |
| DB_POOL_TIMEOUT | Espera máxima de pool en segundos, 10 por defecto |
| WEB_CONCURRENCY / APP_REPLICAS | Número real de procesos y réplicas, ambos 1 por defecto |
| DB_CONNECTION_BUDGET / DB_RESERVED_CONNECTIONS | 20 total y 2 reservadas por defecto |
| GOOGLE_CLIENT_ID | Cliente web Google; vacío mantiene login contraseña y Google devuelve 503 explícito |
| SESSION_MINUTES / CSRF_SECONDS | 30 minutos y 3600 segundos por defecto |
| COOKIE_SAMESITE | lax por defecto; none requiere entorno seguro y diseño cross-site |
| AUTH_RATE_LIMIT / AUTH_RATE_WINDOW | 30 intentos por IP/proceso/60 s; memoria acotada |
| PORT | Puerto de Dockerfile, 8000 por defecto |

Validación de arranque: workers × réplicas × (pool + overflow) + reserva <= presupuesto. Los defaults usan dos conexiones de aplicación, no veinte por worker. La fórmula depende de configurar el conteo real, incluyendo despliegues simultáneos, herramientas y migraciones; no descubre automáticamente otros procesos. Una AsyncSession se utiliza por petición, nunca compartida concurrentemente.

Conexión y comandos DB con timeout; sin echo SQL. TLS verifica hostname y cadena; no se desactiva verificación para resolver certificados. Secretos no van en la colección ni en repositorio; no se definieron valores Azure.

## Handoff al compañero, antes de desplegar

1. Confirmar variables y adaptación de sus pipelines, sin que esta tarea los modifique. Confirmar base por entorno, credenciales, CA, puerto y número real de réplicas/workers.
2. Revisar estado/esquema de la DB existente con su dueño; migración inicial crea users/auth_sessions y no debe aplicarse sobre tablas incompatibles.
3. Ejecutar migración una vez mediante su proceso autorizado, no en cada worker. Mantener presupuesto para migración y despliegue superpuesto.
4. Configurar HTTPS, orígenes de frontend y Google client ID. Readiness verifica revisión, liveness no depende de DB.
5. Revisar IP real/proxy de confianza y limitación global. Docker usa --no-proxy-headers: no confía en X-Forwarded-For del cliente; detrás de proxy el límite puede agrupar usuarios. Coordinación requerida antes de habilitar cabeceras confiables o escalado.
6. Definir mantenimiento de sesiones expiradas (índice expires_at existente), retención de perfiles, auditoría y alertas. No hay tarea remota de limpieza creada.
7. Validar login Google real, navegador cross-origin y cookie Secure según dominios. La prueba local HTTP no demuestra integración Azure.
8. Configurar controles distribuidos de tasa/abuso para varias réplicas. El limitador local y dos hashes Argon2 simultáneos por proceso son límites concretos, no protección global.

## Exclusiones deliberadas

Sin compras/cartas/pagos/correo, endpoint de documento, verificación de cédula, vinculación de cuentas, recuperación/cambio de contraseña ni envío de verificación de email. No existe rol administrador expuesto. GET /me solo devuelve el usuario de sesión. Registro con contraseña admite email no verificado, señalizado como emailVerified=false.

Google usa sub único y no hace auto-link por email. Una coincidencia devuelve 409 hasta implementar flujo explícito y aprobado de vinculación. No habilitar fusión manual insegura para resolverlo.

Docker y PostgreSQL 15/Linux deben verificarse cuando esté disponible el motor; la prueba realizada fue PostgreSQL 18/Windows local. No hay resultado de carga ni promesa de soportar 8190 usuarios.
