"""Camino por eventos: eager upload, antifraude (IOP #5), traslado (#6) y correo (#7).

Ninguna prueba habla con Azure: el publicador se sustituye por uno que captura el
mensaje, el almacenamiento es el de disco y el correo es el de consola. Lo que se
verifica es el contrato entre las tres piezas —API, cola y worker— no el transporte.
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.errors import ApiError
from app.models.commerce import Letter, LetterPhoto
from app.services.service_bus import LetterPublisher, message_payload
from tests.conftest import new_purchase, service_for

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
LETTER = {
    "title": "Para ti 💌",
    "recipientName": "Ana",
    "recipientEmail": "ana@example.com",
    "body": "Primera línea\nSegunda línea con emoji 🌹",
    "theme": "classic",
}


class CapturingPublisher(LetterPublisher):
    """Cola en memoria: guarda el sobre JSON tal como viajaría a Service Bus."""

    enabled = True

    def __init__(self):
        self.messages: list[dict] = []

    async def publish(self, message: dict, *, message_id: str | None = None) -> bool:
        # Se serializa y se vuelve a leer para detectar cualquier cosa no serializable.
        import json

        self.messages.append(message_payload(json.dumps(message).encode()))
        return True


@pytest.fixture
def queue(app):
    publisher = CapturingPublisher()
    app.state.letter_queue = publisher
    return publisher


async def eager(client, name="mi foto.png", data=PNG):
    return await client.post(
        "/api/v1/letters/photos/eager", files={"file": (name, data, "image/png")}
    )


async def create_letter(client, purchase, **changes):
    return await client.post(
        "/api/v1/letters", json={**LETTER, "purchaseId": purchase["id"], **changes}
    )


# --- Eager upload ---------------------------------------------------------------------


async def test_eager_upload_does_not_touch_the_database(client, buyer, app):
    response = await eager(client)
    assert response.status_code == 201
    body = response.json()
    assert body["fileName"] == "mi foto.png"
    assert body["contentType"] == "image/png"
    assert body["byteSize"] == len(PNG)
    assert body["tempId"].startswith("temporal/")
    # La foto vive en el espacio efímero y no hay ninguna fila de foto todavía.
    async with app.state.sessions() as db:
        assert (await db.scalars(select(LetterPhoto))).all() == []
    assert await app.state.storage.get(f"_temporal/{body['tempId']}") == PNG


async def test_eager_upload_rejects_what_is_not_an_image(client, buyer):
    response = await eager(client, name="malo.png", data=b"no soy una imagen")
    assert response.status_code == 415


async def test_eager_upload_requires_session(app):
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as anonymous:
        token = (await anonymous.get("/api/v1/auth/csrf")).json()["csrfToken"]
        anonymous.headers["X-CSRF-Token"] = token
        assert (await eager(anonymous)).status_code == 401


# --- IOP #5: antifraude antes de encolar ------------------------------------------------


async def test_paid_purchase_is_queued_and_answered_immediately(client, paid_purchase, queue, app):
    photo = (await eager(client)).json()
    response = await create_letter(client, paid_purchase, temp_photos=[photo])
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    # La respuesta llega sin haber escrito la carta: eso es trabajo del worker.
    async with app.state.sessions() as db:
        assert (await db.scalars(select(Letter))).all() == []
    message = queue.messages[0]
    assert message["type"] == "letter.create"
    assert message["tempPhotos"][0]["fileName"] == "mi foto.png"


async def test_unpaid_purchase_never_reaches_the_queue(client, gateway, buyer, queue):
    purchase = await new_purchase(client)
    response = await create_letter(client, purchase)
    assert response.status_code == 409
    assert response.json()["code"] == "PURCHASE_NOT_PAID"
    assert queue.messages == []


async def test_going_back_in_the_browser_does_not_create_a_second_letter(
    client, paid_purchase, queue, app
):
    assert (await create_letter(client, paid_purchase)).status_code == 202
    async with app.state.sessions() as db:
        await service_for(app, db).fulfil_queued_letter(queue.messages[0])
    # El comprador retrocede y reenvía el formulario: la compra ya tiene carta.
    repeated = await create_letter(client, paid_purchase, title="Otra novia")
    assert repeated.status_code == 409
    assert repeated.json()["code"] == "LETTER_ALREADY_EXISTS"
    assert len(queue.messages) == 1


async def test_temp_photo_of_another_account_is_rejected(client, paid_purchase, queue):
    stolen = f"temporal/{uuid.uuid4()}/{uuid.uuid4().hex}.png"
    response = await create_letter(
        client, paid_purchase, temp_photos=[{"tempId": stolen, "fileName": "ajena.png"}]
    )
    assert response.status_code == 403
    assert queue.messages == []


@pytest.mark.parametrize(
    "temp_id",
    ["../../etc/passwd", "temporal/../otra/foto.png", "letters/otra/foto.png", "temporal/x/y.exe"],
)
async def test_malformed_temp_ids_are_rejected(client, paid_purchase, queue, temp_id):
    response = await create_letter(
        client, paid_purchase, temp_photos=[{"tempId": temp_id, "fileName": "x.png"}]
    )
    assert response.status_code == 422
    assert queue.messages == []


# --- IOP #6 y #7: el worker traslada, publica y envía -------------------------------------


async def test_worker_moves_photos_and_sends_the_document(client, paid_purchase, queue, app):
    first = (await eager(client, "recuerdo del viaje.png")).json()
    second = (await eager(client, "segunda.png")).json()
    assert (
        await create_letter(client, paid_purchase, temp_photos=[first, second])
    ).status_code == 202

    async with app.state.sessions() as db:
        letter_id = await service_for(app, db).fulfil_queued_letter(queue.messages[0])

    detail = (await client.get(f"/api/v1/letters/{letter_id}")).json()
    assert detail["status"] == "published"
    # El nombre original viaja hasta `caption` para que el comprador reconozca su foto.
    assert [photo["caption"] for photo in detail["photos"]] == [
        "recuerdo del viaje.png",
        "segunda.png",
    ]
    assert [photo["position"] for photo in detail["photos"]] == [0, 1]
    assert detail["deliveries"][0]["status"] == "sent"

    # La foto está en el permanente y el efímero quedó limpio.
    async with app.state.sessions() as db:
        stored = (await db.scalars(select(LetterPhoto))).all()
    for photo in stored:
        assert photo.storage_key.startswith(f"letters/{letter_id}/")
        assert await app.state.storage.get(photo.storage_key) == PNG
        assert photo.byte_size == len(PNG)
    with pytest.raises(ApiError):
        await app.state.storage.get(f"_temporal/{first['tempId']}")

    # IOP #7: correo con enlace y QR en el cuerpo, y la tarjeta QR en PDF como único adjunto.
    sent = app.state.mailer.sent[-1]
    assert detail["publicUrl"] in sent.html
    assert "data:image/png;base64," not in sent.html  # el QR va como parte relacionada
    assert f'src="cid:{sent.inline[0].cid[1:-1]}"' in sent.html
    assert [item.subtype for item in sent.attachments] == ["pdf"]
    assert any(photo.caption == "recuerdo del viaje.png" for photo in stored)


async def test_redelivered_message_writes_and_sends_only_once(client, paid_purchase, queue, app):
    photo = (await eager(client)).json()
    assert (await create_letter(client, paid_purchase, temp_photos=[photo])).status_code == 202
    for _ in range(3):
        async with app.state.sessions() as db:
            await service_for(app, db).fulfil_queued_letter(queue.messages[0])
    async with app.state.sessions() as db:
        assert len((await db.scalars(select(Letter))).all()) == 1
        assert len((await db.scalars(select(LetterPhoto))).all()) == 1
    assert len(app.state.mailer.sent) == 1


async def test_queue_failure_falls_back_to_the_synchronous_path(client, paid_purchase, app):
    class BrokenPublisher(LetterPublisher):
        enabled = True

        async def publish(self, message, *, message_id=None):
            raise RuntimeError("Service Bus no responde")

    app.state.letter_queue = BrokenPublisher()
    response = await create_letter(client, paid_purchase)
    # La carta no se pierde: se escribe en el acto, como antes de existir la cola, y
    # se despacha igual que lo haría el worker: publicada y con el correo enviado.
    assert response.status_code == 201
    body = response.json()
    assert body["title"] == LETTER["title"]
    assert body["status"] == "published"
    assert body["publicUrl"]
    assert [item["status"] for item in body["deliveries"]] == ["sent"]
    assert app.state.mailer.sent[-1].to == LETTER["recipientEmail"]
