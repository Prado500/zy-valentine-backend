# Despacho síncrono de cartas (publicar + correo sin Service Bus) — Plan de implementación

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Que `POST /api/v1/letters`, cuando no hay cola configurada, publique la carta y envíe el correo en el mismo acto, igual que hace el worker.

**Architecture:** El bloque "publicar si hace falta y enviar una vez por versión" se extrae a `CommerceService._dispatch` y lo usan los dos caminos (síncrono y `fulfil_queued_letter`). `LetterCreate` gana `autoPublish: bool = True`, el mismo campo que ya viajaba en el sobre de la cola, para conservar el flujo explícito borrador → publicar.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, Pydantic v2, pytest + pytest-asyncio (integración con PostgreSQL en CI).

---

## Diagnóstico (5 Whys)

| Nivel | Pregunta | Respuesta |
|---|---|---|
| 1 | ¿Por qué no llega el correo? | Nunca se llamó a `deliveries.deliver`; `letter_deliveries` vacía. |
| 2 | ¿Por qué? | El camino síncrono de `create_letter` solo creaba la carta y trasladaba fotos; respondía 201 con `status=draft`. |
| 3 | ¿Por qué el frontend no llama a `/publish`? | Se construyó contra el contrato asíncrono: un 201 sin `publicUrl` se trata como "encolada, espera el correo". |
| 4 | ¿Por qué el síncrono no publicaba? | El auto-publicado (IOP #7) se añadió solo al worker (`autoPublish` en el mensaje). |
| 5 | ¿Por qué divergieron? | El bloque publicar+enviar no estaba factorizado; existía solo en `fulfil_queued_letter`. |

**Causa raíz:** orquestación duplicada e incompleta entre el camino síncrono y el del worker.

## Tareas

### Task 1: Esquema

**Files:** Modify `app/schemas/commerce.py` (`LetterCreate`).

- Añadir `autoPublish: bool = True` con comentario del porqué.
- Test (sin base): `tests/test_sync_dispatch.py::test_auto_publish_defaults_to_true_and_can_be_disabled`.

### Task 2: Despacho compartido

**Files:** Modify `app/services/commerce.py` (`create_letter`, `fulfil_queued_letter`, nuevo `_dispatch`).

- `create_letter`: tras `attach_temp_photos`, `await self._dispatch(letter, payload.autoPublish)` también sobre la carta existente (200) para completar intentos rotos.
- `fulfil_queued_letter`: reemplazar el bloque de publicación por `await self._dispatch(letter, bool(message.get("autoPublish", True)))`.
- `_dispatch`: sin correo o con `auto_publish=False` no hace nada; publica si es borrador; no reenvía si ya hay `sent` de la misma versión.

### Task 3: Sobre de la cola

**Files:** Modify `app/services/letters.py` (`enqueue`).

- `build_letter_message(user.id, payload, auto_publish=payload.autoPublish)`.
- Test: `test_queue_envelope_carries_the_requested_auto_publish`.

### Task 4: Pruebas (Rule of 10)

**Files:** Create `tests/test_sync_dispatch.py`; modify `tests/test_commerce.py` (helper con `autoPublish: False`) y `tests/test_async_letters.py` (fallback verifica publicada + enviada).

1. Happy path: 201 publicada, `publicUrl`, `qrUrl`, entrega `sent`, correo con enlace, QR y adjunto.
2. Sad path: sin `recipientEmail` → borrador, sin entrega, sin correo.
3. Borde: `autoPublish=false` → borrador editable, `/publish` explícito sigue funcionando.
4. Borde: reenviar el formulario (200) no manda un segundo correo; un borrador atascado se completa en el reintento.
5. Excepción: `Mailer` que lanza → 201, carta publicada, entrega `failed`, reintento con `/deliveries` funciona.
6. Worker: `fulfil_queued_letter` con `autoPublish=false` deja borrador.

Run: `python -m ruff check . && python -m pytest`. Sin `TEST_DATABASE_URL` las de integración se omiten; corren en CI.

### Task 5: Documentación

**Files:** Modify `docs/DOMINIO_COMERCIAL.md` (tabla IOP y contratos), `docs/GUIA_TECNICA.md` (§9.1 flujo).

## Operación en Azure (DEV)

Variables del App Service para Gmail con contraseña de aplicación: `MAIL_BACKEND=smtp`, `MAIL_HOST=smtp.gmail.com`, `MAIL_PORT=587`, `MAIL_USERNAME=<cuenta gmail>`, `MAIL_PASSWORD=<app password>`, `MAIL_FROM=<misma cuenta>` (opcional), `MAIL_TIMEOUT=20` (opcional). `FRONTEND_URL` debe apuntar a la SWA: es la base del enlace y del QR del correo.

Cartas que quedaron en `draft` antes del fix: `POST /api/v1/letters/{id}/publish` desde Swagger, o reenviar el formulario con la misma compra (el 200 ahora completa el despacho).
