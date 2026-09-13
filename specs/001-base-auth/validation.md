# Validación 001 — resultado final

Fecha: 2026-09-05. Rama develop-julian; sin commits/push. Alcance base + ambas autenticaciones.

## Ejecutado

- Python 3.12, PostgreSQL 18 efímero en loopback, base exclusiva zy_auth_validation. Instancia detenida al finalizar.
- Alembic upgrade head -> check (sin nuevas operaciones/drift) -> downgrade base -> upgrade head: correcto.
- Pytest: **52 passed in 4.92s**, cero fallos. Registro/login/logout/perfil, duplicados, concurrencia, sesión vencida/revocada/desactivada, CSRF, CORS, configuración, límites y Google.
- Google: firmas RSA reales generadas localmente, transporte de certificados simulado; se rechazan firma, audiencia, emisor, expiración y nonce incorrectos. No usa credenciales reales ni demuestra login Google remoto.
- Newman contra Uvicorn local y DB efímera: **14 solicitudes, 20 aserciones, cero fallos**. Carpeta Automated local de la colección importable. Incluye Google sin configurar (503), no login Google real.
- Ruff check y formato aplicados; pip check sin dependencias rotas.
- Docker Compose config --quiet: válido mediante plugin instalado.

## Límites

Docker client 29.4.3 existe, pero motor desktop-linux no responde incluso fuera del aislamiento. No se construyó ni ejecutó imagen. PostgreSQL 15/Linux de Compose pendiente; versión probada PostgreSQL 18/Windows.

No Azure, pipelines, migraciones remotas, frontend ni repositorio de referencia modificados. Iops.md preexistente sin seguimiento se preservó. No client ID Google ni login real; pendiente configuración por compañero/usuario. No prueba de carga, proxies, cookies en navegador cross-site, verificación de email o cédula.

Las dependencias se instalaron en .venv y Newman en .local-validation/tools. Los artefactos locales están ignorados. No secretos en la colección o plantilla de entorno. Datos de pruebas sintéticos; no se usó una DB existente.

## Trazabilidad

| Requisito | Evidencia |
|---|---|
| B01/B02 | tests/test_base.py, validación Compose, ciclo Alembic |
| B03 | Alembic check + tests/test_auth.py |
| A01/A02/A03/A05/A06 | tests/test_auth.py y tests/test_base.py; Newman |
| A04 | tests/test_google_crypto.py, tests/test_auth.py y tests/test_base.py |
| Q01 | postman/zyvalentine.postman_collection.json y scripts/validate_local.py --postman |

Los antecedentes docs/AUDITORIA.md y docs/INTEGRACION_Y_PLAN.md se conservan como diagnóstico/propuesta inicial; README y spec 001 reflejan el estado implementado.
