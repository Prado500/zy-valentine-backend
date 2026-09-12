"""Retomar un borrador desde "Mis dedicatorias": ``POST /api/v1/letters`` sobre una
compra que ya tiene carta en borrador.

Regla del producto: no hay autoguardado. Lo que llega al retomar es la carta entera y
sustituye lo que quedó a medias: mismo ``letters.id``, contenido nuevo, fotos nuevas, y
se publica en el mismo acto. Sigue habiendo exactamente una carta por compra.

Una carta **publicada** no se toca: el camino síncrono devuelve la misma con 200 y sin
segundo correo (el contrato del frontend desplegado), y el de la cola responde 409
``LETTER_ALREADY_EXISTS`` antes de encolar nada.

Las pruebas de dominio corren sin PostgreSQL, con una sesión y un almacenamiento
falsos; las de integración (HTTP y worker) corren en CI.
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.errors import ApiError
from app.models.commerce import Letter, LetterDelivery, LetterPhoto, Purchase
from app.models.user import User
from app.schemas.commerce import LetterCreate, TempPhotoRef
from app.services import letters
from app.services.commerce import CommerceService
from app.services.mailer import ConsoleMailer
from app.services.service_bus import (
    LetterPublisher,
    MockPublisher,
    build_letter_message,
    message_payload,
)
from tests.conftest import service_for

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
LETTER = {
    "title": "Para ti 💌",
    "recipientName": "Ana",
    "recipientEmail": "ana@example.com",
    "body": "Primera línea\nSegunda línea con emoji 🌹",
    "theme": "classic",
}
# Lo que el comprador escribe al retomar: todo distinto, para ver que nada viejo sobrevive.
NEW_CONTENT = {
    "title": "Segunda vuelta",
    "recipientName": "Lucía",
    "recipientEmail": "lucia@example.com",
    "body": "Texto nuevo\ndesde cero",
    "theme": "modern",
}


# --- Dobles sin base de datos ----------------------------------------------------------------


class FakeSession:
    """Sesión mínima para el dominio: anota lo que se le pide y nunca abre PostgreSQL."""

    def __init__(self, user: User | None = None):
        self.user = user
        self.commits = 0
        self.deleted: list = []
        self.added: list = []
        self.refreshed: list = []

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None

    async def refresh(self, instance):
        self.refreshed.append(instance)

    async def delete(self, instance):
        self.deleted.append(instance)

    def add_all(self, instances):
        self.added.extend(instances)

    async def get(self, model, key):
        return self.user if model is User else None


class FakeStorage:
    """Almacenamiento que solo anota traslados y borrados."""

    def __init__(self):
        self.moved: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.missing: set[str] = set()
        self.delete_error: Exception | None = None

    async def move_blob(self, source_key: str, dest_key: str) -> int:
        if source_key in self.missing:
            raise ApiError(404, "TEMP_PHOTO_NOT_FOUND", "La foto temporal ya no existe.")
        self.moved.append((source_key, dest_key))
        return 64

    async def delete(self, key: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(key)


class RecordingPublisher(LetterPublisher):
    """Cola en memoria que además recuerda el ``message_id`` de cada publicación."""

    enabled = True

    def __init__(self, accept: bool = True):
        self.accept = accept
        self.messages: list[dict] = []
        self.ids: list[str | None] = []

    async def publish(self, message: dict, *, message_id: str | None = None) -> bool:
        # Ida y vuelta por JSON: lo que llega al worker es lo que viajaría de verdad.
        self.messages.append(message_payload(json.dumps(message).encode()))
        self.ids.append(message_id)
        return self.accept


def buyer() -> User:
    return User(id=uuid.uuid4(), email="comprador@example.com", is_active=True)


def purchase_of(owner: User, status: str = "paid") -> Purchase:
    return Purchase(
        id=uuid.uuid4(),
        user_id=owner.id,
        status=status,
        amount_cents=1500000,
        currency="COP",
        external_reference=f"zv-{uuid.uuid4().hex}",
        idempotency_key="idem-key-0001",
        expires_at=datetime.now(UTC),
    )


def letter_of(
    purchase: Purchase, status: str = "draft", recipient_email="ana@example.com"
) -> Letter:
    now = datetime.now(UTC)
    return Letter(
        id=uuid.uuid4(),
        purchase_id=purchase.id,
        user_id=purchase.user_id,
        public_slug="slug-viejo",
        status=status,
        title="Título viejo",
        recipient_name="Ana",
        recipient_email=recipient_email,
        body="Cuerpo viejo",
        theme="classic",
        published_version=1 if status == "published" else 0,
        created_at=now,
        updated_at=now,
    )


def payload_for(purchase: Purchase, **changes) -> LetterCreate:
    return LetterCreate(purchaseId=purchase.id, **{**NEW_CONTENT, **changes})


def temp_ref(owner: User, name: str = "nueva.png") -> TempPhotoRef:
    return TempPhotoRef(tempId=f"temporal/{owner.id}/{uuid.uuid4().hex}.png", fileName=name)


def stored_photo(letter: Letter, key: str, position: int) -> LetterPhoto:
    return LetterPhoto(
        id=uuid.uuid4(),
        letter_id=letter.id,
        position=position,
        storage_key=key,
        content_type="image/png",
        byte_size=64,
        caption=f"vieja-{position}.png",
    )


def repo_with(monkeypatch, purchase: Purchase | None, letter: Letter | None):
    """El repositorio responde con esta compra y esta carta, sin consultar nada."""

    async def lock_purchase(db, purchase_id):
        return purchase if purchase is not None and purchase.id == purchase_id else None

    async def owned_purchase(db, purchase_id, user_id):
        if purchase is None or purchase.id != purchase_id or purchase.user_id != user_id:
            return None
        return purchase

    async def letter_of_purchase(db, purchase_id):
        return letter

    monkeypatch.setattr(letters.commerce, "lock_purchase", lock_purchase)
    monkeypatch.setattr(letters.commerce, "owned_purchase", owned_purchase)
    monkeypatch.setattr(letters.commerce, "letter_of_purchase", letter_of_purchase)


def photos_returning(monkeypatch, photos: list[LetterPhoto]):
    async def photos_of_letter(db, letter_id):
        return list(photos)

    monkeypatch.setattr(letters.commerce, "photos_of_letter", photos_of_letter)


# --- `letters.create`: el borrador se sobrescribe, la publicada no ------------------------


async def test_happy_path_retaking_a_draft_overwrites_the_same_row(settings, monkeypatch):
    owner = buyer()
    purchase = purchase_of(owner)
    draft = letter_of(purchase)
    repo_with(monkeypatch, purchase, draft)
    db = FakeSession()

    letter, created = await letters.create(db, settings, owner, payload_for(purchase))

    assert created is False
    assert letter is draft  # misma fila: una compra, una carta
    assert letter.title == "Segunda vuelta"
    assert letter.recipient_name == "Lucía"
    assert letter.recipient_email == "lucia@example.com"
    assert letter.body == "Texto nuevo\ndesde cero"
    assert letter.theme == "modern"
    # Publicar es cosa del despacho, y el slug no cambia: no hay enlaces viejos que romper.
    assert letter.status == "draft"
    assert letter.public_slug == "slug-viejo"
    assert db.commits == 1
    assert db.refreshed == [draft]


async def test_sad_path_a_published_letter_is_returned_untouched(settings, monkeypatch):
    owner = buyer()
    purchase = purchase_of(owner)
    published = letter_of(purchase, status="published")
    repo_with(monkeypatch, purchase, published)
    db = FakeSession()

    letter, created = await letters.create(db, settings, owner, payload_for(purchase))

    assert (letter, created) == (published, False)
    assert letter.title == "Título viejo"
    assert letter.body == "Cuerpo viejo"
    assert db.commits == 0


async def test_edge_overwriting_is_total_not_partial(settings, monkeypatch):
    """Si el payload nuevo no trae correo, el correo viejo no se conserva; y se normaliza."""
    owner = buyer()
    purchase = purchase_of(owner)
    draft = letter_of(purchase, recipient_email="ana@example.com")
    repo_with(monkeypatch, purchase, draft)

    await letters.create(FakeSession(), settings, owner, payload_for(purchase, recipientEmail=None))
    assert draft.recipient_email is None

    await letters.create(
        FakeSession(), settings, owner, payload_for(purchase, recipientEmail="LUCIA@Example.com")
    )
    assert draft.recipient_email == "lucia@example.com"


async def test_edge_a_draft_of_another_account_cannot_be_retaken(settings, monkeypatch):
    owner, intruder = buyer(), buyer()
    purchase = purchase_of(owner)
    draft = letter_of(purchase)
    repo_with(monkeypatch, purchase, draft)
    db = FakeSession()

    with pytest.raises(ApiError) as error:
        await letters.create(db, settings, intruder, payload_for(purchase))

    assert error.value.status_code == 404
    assert error.value.detail["code"] == "PURCHASE_NOT_FOUND"
    assert draft.title == "Título viejo"
    assert db.commits == 0


async def test_exception_an_unpaid_purchase_cannot_be_retaken(settings, monkeypatch):
    owner = buyer()
    purchase = purchase_of(owner, status="pending")
    draft = letter_of(purchase)
    repo_with(monkeypatch, purchase, draft)
    db = FakeSession()

    with pytest.raises(ApiError) as error:
        await letters.create(db, settings, owner, payload_for(purchase))

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "PURCHASE_NOT_PAID"
    assert draft.title == "Título viejo"
    assert db.commits == 0


# --- `letters.enqueue`: el retome viaja por la cola; la publicada se corta antes ----------


async def test_happy_path_a_draft_is_enqueued_with_its_own_message_id(settings, monkeypatch):
    owner = buyer()
    purchase = purchase_of(owner)
    repo_with(monkeypatch, purchase, letter_of(purchase))
    queue = RecordingPublisher()

    queued = await letters.enqueue(FakeSession(), settings, queue, owner, payload_for(purchase))

    assert queued is True
    assert queue.messages[0]["letter"]["title"] == "Segunda vuelta"
    # Un id distinto al del primer envío: la detección de duplicados de Service Bus no
    # debe descartar el retome como si fuera el doble clic de la creación.
    assert queue.ids[0] != f"letter-{purchase.id}"
    assert queue.ids[0].startswith(f"letter-{purchase.id}-")


async def test_sad_path_a_published_letter_is_refused_before_enqueueing(settings, monkeypatch):
    owner = buyer()
    purchase = purchase_of(owner)
    repo_with(monkeypatch, purchase, letter_of(purchase, status="published"))
    queue = RecordingPublisher()

    with pytest.raises(ApiError) as error:
        await letters.enqueue(FakeSession(), settings, queue, owner, payload_for(purchase))

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "LETTER_ALREADY_EXISTS"
    assert queue.messages == []


async def test_edge_the_first_submission_keeps_the_deduplicating_id(settings, monkeypatch):
    """Sin carta previa el id sigue derivado de la compra: dos pestañas encolan una sola."""
    owner = buyer()
    purchase = purchase_of(owner)
    repo_with(monkeypatch, purchase, None)
    queue = RecordingPublisher()

    assert await letters.enqueue(FakeSession(), settings, queue, owner, payload_for(purchase))
    assert queue.ids == [f"letter-{purchase.id}"]


async def test_edge_too_many_photos_are_refused_before_enqueueing(settings, monkeypatch):
    owner = buyer()
    purchase = purchase_of(owner)
    repo_with(monkeypatch, purchase, letter_of(purchase))
    settings.max_photos_per_letter = 1
    queue = RecordingPublisher()
    payload = payload_for(purchase, temp_photos=[temp_ref(owner), temp_ref(owner)])

    with pytest.raises(ApiError) as error:
        await letters.enqueue(FakeSession(), settings, queue, owner, payload)

    assert error.value.detail["code"] == "PHOTO_LIMIT_REACHED"
    assert queue.messages == []


async def test_exception_transport_failures_are_not_swallowed_here(settings, monkeypatch):
    """El transporte que falla sube tal cual: es el llamador quien cae al camino síncrono."""
    owner = buyer()
    purchase = purchase_of(owner)
    repo_with(monkeypatch, purchase, letter_of(purchase))

    class BrokenPublisher(LetterPublisher):
        enabled = True

        async def publish(self, message, *, message_id=None):
            raise RuntimeError("Service Bus no responde")

    with pytest.raises(RuntimeError):
        await letters.enqueue(
            FakeSession(), settings, BrokenPublisher(), owner, payload_for(purchase)
        )
    rejected = RecordingPublisher(accept=False)
    assert (
        await letters.enqueue(FakeSession(), settings, rejected, owner, payload_for(purchase))
        is False
    )


# --- `letters.attach_temp_photos(replace=True)`: las fotos son las del payload de ahora ----


async def test_happy_path_replacing_discards_the_old_photos_and_attaches_the_new(
    settings, monkeypatch
):
    owner = buyer()
    draft = letter_of(purchase_of(owner))
    old = [stored_photo(draft, f"letters/{draft.id}/vieja.png", 0)]
    photos_returning(monkeypatch, old)
    db, storage, ref = FakeSession(), FakeStorage(), temp_ref(owner)

    result = await letters.attach_temp_photos(db, settings, storage, draft, [ref], replace=True)

    assert db.deleted == old
    assert storage.deleted == [old[0].storage_key]
    assert storage.moved == [(ref.tempId, letters.permanent_key(draft, ref))]
    assert [(photo.position, photo.caption) for photo in result] == [(0, "nueva.png")]
    assert db.commits == 2  # uno para soltar las viejas, otro para guardar las nuevas


async def test_sad_path_replacing_with_no_photos_leaves_the_letter_without_any(
    settings, monkeypatch
):
    owner = buyer()
    draft = letter_of(purchase_of(owner))
    old = [
        stored_photo(draft, f"letters/{draft.id}/a.png", 0),
        stored_photo(draft, f"letters/{draft.id}/b.png", 1),
    ]
    photos_returning(monkeypatch, old)
    db, storage = FakeSession(), FakeStorage()

    result = await letters.attach_temp_photos(db, settings, storage, draft, [], replace=True)

    assert result == []
    assert db.deleted == old
    assert sorted(storage.deleted) == sorted(photo.storage_key for photo in old)
    assert db.commits == 1


async def test_edge_a_photo_the_payload_brings_again_is_kept_and_not_moved_twice(
    settings, monkeypatch
):
    """Reentrega del mensaje tras mover los blobs: misma clave determinista, nada se pierde."""
    owner = buyer()
    draft = letter_of(purchase_of(owner))
    ref = temp_ref(owner)
    already = [stored_photo(draft, letters.permanent_key(draft, ref), 0)]
    photos_returning(monkeypatch, already)
    db, storage = FakeSession(), FakeStorage()

    result = await letters.attach_temp_photos(db, settings, storage, draft, [ref], replace=True)

    assert result == already
    assert db.deleted == []
    assert storage.deleted == []
    assert storage.moved == []
    assert db.commits == 0


async def test_edge_without_replace_the_old_photos_stay_and_the_new_go_after(settings, monkeypatch):
    owner = buyer()
    draft = letter_of(purchase_of(owner))
    old = [
        stored_photo(draft, f"letters/{draft.id}/a.png", 0),
        stored_photo(draft, f"letters/{draft.id}/b.png", 1),
    ]
    photos_returning(monkeypatch, old)
    db, storage = FakeSession(), FakeStorage()

    result = await letters.attach_temp_photos(db, settings, storage, draft, [temp_ref(owner)])

    assert [photo.position for photo in result] == [0, 1, 2]
    assert db.deleted == []
    assert storage.deleted == []


async def test_exception_a_blob_that_cannot_be_deleted_does_not_block_the_retake(
    settings, monkeypatch
):
    """Azure no borra la foto vieja y una nueva ya no está en el temporal: la carta sale igual."""
    owner = buyer()
    draft = letter_of(purchase_of(owner))
    old = [stored_photo(draft, f"letters/{draft.id}/vieja.png", 0)]
    photos_returning(monkeypatch, old)
    db, storage = FakeSession(), FakeStorage()
    storage.delete_error = RuntimeError("ServiceRequestError")
    survivor, lost = temp_ref(owner, "sobrevive.png"), temp_ref(owner, "perdida.png")
    storage.missing.add(lost.tempId)

    result = await letters.attach_temp_photos(
        db, settings, storage, draft, [survivor, lost], replace=True
    )

    assert db.deleted == old  # la fila vieja sí desaparece aunque el blob quede huérfano
    assert [(photo.position, photo.caption) for photo in result] == [(0, "sobrevive.png")]


# --- Orquestación de `CommerceService`, sin base de datos ---------------------------------


class Ledger:
    """Anota qué servicios de dominio invoca `CommerceService`, con el `replace` pedido."""

    def __init__(self, letter: Letter, created: bool, delivered_versions=()):
        self.letter = letter
        self.created = created
        self.calls: list[str] = []
        self.delivered_versions = list(delivered_versions)

    def install(self, monkeypatch):
        from app.services import commerce as service_module

        async def create(db, settings, user, payload):
            self.calls.append("create")
            return self.letter, self.created

        async def attach(db, settings, storage, letter, refs, *, replace=False):
            self.calls.append(f"attach(replace={replace})")
            return []

        async def publish(db, settings, letter):
            self.calls.append("publish")
            letter.status = "published"
            letter.published_version += 1
            return letter

        async def deliver(db, settings, mailer, letter, recipient=None, storage=None):
            self.calls.append("deliver")
            self.delivered_versions.append(letter.published_version)
            return LetterDelivery(
                id=uuid.uuid4(),
                letter_id=letter.id,
                recipient_email=letter.recipient_email,
                status="sent",
                attempts=1,
                letter_version=letter.published_version,
                created_at=datetime.now(UTC),
            )

        async def deliveries_of_letter(db, letter_id):
            return [
                LetterDelivery(
                    id=uuid.uuid4(),
                    letter_id=letter_id,
                    recipient_email=self.letter.recipient_email or "x@example.com",
                    status="sent",
                    attempts=1,
                    letter_version=version,
                    created_at=datetime.now(UTC),
                )
                for version in self.delivered_versions
            ]

        async def photos_of_letter(db, letter_id):
            return []

        monkeypatch.setattr(service_module.letters, "create", create)
        monkeypatch.setattr(service_module.letters, "attach_temp_photos", attach)
        monkeypatch.setattr(service_module.letters, "publish", publish)
        monkeypatch.setattr(service_module.deliveries, "deliver", deliver)
        monkeypatch.setattr(service_module.repo, "deliveries_of_letter", deliveries_of_letter)
        monkeypatch.setattr(service_module.repo, "photos_of_letter", photos_of_letter)
        return self


def sync_service(settings, db=None) -> CommerceService:
    return CommerceService(
        db=db or FakeSession(),
        settings=settings,
        storage=None,
        mailer=ConsoleMailer(),
        payments=None,
        queue=MockPublisher(),
    )


async def test_orchestration_retaking_a_draft_replaces_photos_then_publishes_and_sends(
    settings, monkeypatch
):
    draft = letter_of(purchase_of(buyer()))
    ledger = Ledger(draft, created=False).install(monkeypatch)

    outcome = await sync_service(settings).create_letter(buyer(), payload_for(draft))

    assert outcome.status == 200
    assert ledger.calls == ["create", "attach(replace=True)", "publish", "deliver"]
    assert outcome.body.status == "published"


async def test_orchestration_a_new_letter_attaches_without_replacing(settings, monkeypatch):
    draft = letter_of(purchase_of(buyer()))
    ledger = Ledger(draft, created=True).install(monkeypatch)

    outcome = await sync_service(settings).create_letter(buyer(), payload_for(draft))

    assert outcome.status == 201
    assert ledger.calls == ["create", "attach(replace=False)", "publish", "deliver"]


async def test_orchestration_a_published_letter_is_left_alone(settings, monkeypatch):
    published = letter_of(purchase_of(buyer()), status="published")
    ledger = Ledger(published, created=False, delivered_versions=[1]).install(monkeypatch)

    outcome = await sync_service(settings).create_letter(buyer(), payload_for(published))

    assert outcome.status == 200
    assert ledger.calls == ["create"]  # ni fotos, ni publicar, ni segundo correo


async def test_orchestration_auto_publish_false_keeps_the_retaken_draft(settings, monkeypatch):
    draft = letter_of(purchase_of(buyer()))
    ledger = Ledger(draft, created=False).install(monkeypatch)

    outcome = await sync_service(settings).create_letter(
        buyer(), payload_for(draft, autoPublish=False)
    )

    assert ledger.calls == ["create", "attach(replace=True)"]
    assert outcome.body.status == "draft"


async def test_orchestration_the_worker_retakes_a_draft_the_same_way(settings, monkeypatch):
    """El mensaje de la cola pasa por la misma orquestación: sin divergencias."""
    owner = buyer()
    draft = letter_of(purchase_of(owner))
    ledger = Ledger(draft, created=False).install(monkeypatch)
    message = build_letter_message(owner.id, payload_for(draft, temp_photos=[temp_ref(owner)]))

    letter_id = await sync_service(settings, FakeSession(owner)).fulfil_queued_letter(message)

    assert letter_id == draft.id
    assert ledger.calls == ["create", "attach(replace=True)", "publish", "deliver"]


# --- Integración: HTTP y worker (requiere TEST_DATABASE_URL) --------------------------------


async def post_letter(client, purchase, **changes):
    return await client.post(
        "/api/v1/letters", json={**LETTER, "purchaseId": purchase["id"], **changes}
    )


async def eager(client, name="nueva.png"):
    response = await client.post(
        "/api/v1/letters/photos/eager", files={"file": (name, PNG, "image/png")}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def count(app, model) -> int:
    async with app.state.sessions() as db:
        return await db.scalar(select(func.count()).select_from(model))


async def test_http_retaking_a_draft_publishes_the_new_content_with_the_new_photos(
    client, paid_purchase, app
):
    stuck = await post_letter(client, paid_purchase, autoPublish=False)
    assert stuck.status_code == 201, stuck.text
    assert stuck.json()["status"] == "draft"
    old_photo = await client.post(
        f"/api/v1/letters/{stuck.json()['id']}/photos",
        files={"file": ("vieja.png", PNG, "image/png")},
    )
    assert old_photo.status_code == 201, old_photo.text
    fresh = await eager(client, "nueva.png")

    retaken = await post_letter(client, paid_purchase, **NEW_CONTENT, temp_photos=[fresh])

    assert retaken.status_code == 200, retaken.text
    body = retaken.json()
    assert body["id"] == stuck.json()["id"]
    assert body["status"] == "published"
    assert body["title"] == "Segunda vuelta"
    assert body["body"] == NEW_CONTENT["body"]
    assert body["recipientEmail"] == "lucia@example.com"
    assert [photo["caption"] for photo in body["photos"]] == ["nueva.png"]
    assert [item["status"] for item in body["deliveries"]] == ["sent"]
    assert await count(app, Letter) == 1
    assert await count(app, LetterPhoto) == 1
    assert app.state.mailer.sent[-1].to == "lucia@example.com"


async def test_http_a_published_letter_is_not_overwritten(client, paid_purchase, app):
    first = await post_letter(client, paid_purchase)
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "published"

    again = await post_letter(client, paid_purchase, **NEW_CONTENT)

    assert again.status_code == 200
    assert again.json()["title"] == LETTER["title"]
    assert len(app.state.mailer.sent) == 1


async def test_queue_retaking_a_draft_is_enqueued_and_the_worker_overwrites_it(
    client, paid_purchase, app
):
    stuck = await post_letter(client, paid_purchase, autoPublish=False)
    assert stuck.status_code == 201, stuck.text
    queue = RecordingPublisher()
    app.state.letter_queue = queue

    response = await post_letter(client, paid_purchase, **NEW_CONTENT)

    assert response.status_code == 202, response.text
    assert queue.ids[0].startswith(f"letter-{paid_purchase['id']}-")
    async with app.state.sessions() as db:
        letter_id = await service_for(app, db).fulfil_queued_letter(queue.messages[0])
    assert str(letter_id) == stuck.json()["id"]
    detail = (await client.get(f"/api/v1/letters/{letter_id}")).json()
    assert detail["status"] == "published"
    assert detail["title"] == "Segunda vuelta"
    assert await count(app, Letter) == 1


async def test_queue_a_published_letter_is_refused_with_409(client, paid_purchase, app):
    first = await post_letter(client, paid_purchase)
    assert first.status_code == 201, first.text
    queue = RecordingPublisher()
    app.state.letter_queue = queue

    response = await post_letter(client, paid_purchase, **NEW_CONTENT)

    assert response.status_code == 409
    assert response.json()["code"] == "LETTER_ALREADY_EXISTS"
    assert queue.messages == []


async def test_the_dashboard_follows_the_retake(client, paid_purchase):
    stuck = (await post_letter(client, paid_purchase, autoPublish=False)).json()
    before = (await client.get("/api/v1/me/dedications")).json()
    assert [(item["state"], item["letterId"]) for item in before] == [("draft", stuck["id"])]

    assert (await post_letter(client, paid_purchase, **NEW_CONTENT)).status_code == 200

    after = (await client.get("/api/v1/me/dedications")).json()
    assert [(item["state"], item["title"]) for item in after] == [("published", "Segunda vuelta")]
