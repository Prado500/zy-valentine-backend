"""Eager upload: la foto sube antes de que la carta exista.

Rule of 10 sobre ``POST /api/v1/letters/photos/eager``. Es el único endpoint que
acepta archivos sin que haya todavía una carta a la que atarlos, así que es el que
más disciplina necesita: valida por bytes, no por lo que declare el navegador, y no
gasta ni una escritura en PostgreSQL.
"""

import asyncio

import pytest
from sqlalchemy import func, select

from app.models.commerce import LetterPhoto
from app.services.storage import temp_key_owner

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
JPEG = b"\xff\xd8\xff" + b"0" * 64
WEBP = b"RIFF" + b"0000" + b"WEBP" + b"0" * 64


async def eager(client, name="mi foto.png", data=PNG, declared="image/png"):
    return await client.post("/api/v1/letters/photos/eager", files={"file": (name, data, declared)})


# --- Los cinco casos ---------------------------------------------------------------------


async def test_happy_path_stores_the_photo_without_touching_the_database(client, buyer, app):
    response = await eager(client)

    assert response.status_code == 201
    body = response.json()
    assert body == {
        "tempId": body["tempId"],
        "fileName": "mi foto.png",
        "contentType": "image/png",
        "byteSize": len(PNG),
    }
    assert await app.state.storage.get(f"_temporal/{body['tempId']}") == PNG
    # Cero escrituras: mientras el comprador elige fotos no se gasta un solo IOP.
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(LetterPhoto)) == 0


@pytest.mark.parametrize(
    ("data", "declared", "expected"),
    [
        (b"", "image/png", 422),  # archivo vacío
        (b"<script>alert(1)</script>", "image/png", 415),  # MIME falsificado
        (b"GIF89a" + b"0" * 64, "image/gif", 415),  # formato no admitido
        (b"%PDF-1.7", "image/jpeg", 415),  # documento disfrazado de foto
    ],
)
async def test_sad_path_rejects_what_is_not_a_supported_image(
    client, buyer, data, declared, expected
):
    """El tipo lo deciden los bytes. Declarar `image/png` no convierte nada en imagen."""
    response = await eager(client, data=data, declared=declared)

    assert response.status_code == expected
    assert "code" in response.json()


async def test_edge_the_exact_size_limit_is_accepted_and_one_byte_more_is_not(client, buyer, app):
    """Frontera exacta de ``MAX_PHOTO_BYTES``, por arriba y por abajo."""
    app.state.settings.max_photo_bytes = 2048
    header = b"\x89PNG\r\n\x1a\n"

    exact = await eager(client, data=header + b"0" * (2048 - len(header)))
    over = await eager(client, data=header + b"0" * (2049 - len(header)))

    assert exact.status_code == 201
    assert exact.json()["byteSize"] == 2048
    assert over.status_code == 413


async def test_edge_simultaneous_uploads_never_share_a_key(client, buyer, app):
    """Doce subidas a la vez: doce claves distintas y doce archivos íntegros."""
    responses = await asyncio.gather(*(eager(client, data=PNG + bytes([i])) for i in range(12)))

    assert {response.status_code for response in responses} == {201}
    keys = [response.json()["tempId"] for response in responses]
    assert len(set(keys)) == 12
    for index, key in enumerate(keys):
        assert await app.state.storage.get(f"_temporal/{key}") == PNG + bytes([index])


async def test_exception_a_storage_failure_answers_without_leaking_internals(
    client, buyer, app, mocker
):
    """Azure caído: 500 con el formato de error de siempre y sin rastro de la excepción.

    El transporte se monta con ``raise_app_exceptions=False`` para ver lo que recibe
    el navegador. Starlette entrega la respuesta del manejador y **además** relanza la
    excepción para que el servidor la registre; en producción eso acaba en el log de
    Uvicorn, no en la respuesta.
    """
    import httpx

    mocker.patch.object(
        app.state.storage,
        "put_temp",
        side_effect=RuntimeError("ServiceRequestError: connection to blob.core.windows.net"),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
        cookies=client.cookies,
        headers=client.headers,
    ) as watcher:
        response = await eager(watcher)

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "INTERNAL_ERROR"
    # Ni el proveedor, ni el host, ni el tipo de excepción llegan al cliente.
    assert "blob.core.windows.net" not in response.text
    assert "RuntimeError" not in response.text
    assert body["requestId"]  # queda la traza para correlacionar con el log


# --- Propiedad y forma de la clave efímera --------------------------------------------------


async def test_the_key_belongs_to_whoever_uploaded_it(client, buyer, app):
    """Sin esto, una carta podría reclamar la foto que subió otra cuenta."""
    key = (await eager(client)).json()["tempId"]

    async with app.state.sessions() as db:
        from app.repositories import users

        owner = await users.by_email(db, buyer["email"])
    assert temp_key_owner(key) == str(owner.id)


@pytest.mark.parametrize(
    ("data", "declared", "expected_type"),
    [
        (PNG, "image/png", "image/png"),
        (JPEG, "image/jpeg", "image/jpeg"),
        (WEBP, None, "image/webp"),
    ],
)
async def test_every_supported_format_keeps_its_type_in_the_key(
    client, buyer, data, declared, expected_type
):
    response = await eager(client, data=data, declared=declared)

    assert response.status_code == 201
    assert response.json()["contentType"] == expected_type


async def test_a_hostile_file_name_is_cleaned_before_being_stored(client, buyer):
    """El nombre acaba en `caption`, que se muestra: entra limpio de controles."""
    response = await eager(client, name="foto\r\n\x00 rara.png")

    assert response.status_code == 201
    name = response.json()["fileName"]
    assert "\x00" not in name and "\r" not in name


async def test_without_a_session_nothing_can_be_uploaded(app):
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as anonymous:
        token = (await anonymous.get("/api/v1/auth/csrf")).json()["csrfToken"]
        anonymous.headers["X-CSRF-Token"] = token
        assert (await eager(anonymous)).status_code == 401


async def test_without_the_csrf_header_nothing_can_be_uploaded(app, buyer):
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as bare:
        assert (await eager(bare)).status_code == 403
