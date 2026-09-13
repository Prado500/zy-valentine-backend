"""Traslación de fotos del contenedor efímero al permanente (IOP #6).

Rule of 10 sobre ``letters.attach_temp_photos``, ejecutada como la ejecuta el
worker: a través de ``CommerceService.fulfil_queued_letter``. Es el punto donde una
foto pagada puede perderse, así que aquí están los casos raros —reentrega, blob ya
borrado, almacenamiento caído— y no solo el feliz.
"""

import uuid

import pytest
from sqlalchemy import func, select

from app.core.errors import ApiError
from app.models.commerce import Letter, LetterPhoto
from app.schemas.commerce import LetterCreate, TempPhotoRef
from app.services.service_bus import build_letter_message
from tests.conftest import service_for

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


async def upload(client, name="recuerdo.png", data=PNG):
    response = await client.post(
        "/api/v1/letters/photos/eager", files={"file": (name, data, "image/png")}
    )
    assert response.status_code == 201, response.text
    return response.json()


def message_for(purchase, user_id, photos, **changes):
    """Sobre idéntico al que publica la API cuando la cola está activa."""
    payload = LetterCreate(
        purchaseId=purchase["id"],
        title="Para ti",
        recipientName="Ana",
        recipientEmail="ana@example.com",
        body="Hola",
        theme="classic",
        temp_photos=[
            TempPhotoRef(tempId=photo["tempId"], fileName=photo["fileName"]) for photo in photos
        ],
    )
    return {**build_letter_message(user_id, payload), **changes}


@pytest.fixture
async def buyer_id(app, buyer):
    from app.repositories import users

    async with app.state.sessions() as db:
        return (await users.by_email(db, buyer["email"])).id


# --- Los cinco casos -----------------------------------------------------------------------


async def test_happy_path_moves_every_photo_and_keeps_its_original_name(
    client, paid_purchase, app, buyer_id
):
    first = await upload(client, "recuerdo del viaje.png")
    second = await upload(client, "segunda.png", PNG + b"2")
    message = message_for(paid_purchase, buyer_id, [first, second])

    async with app.state.sessions() as db:
        letter_id = await service_for(app, db).fulfil_queued_letter(message)

    detail = (await client.get(f"/api/v1/letters/{letter_id}")).json()
    assert [photo["caption"] for photo in detail["photos"]] == [
        "recuerdo del viaje.png",
        "segunda.png",
    ]
    assert [photo["position"] for photo in detail["photos"]] == [0, 1]
    assert [photo["byteSize"] for photo in detail["photos"]] == [len(PNG), len(PNG) + 1]
    # El permanente tiene los bytes y el efímero quedó limpio.
    async with app.state.sessions() as db:
        stored = list(await db.scalars(select(LetterPhoto)))
    assert await app.state.storage.get(stored[0].storage_key) == PNG
    with pytest.raises(ApiError):
        await app.state.storage.get(f"_temporal/{first['tempId']}")


async def test_sad_path_a_photo_from_another_account_is_refused(
    client, paid_purchase, app, buyer_id
):
    """La propiedad se comprueba también aquí, no solo en la API que encoló."""
    stolen = {
        "tempId": f"temporal/{uuid.uuid4()}/{uuid.uuid4().hex}.png",
        "fileName": "ajena.png",
    }
    message = message_for(paid_purchase, buyer_id, [stolen])

    async with app.state.sessions() as db:
        with pytest.raises(ApiError) as error:
            await service_for(app, db).fulfil_queued_letter(message)

    assert error.value.status_code == 403
    assert error.value.detail["code"] == "TEMP_PHOTO_FORBIDDEN"


async def test_edge_more_photos_than_allowed_are_trimmed_not_rejected(
    client, paid_purchase, app, buyer_id
):
    """Frontera de ``MAX_PHOTOS_PER_LETTER``: entran las primeras, no falla el mensaje."""
    app.state.settings.max_photos_per_letter = 2
    photos = [await upload(client, f"foto-{index}.png", PNG + bytes([index])) for index in range(4)]
    message = message_for(paid_purchase, buyer_id, photos)

    async with app.state.sessions() as db:
        letter_id = await service_for(app, db).fulfil_queued_letter(message)

    detail = (await client.get(f"/api/v1/letters/{letter_id}")).json()
    assert [photo["caption"] for photo in detail["photos"]] == ["foto-0.png", "foto-1.png"]


async def test_edge_a_blob_deleted_before_the_move_does_not_lose_the_letter(
    client, paid_purchase, app, buyer_id
):
    """Concurrencia: alguien borró el temporal entre el encolado y el traslado.

    Reintentar no lo traería de vuelta. La carta —pagada— sale igual, sin esa foto,
    en vez de morir en la dead-letter queue con el dinero ya cobrado.
    """
    survivor = await upload(client, "sobrevive.png")
    lost = await upload(client, "perdida.png", PNG + b"9")
    await app.state.storage.delete(f"_temporal/{lost['tempId']}")
    message = message_for(paid_purchase, buyer_id, [survivor, lost])

    async with app.state.sessions() as db:
        letter_id = await service_for(app, db).fulfil_queued_letter(message)

    detail = (await client.get(f"/api/v1/letters/{letter_id}")).json()
    assert [photo["caption"] for photo in detail["photos"]] == ["sobrevive.png"]
    assert detail["status"] == "published"
    assert detail["deliveries"][0]["status"] == "sent"


