# Auditoría del backend Zyvencore Valentine

Fecha: 2026-09-05. Repositorio: zy-valentine-backend.
Base inspeccionada: 1880b35 (chore(code-baseline): implement iteration 0 for project zyvalentine backend).
Alcance: inspección estática y propuesta documental, sin implementación.

Leer junto con [Contrato y plan conjunto](INTEGRACION_Y_PLAN.md), copia idéntica de la documentación del frontend. Allí están contratos JSON, recuperación de navegación, identidad/cédula, cardinalidad compra-carta, estados, correo y matriz de aceptación.

## Estado comprobado

El propósito declarado es una API para cartas personalizadas. Actualmente es una base inicial: [app/main.py](../app/main.py) crea FastAPI y responde GET /. No hay routers de negocio registrados, esquemas, modelos ni repositorios implementados.

[pyproject.toml](../pyproject.toml) declara Python >=3.11, FastAPI/Uvicorn, SQLAlchemy/asyncpg, Alembic, Pydantic/settings, bcrypt/PyJWT, correo y Azure Blob. Declarar dependencias no implementa esas capacidades. Las dependencias principales no están fijadas por un lockfile identificado. El extra de pruebas se llama test; el README indica dev, inconsistencia a corregir.

| Área | Evidencia y límite |
|---|---|
| Salud | GET / devuelve estado ok sin comprobar DB ni almacenamiento |
| Configuración | app/core vacío; .env.example no implica lectura/validación en aplicación |
| Persistencia | app/db y app/models solo contienen __init__.py vacío |
| API/servicios | dependencies.py y capas de routers, schemas, repositories y services vacías |
| Auth | Sin verificación Google, usuarios, sesiones ni comprobación de propietario |
| Migraciones | [alembic/env.py](../alembic/env.py) importa app.models.base y app.models.reserva inexistentes |
| Tests | tests/__init__.py vacío; no suite real pese a descripción aspiracional del README |
| Contenedores | Compose configura PostgreSQL 15 y API; no prueba de arranque realizada |
| Pago/correo/media | Sin implementación; ningún proveedor conectado/verificado |

Los metadatos FastAPI todavía dicen Ecotur-ASOPRADO y paquetes turísticos. CORS permite origen comodín con credenciales: debe diseñarse con orígenes explícitos al introducir sesiones. El README afirma necesidad de DB al iniciar/crear tablas, pero main.py no realiza esas operaciones.

Dockerfile copia el contexto completo e instala dependencias de test. No se identificó .dockerignore. Antes de usarlo conviene excluir .env, .git y artefactos, instalar solo runtime y separar checks de desarrollo. Compose incluye contraseña de desarrollo por defecto y expone PostgreSQL; revisar según entorno, sin tratarlo como configuración de producción. Los ejemplos de configuración conservan referencias ajenas y valores no utilizables; reemplazarlos por placeholders coherentes al implementar, sin asumir que son credenciales reales.

## Arquitectura propuesta y propiedad

Conservar un backend modular, no dividir en microservicios. Proponer módulos identity, purchases, cards, media y deliveries sobre las capas existentes. Cada servicio define una transacción completa para sus invariantes; dependencias de router suministran sesión y usuario, no lógica de negocio.

- PostgreSQL conserva comprador, documento protegido, compra, borrador, carta, versión y entrega.
- Almacenamiento de objetos conserva fotos privadas de borrador y medios autorizados del visor.
- Un trabajador procesa outbox duradera; una tarea en memoria no basta para entrega reintentable.
- Settings valida configuración por entorno; health distingue liveness de readiness, sin filtrar secretos.
- Pydantic/OpenAPI define contrato y validación de ejecución, con generación de tipos frontend.
- Logs estructurados usan requestId y IDs internos, evitando cédula, texto íntimo, email completo y enlaces secretos.
- Pruebas de integración de concurrencia deben ejecutarse con PostgreSQL: SQLite no demuestra las mismas garantías de bloqueos.

No hay esquemas backend existentes que preservar; DedicationForm del frontend es el punto de partida, no el modelo de persistencia final.

## Una compra pagada por carta

Aplicar el modelo detallado en el plan conjunto: purchases 1:N por usuario; cards.purchase_id NOT NULL UNIQUE y FK; borrador único de la compra; propietario consistente mediante FKs compuestas. Guardar la relación aunque se archive/revoque la carta. Evitar UNIQUE(user_id) en cartas.

Bloquear compra durante publicación y transiciones de pago. Validar paid confirmado por servidor, pertenencia, revision y medios. Insertar carta/versión/enlace/intención/outbox atómicamente. Con claves distintas, la restricción de compra sigue impidiendo segunda carta; con la misma clave, devolver resultado anterior o rechazar cuerpo diferente. Reenvío no participa en asignación de derechos.

No basta con JWT, un botón deshabilitado o comprobar paid antes de abrir una transacción. Pago debe verificarse con proveedor, importe, moneda y cuenta comercial. Retorno del navegador no prueba pago. Eventos duplicados/fuera de orden y timeout de checkout requieren reconciliación y pruebas específicas del proveedor elegido.

Recomendación aún pendiente: congelar contenido/destinatario al publicar, evitando reutilizar una carta pagada para regalos sucesivos mediante edición. Toda excepción tendrá alcance/auditoría definidos; no asumir aceptada la conservación de versión entregada.

## Recuperación y contratos

Crear borrador asociado a compra y comprador al iniciar flujo estable. GET/PUT reanudan usando revision, createdAt inmutable y updatedAt. Un timeout de guardado se resuelve consultando resultado o repitiendo clave; no recreando compra.

El frontend hoy solo conserva pasos en React, publica localmente y no tiene checkout. Para atrás del navegador, recarga o retorno del proveedor, API debe poder recuperar compra/borrador sin depender de memoria del navegador. Mis cartas lista exclusivamente objetos del usuario autenticado; la cédula sirve como dato de negocio privado, nunca como autorización.

Separar DTO de guardado con mediaId/position del DTO público con URL de medio. No recibir owner del cliente, URLs blob, estado de compra ni precio como autoridad. Correo destino es dato privado de entrega; el botón y QR del correo apuntan a la misma publicación. SchemaVersion y validación se describen en el plan.

## Mejoras y orden de ejecución futuro

1. Limpiar metadatos, README/configuración e importaciones; crear Base/engine/sesión y migraciones reales del dominio. Validar arranque y migración vacía sin datos reales.
2. Fijar contratos, errores, índices/FKs, protocolo transaccional y política de documento. Validar configuración y dependencias reproducibles.
3. Implementar Google/sesión, ownership, documento y borradores con revisión. Probar acceso ajeno, sesión expirada y varias pestañas.
4. Implementar compras/checkout/webhooks en sandbox; persistir eventos e intentos y reconciliar respuestas inciertas.
5. Implementar medios y publicación atómica, con acceso público secreto y política de versión aprobada.
6. Implementar biblioteca, outbox/correo y reenvío idempotente, con métricas de fallos/reintentos sin PII.
7. Ejecutar matriz conjunta y pruebas de carrera, restarts y fallos externos; documentar evidencia y límites antes de plantear producción.

## Límites de validación

Solo inspección estática y comprobación documental. No instalación, importación/arranque de API, Docker, migraciones, pruebas de DB ni llamadas a producción. No tests ejecutados: no existe suite funcional en el repositorio. No AGENTS.md aplicables encontrados. Git estaba limpio antes de añadir documentación.

La ausencia de modelos importados demuestra un defecto de configuración de migraciones por lectura, pero no se reporta una ejecución fallida que no se realizó. No hay auditoría exhaustiva de seguridad ni garantía comercial.

