"""Despacho en el camino síncrono: sin Service Bus la carta se publica y se envía.

Incidente que motiva estas pruebas: en DEV, sin cola configurada, ``POST /letters``
respondía 201 con la carta en borrador. El frontend, construido contra el contrato
asíncrono, mostraba "espera el correo" y ese correo no salía nunca porque nadie
llamaba a ``deliveries.deliver``. ``letter_deliveries`` quedaba vacía.

Ninguna prueba habla con Gmail ni con Azure: el correo es el ``ConsoleMailer`` y el
almacenamiento es el de disco. Se comprueba el contrato del despacho, no el transporte.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.models.commerce import Letter, LetterDelivery
from app.models.user import User
from app.schemas.commerce import LetterCreate
from app.services.commerce import CommerceService
from app.services.mailer import ConsoleMailer
from app.services.service_bus import MockPublisher, build_letter_message
from tests.conftest import service_for

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
LETTER = {
    "title": "Para ti 💌",
    "recipientName": "Ana",
    "recipientEmail": "ana@example.com",
    "body": "Primera línea\nSegunda línea con emoji 🌹",
    "theme": "classic",
}


async def create_letter(client, purchase, **changes):
    """Tal como lo manda el frontend desplegado: sin ``autoPublish`` explícito."""
    payload = {**LETTER, "purchaseId": purchase["id"], **changes}
    return await client.post("/api/v1/letters", json=payload)


async def eager(client, name="mi foto.png"):
    return await client.post(
        "/api/v1/letters/photos/eager", files={"file": (name, PNG, "image/png")}
    )


async def count(app, model) -> int:
    async with app.state.sessions() as db:
        return await db.scalar(select(func.count()).select_from(model))


# --- Sin base de datos: el contrato del esquema y del sobre de la cola --------------------


def test_auto_publish_defaults_to_true_and_can_be_disabled():
    """El frontend no manda el campo: debe publicar. Quien quiera borrador lo pide."""
    purchase_id = "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f"
    implicit = LetterCreate(purchaseId=purchase_id, **LETTER)
    explicit = LetterCreate(purchaseId=purchase_id, autoPublish=False, **LETTER)
    assert implicit.autoPublish is True
    assert explicit.autoPublish is False


def test_queue_envelope_carries_the_requested_auto_publish():
    """Lo que el comprador pide en la API es lo que el worker ejecuta."""
    payload = LetterCreate(purchaseId=uuid.uuid4(), autoPublish=False, **LETTER)
    envelope = build_letter_message(uuid.uuid4(), payload, auto_publish=payload.autoPublish)
    assert envelope["autoPublish"] is False


# --- Sin base de datos: la orquestación de `create_letter`, con los servicios parcheados ----


class Ledger:
    """Anota qué servicios de dominio invoca `CommerceService` y en qué orden.

    Sustituye a `letters.create`, `attach_temp_photos`, `publish`, `deliveries.deliver`
    y al repositorio, así que estas pruebas no abren PostgreSQL: verifican el contrato
    del despacho (qué se llama y qué no) sin depender de que exista una base local.
    """

    def __init__(self, letter: Letter, created: bool = True, delivered_versions=()):
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
            self.calls.append("attach")
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


def draft_letter(recipient_email="ana@example.com") -> Letter:
    now = datetime.now(UTC)
    return Letter(
        id=uuid.uuid4(),
        purchase_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        public_slug="slug-de-prueba",
        status="draft",
        title=LETTER["title"],
        recipient_name=LETTER["recipientName"],
        recipient_email=recipient_email,
        body=LETTER["body"],
        theme="classic",
        published_version=0,
        created_at=now,
        updated_at=now,
    )


def sync_service(settings) -> CommerceService:
    """Servicio sin cola (MockPublisher) y con puertos inofensivos."""
    return CommerceService(
        db=object(),
        settings=settings,
        storage=None,
        mailer=ConsoleMailer(),
        payments=None,
        queue=MockPublisher(),
    )


def buyer_user() -> User:
    return User(id=uuid.uuid4(), email="comprador@example.com", is_active=True)


async def test_sync_path_publishes_and_delivers_by_default(settings, monkeypatch):
    letter = draft_letter()
    ledger = Ledger(letter).install(monkeypatch)
    payload = LetterCreate(purchaseId=letter.purchase_id, **LETTER)

    outcome = await sync_service(settings).create_letter(buyer_user(), payload)

    assert outcome.status == 201
    assert ledger.calls == ["create", "attach", "publish", "deliver"]
    assert outcome.body.status == "published"
    assert outcome.body.publicUrl == f"{settings.public_base_url}/carta/slug-de-prueba"
    assert outcome.body.deliveries[0].status == "sent"


async def test_sync_path_without_recipient_only_writes_the_draft(settings, monkeypatch):
    letter = draft_letter(recipient_email=None)
    ledger = Ledger(letter).install(monkeypatch)
    payload = LetterCreate(purchaseId=letter.purchase_id, **{**LETTER, "recipientEmail": None})

    outcome = await sync_service(settings).create_letter(buyer_user(), payload)

    assert outcome.status == 201
    assert ledger.calls == ["create", "attach"]
    assert outcome.body.status == "draft"
    assert outcome.body.publicUrl is None


async def test_sync_path_honours_auto_publish_false(settings, monkeypatch):
    letter = draft_letter()
    ledger = Ledger(letter).install(monkeypatch)
    payload = LetterCreate(purchaseId=letter.purchase_id, autoPublish=False, **LETTER)

    outcome = await sync_service(settings).create_letter(buyer_user(), payload)

    assert ledger.calls == ["create", "attach"]
    assert outcome.body.status == "draft"


async def test_existing_published_letter_is_not_emailed_again(settings, monkeypatch):
    # La carta ya existe, está publicada (versión 1) y esa versión ya se envió.
    letter = draft_letter()
    letter.status, letter.published_version = "published", 1
    ledger = Ledger(letter, created=False, delivered_versions=[1]).install(monkeypatch)
    payload = LetterCreate(purchaseId=letter.purchase_id, **LETTER)

    outcome = await sync_service(settings).create_letter(buyer_user(), payload)

    assert outcome.status == 200
    assert ledger.calls == ["create"]  # ni fotos, ni publicar, ni un segundo correo


async def test_existing_draft_is_completed_on_retry(settings, monkeypatch):
    # Un intento anterior murió entre crear y despachar: el reintento lo termina. Al ser
    # un borrador, las fotos vuelven a adjuntarse (las del payload de ahora) y se publica.
    letter = draft_letter()
    ledger = Ledger(letter, created=False).install(monkeypatch)
    payload = LetterCreate(purchaseId=letter.purchase_id, **LETTER)

    outcome = await sync_service(settings).create_letter(buyer_user(), payload)

    assert outcome.status == 200
    assert ledger.calls == ["create", "attach", "publish", "deliver"]
    assert outcome.body.status == "published"


# --- Camino feliz (integración, requiere TEST_DATABASE_URL) ------------------------------


async def test_letter_is_published_and_emailed_on_creation(client, paid_purchase, app):
    photo = (await eager(client, "recuerdo.png")).json()
    response = await create_letter(client, paid_purchase, temp_photos=[photo])
    assert response.status_code == 201, response.text
    body = response.json()

    # La respuesta ya trae lo que el frontend necesita para enseñar enlace y QR.
    assert body["status"] == "published"
    assert body["publishedVersion"] == 1
    assert body["publicSlug"]
    assert body["publicUrl"].endswith(f"/carta/{body['publicSlug']}")
    assert body["qrUrl"]
    assert [photo["caption"] for photo in body["photos"]] == ["recuerdo.png"]

    # Y el envío quedó registrado y hecho.
    assert [item["status"] for item in body["deliveries"]] == ["sent"]
    assert body["deliveries"][0]["recipientEmail"] == LETTER["recipientEmail"]
    assert body["deliveries"][0]["letterVersion"] == 1
    assert await count(app, LetterDelivery) == 1

    sent = app.state.mailer.sent[-1]
    assert sent.to == LETTER["recipientEmail"]
    assert body["publicUrl"] in sent.html
    assert "data:image/png;base64," not in sent.html  # el QR va como parte relacionada
    assert f'src="cid:{sent.inline[0].cid[1:-1]}"' in sent.html
    assert [item.subtype for item in sent.attachments] == ["pdf"]  # solo la tarjeta
    assert "recuerdo.png" in [photo["caption"] for photo in body["photos"]]


# --- Camino triste: sin destinatario no hay nada que despachar -----------------------------


async def test_without_recipient_email_the_letter_stays_a_draft(client, paid_purchase, app):
    response = await create_letter(client, paid_purchase, recipientEmail=None)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "draft"
    assert body["publicUrl"] is None
    assert body["deliveries"] == []
    assert await count(app, LetterDelivery) == 0
    assert app.state.mailer.sent == []


# --- Borde 1: el contrato explícito de borrador sigue existiendo ---------------------------


async def test_auto_publish_false_keeps_the_draft_flow(client, paid_purchase, app):
    response = await create_letter(client, paid_purchase, autoPublish=False)
    assert response.status_code == 201, response.text
    letter = response.json()
    assert letter["status"] == "draft"
    assert app.state.mailer.sent == []

    # Editable, con fotos, y se publica cuando el comprador lo decide.
    edited = await client.patch(f"/api/v1/letters/{letter['id']}", json={"body": "otro texto"})
    assert edited.status_code == 200
    published = await client.post(f"/api/v1/letters/{letter['id']}/publish")
    assert published.status_code == 200
    assert published.json()["status"] == "published"
    assert [item["status"] for item in published.json()["deliveries"]] == ["sent"]
    assert len(app.state.mailer.sent) == 1


# --- Borde 2: reintentos y concurrencia no duplican correos --------------------------------


async def test_resubmitting_the_form_does_not_send_twice(client, paid_purchase, app):
    first = await create_letter(client, paid_purchase)
    assert first.status_code == 201
    # El comprador retrocede y reenvía: misma carta con 200, y ningún correo nuevo.
    again = await create_letter(client, paid_purchase, title="Otro intento")
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["title"] == LETTER["title"]
    assert len(again.json()["deliveries"]) == 1
    assert await count(app, Letter) == 1
    assert await count(app, LetterDelivery) == 1
    assert len(app.state.mailer.sent) == 1


async def test_retry_completes_a_letter_left_in_draft(client, paid_purchase, app):
    # Un intento anterior se quedó a medias (o fue creado con el código antiguo).
    stuck = await create_letter(client, paid_purchase, autoPublish=False)
    assert stuck.json()["status"] == "draft"
    # El comprador vuelve a enviar el formulario: se completa el despacho pendiente.
    retry = await create_letter(client, paid_purchase)
    assert retry.status_code == 200
    assert retry.json()["status"] == "published"
    assert [item["status"] for item in retry.json()["deliveries"]] == ["sent"]
    assert await count(app, Letter) == 1
    assert len(app.state.mailer.sent) == 1


# --- Excepciones: el correo falla, la carta no -------------------------------------------


async def test_mail_failure_keeps_the_letter_published_and_retryable(client, paid_purchase, app):
    class FailingMailer(ConsoleMailer):
        async def send(self, message):
            raise TimeoutError("smtp timeout")

    app.state.mailer = FailingMailer()
    response = await create_letter(client, paid_purchase)
    # La petición no se rompe: la carta existe, está publicada y el fallo quedó anotado.
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "published"
    assert body["publicUrl"]
    assert body["deliveries"][0]["status"] == "failed"
    assert body["deliveries"][0]["lastError"] == "TimeoutError"

    # Reintento explícito con el correo ya operativo: otra fila, misma carta.
    app.state.mailer = ConsoleMailer()
    retry = await client.post(f"/api/v1/letters/{body['id']}/deliveries", json={})
    assert retry.status_code == 202
    assert retry.json()["status"] == "sent"
    assert await count(app, LetterDelivery) == 2
    assert await count(app, Letter) == 1


# --- El worker usa el mismo despacho -------------------------------------------------------


async def test_worker_respects_auto_publish_false(client, paid_purchase, app):
    async with app.state.sessions() as db:
        user_id = await db.scalar(select(User.id))
    payload = LetterCreate(purchaseId=paid_purchase["id"], autoPublish=False, **LETTER)
    message = build_letter_message(user_id, payload, auto_publish=payload.autoPublish)
    async with app.state.sessions() as db:
        letter_id = await service_for(app, db).fulfil_queued_letter(message)
    detail = (await client.get(f"/api/v1/letters/{letter_id}")).json()
    assert detail["status"] == "draft"
    assert detail["deliveries"] == []
    assert app.state.mailer.sent == []
