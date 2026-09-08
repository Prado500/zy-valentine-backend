# "Mis dedicatorias" (panel posventa) y retome de borradores — Plan de implementación

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Que el frontend pueda mostrar en un panel las cartas terminadas y las compras pagadas cuya carta quedó a medias, y retomar estas últimas desde cero.

**Architecture:** Sin autoguardado y sin migración. El "slot" de quien pagó y abandonó el editor es la compra pagada sin carta; el estado del panel se deriva al leer (`draft` | `published`) en un servicio de dominio puro y se sirve con una sola consulta `LEFT JOIN`. Retomar es el mismo `POST /api/v1/letters` de siempre: sobre un borrador sobrescribe la fila (contenido y fotos nuevas) y publica; sobre una publicada no toca nada.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, Pydantic v2, pytest + pytest-asyncio (integración con PostgreSQL en CI).

---

## Diagnóstico previo

| Pregunta | Hallazgo |
|---|---|
| ¿Existe `status` en `Letter`? | Sí: `draft` / `published` con CHECK en base. Pero `draft` no representa "pagó y abandonó": la fila de carta solo nace al enviar el formulario, y por defecto ya publicada. El abandono es una compra `paid` sin carta. |
| ¿`GET /api/v1/letters` sirve al panel? | Incluye `status` y `purchaseId`, pero no lista compras pagadas sin carta, cuesta 1 + 2N consultas (fotos y entregas por carta) y viaja el cuerpo completo. |
| ¿`PATCH` completa un borrador? | Edita campos y no cambia el estado; publicar exige `POST /letters/{id}/publish`. No acepta `temp_photos`. Reenviar `POST /letters` ignoraba el contenido nuevo (síncrono) o respondía 409 (cola). |
| Hallazgo de seguridad | 🟡 CWE-20: `PATCH` con `"title": null` (o `body`, `recipientName`) moría en el validador heredado con `AttributeError` → 500 `INTERNAL_ERROR`. |

## Decisiones acordadas

- Ruta `GET /api/v1/me/dedications`; vocabulario `draft` | `published`; sin compras `pending` (es posventa).
- Retome "desde cero": `POST /letters` sobre un borrador lo sobrescribe y publica; 409 `LETTER_ALREADY_EXISTS` solo para publicadas (camino con cola). El camino síncrono conserva el 200 idempotente sobre publicadas: contrato del frontend desplegado.
- Las fotos del borrador retomado se reemplazan por las del payload; se conservan las que el payload vuelve a traer (misma clave determinista), para no perderlas en una reentrega del mensaje.
- El `message_id` del retome lleva sufijo único: con el id de la creación, la detección de duplicados de Service Bus descartaría el retome.

## Tareas

### Task 1: Dominio y repositorio (A)

**Files:** Create `app/services/dedications.py`; modify `app/repositories/commerce.py`.

- `dedications.state_of(purchase, letter) -> "draft" | "published" | None` (puro).
- `repo.dedications_statement(user_id)` (`select(Purchase, Letter).outerjoin(...)`, filtro pagada o publicada, orden por `created_at desc`) y `repo.dedications_of_user`.

### Task 2: Esquema, servicio y router (A)

**Files:** Modify `app/schemas/commerce.py` (`DedicationResponse`, `state: Literal[...]`), `app/services/commerce.py` (`list_dedications`, `_dedication_schema`), `app/api/routers/commerce.py` (`GET /me/dedications`).

### Task 3: Retome del borrador (B)

**Files:** Modify `app/services/letters.py` (`content_values`, `create` sobrescribe borradores, `enqueue` solo corta publicadas y usa id único para el retome, `attach_temp_photos(replace=...)` + `discard_stale_photos`), `app/services/commerce.py` (`create_letter` y `fulfil_queued_letter` adjuntan fotos también sobre el borrador retomado, con `replace=True`).

### Task 4: Validadores tolerantes a `null` (C)

**Files:** Modify `app/schemas/commerce.py` (`nonblank`, `clean_body` devuelven `None` sin tocarlo).

### Task 5: Pruebas (Rule of 10)

**Files:** Create `tests/test_dedications.py`, `tests/test_retake_draft.py`, `tests/test_letter_update_schema.py`; modify `tests/test_commerce.py`, `tests/test_sync_dispatch.py`.

- Sin base de datos: derivación de estado (5), SQL compilado (5), `list_dedications` (5), `letters.create` (5), `letters.enqueue` (5), `attach_temp_photos(replace)` (5), orquestación de `CommerceService` (5), `LetterUpdate` con `null` (5).
- Integración (CI): endpoint del panel (5), retome por HTTP y por worker (5), `PATCH` con `null` (1).

Run: `python -m ruff check app tests && python -m ruff format --check app tests && python -m pytest`. Sin `TEST_DATABASE_URL` las de integración se omiten; corren en CI.

### Task 6: Documentación (D)

**Files:** Modify `docs/DOMINIO_COMERCIAL.md` (reglas, IOP #4, contratos, errores, §9), `README.md` (tabla y recorrido), `docs/GUIA_TECNICA.md` (§9.1), `postman/zyvalentine.postman_collection.json` (peticiones del panel).

## Contrato del panel

```jsonc
// GET /api/v1/me/dedications   200
[ { "purchaseId": "uuid", "letterId": "uuid|null", "state": "draft|published",
    "title": "…|null", "recipientName": "…|null", "theme": "…|null",
    "publicSlug": "…|null", "publicUrl": "…|null",
    "paidAt": "…", "publishedAt": "…|null", "updatedAt": "…" } ]
```

Frontend: con `state = "draft"` se abre el editor con `purchaseId` y se envía `POST /api/v1/letters` como hoy; con `published` se enseña `publicUrl`.