async def test_edge_a_redelivery_after_moving_the_blob_still_records_the_photo(
    client, paid_purchase, app, buyer_id, mocker
):
    """El caso feo: el blob se movió, la fila no llegó a guardarse y el mensaje vuelve.

    Como el destino es determinista, el segundo intento reconoce lo ya movido y
    registra la foto en vez de perderla.
    """
    photo = await upload(client, "unica.png")
    message = message_for(paid_purchase, buyer_id, [photo])

    # Primer intento: la carta se guarda, el blob se mueve y la base cae justo en el
    # commit de las fotos. El primer commit es real; solo el segundo falla.
    async with app.state.sessions() as db:
        real_commit = db.commit
        attempts = {"count": 0}

        async def flaky_commit():
            attempts["count"] += 1
            if attempts["count"] == 2:
                raise RuntimeError("la base se cayó")
            return await real_commit()

        mocker.patch.object(db, "commit", side_effect=flaky_commit)
        with pytest.raises(RuntimeError):
            await service_for(app, db).fulfil_queued_letter(message)

    # El blob ya no está en el temporal, pero tampoco hay fila que lo registre.
    with pytest.raises(ApiError):
        await app.state.storage.get(f"_temporal/{photo['tempId']}")
    assert await _stored_photos(app) == []

    # Segundo intento, ya sin fallos: la foto aparece con sus bytes intactos porque el
    # destino es determinista y `move_blob` reconoce lo que ya se movió.
    async with app.state.sessions() as db:
        letter_id = await service_for(app, db).fulfil_queued_letter(message)

    detail = (await client.get(f"/api/v1/letters/{letter_id}")).json()
    assert [item["caption"] for item in detail["photos"]] == ["unica.png"]
    stored = await _stored_photos(app)
    assert await app.state.storage.get(stored[0].storage_key) == PNG


async def test_exception_a_storage_failure_is_transient_and_leaves_no_half_letter(
    client, paid_purchase, app, buyer_id, mocker
):
    """Azure caído a mitad del traslado: la excepción sube y no queda ninguna fila de foto."""
    photo = await upload(client)
    message = message_for(paid_purchase, buyer_id, [photo])
    mocker.patch.object(
        app.state.storage, "move_blob", side_effect=RuntimeError("ServiceRequestError")
    )

    async with app.state.sessions() as db:
        with pytest.raises(RuntimeError):
            await service_for(app, db).fulfil_queued_letter(message)

    assert await _stored_photos(app) == []
    # La carta sí existe (su commit es previo) y sigue sin publicar: al reintentar el
    # mensaje, la idempotencia la reutiliza y termina el trabajo.
    async with app.state.sessions() as db:
        letter = await db.scalar(select(Letter))
    assert letter is not None and letter.status == "draft"


# --- Idempotencia de la reentrega -------------------------------------------------------------


async def test_three_deliveries_of_the_same_message_produce_one_letter_and_one_email(
    client, paid_purchase, app, buyer_id
):
    photo = await upload(client)
    message = message_for(paid_purchase, buyer_id, [photo])

    for _ in range(3):
        async with app.state.sessions() as db:
            await service_for(app, db).fulfil_queued_letter(message)

    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Letter)) == 1
        assert await db.scalar(select(func.count()).select_from(LetterPhoto)) == 1
    assert len(app.state.mailer.sent) == 1


# --- Mensajes que el worker no debe aceptar -----------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"type": "otra.cosa"}, "UNKNOWN_MESSAGE_TYPE"),
        ({"version": 99}, "UNKNOWN_MESSAGE_VERSION"),
        ({"letter": {"title": ""}}, "INVALID_MESSAGE"),
        ({"userId": "no-es-un-uuid"}, "INVALID_MESSAGE"),
    ],
)
async def test_a_message_with_an_unexpected_shape_is_permanent(
    client, paid_purchase, app, buyer_id, changes, code
):
    message = message_for(paid_purchase, buyer_id, [], **changes)

    async with app.state.sessions() as db:
        with pytest.raises(ApiError) as error:
            await service_for(app, db).fulfil_queued_letter(message)

    assert error.value.detail["code"] == code
    # 422 está en PERMANENT_STATUS: el worker lo manda a la dead-letter queue.
    assert error.value.status_code == 422


async def test_a_message_from_a_deactivated_buyer_is_permanent(
    client, paid_purchase, app, buyer_id
):
    message = message_for(paid_purchase, uuid.uuid4(), [])

    async with app.state.sessions() as db:
        with pytest.raises(ApiError) as error:
            await service_for(app, db).fulfil_queued_letter(message)

    assert error.value.status_code == 404
    assert error.value.detail["code"] == "USER_UNAVAILABLE"


async def _stored_photos(app) -> list[LetterPhoto]:
    async with app.state.sessions() as db:
        return list(await db.scalars(select(LetterPhoto)))
