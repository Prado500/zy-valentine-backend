"""Mis dedicatorias: el panel posventa del comprador (``GET /api/v1/me/dedications``).

Regla del producto: no hay autoguardado. Quien pagó y abandonó el editor no tiene fila
de carta; su borrador **es** la compra pagada sin carta. El panel deriva el estado en
cada lectura y no lo guarda: ninguna columna nueva, ninguna migración.

Rule of 10 sobre las piezas nuevas: la derivación del estado (dominio), la consulta
(repositorio), el armado de la respuesta (servicio de aplicación) y el endpoint. Las
tres primeras se prueban sin base de datos; el endpoint es integración y corre en CI.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from app.models.commerce import Letter, Purchase
from app.models.user import User
from app.repositories import commerce as repo
from app.schemas.commerce import DedicationResponse
from app.services import dedications
from app.services.commerce import CommerceService
from app.services.mailer import ConsoleMailer
from app.services.service_bus import MockPublisher
from tests.conftest import BUYER, new_purchase

NOW = datetime.now(UTC)
LETTER = {
    "title": "Para ti 💌",
    "recipientName": "Ana",
    "recipientEmail": "ana@example.com",
    "body": "Primera línea\nSegunda línea con emoji 🌹",
    "theme": "classic",
}


def purchase_row(status: str = "paid", user_id: uuid.UUID | None = None) -> Purchase:
    paid = status != "pending"
    return Purchase(
        id=uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
        status=status,
        amount_cents=1500000,
        currency="COP",
        external_reference=f"zv-{uuid.uuid4().hex}",
        idempotency_key="idem-key-0001",
        paid_at=NOW - timedelta(hours=1) if paid else None,
        expires_at=NOW + timedelta(hours=1),
        created_at=NOW - timedelta(hours=2),
        updated_at=NOW - timedelta(hours=1),
    )


def letter_row(purchase: Purchase, status: str = "published") -> Letter:
    published = status == "published"
    return Letter(
        id=uuid.uuid4(),
        purchase_id=purchase.id,
        user_id=purchase.user_id,
        public_slug="slug-de-prueba",
        status=status,
        title=LETTER["title"],
        recipient_name=LETTER["recipientName"],
        recipient_email=LETTER["recipientEmail"],
        body=LETTER["body"],
        theme="classic",
        published_version=1 if published else 0,
        published_at=NOW - timedelta(minutes=30) if published else None,
        created_at=NOW - timedelta(minutes=45),
        updated_at=NOW - timedelta(minutes=30),
    )


def service_without_db(settings) -> CommerceService:
    return CommerceService(
        db=object(),
        settings=settings,
        storage=None,
        mailer=ConsoleMailer(),
        payments=None,
        queue=MockPublisher(),
    )


# --- Dominio: `dedications.state_of` -------------------------------------------------------


def test_happy_path_published_letter_is_published():
    purchase = purchase_row()
    assert dedications.state_of(purchase, letter_row(purchase)) == "published"


def test_sad_path_paid_purchase_without_letter_is_a_draft():
    """El comprador pagó y cerró el editor: el borrador es la compra, no hay carta."""
    assert dedications.state_of(purchase_row(), None) == "draft"


def test_edge_letter_left_in_draft_is_a_draft():
    purchase = purchase_row()
    assert dedications.state_of(purchase, letter_row(purchase, "draft")) == "draft"


def test_edge_refunded_purchase_keeps_its_published_letter():
    """Un reembolso posterior no borra la carta ya publicada: el enlace sigue vivo."""
    purchase = purchase_row("cancelled")
    assert dedications.state_of(purchase, letter_row(purchase)) == "published"


@pytest.mark.parametrize(
    ("purchase_status", "letter_status"),
    [("pending", None), ("expired", None), ("cancelled", None), ("cancelled", "draft")],
)
def test_exception_what_is_not_post_sale_has_no_state(purchase_status, letter_status):
    """Sin pago confirmado no hay dedicatoria que mostrar, ni siquiera un borrador."""
    purchase = purchase_row(purchase_status)
    letter = letter_row(purchase, letter_status) if letter_status else None
    assert dedications.state_of(purchase, letter) is None


# --- Repositorio: una sola consulta con LEFT JOIN ------------------------------------------


def compiled(user_id: uuid.UUID) -> str:
    statement = repo.dedications_statement(user_id)
    return str(statement.compile(dialect=postgresql.dialect()))


def test_statement_is_a_single_left_outer_join():
    sql = compiled(uuid.uuid4())
    assert "FROM purchases LEFT OUTER JOIN letters ON letters.purchase_id = purchases.id" in sql


def test_statement_is_scoped_to_the_user():
    assert "purchases.user_id = " in compiled(uuid.uuid4())


def test_statement_only_brings_post_sale_rows():
    sql = compiled(uuid.uuid4())
    assert "purchases.status = " in sql
    assert "OR letters.status = " in sql


def test_statement_lists_newest_purchase_first():
    assert compiled(uuid.uuid4()).rstrip().endswith("ORDER BY purchases.created_at DESC")


def test_statement_selects_both_entities():
    """Compra y carta viajan juntas: ni una consulta por fila ni una por foto."""
    sql = compiled(uuid.uuid4())
    assert "purchases.paid_at" in sql
    assert "letters.title" in sql
    assert "letters.body" in sql


# --- Servicio de aplicación: `CommerceService.list_dedications` --------------------------


def rows_returning(monkeypatch, rows):
    from app.services import commerce as service_module

    async def dedications_of_user(db, user_id):
        return rows

    monkeypatch.setattr(service_module.repo, "dedications_of_user", dedications_of_user)


def buyer_user() -> User:
    return User(id=uuid.uuid4(), email="comprador@example.com", is_active=True)


async def test_happy_path_published_row_is_mapped_with_public_link(settings, monkeypatch):
    purchase = purchase_row()
    letter = letter_row(purchase)
    rows_returning(monkeypatch, [(purchase, letter)])

    items = await service_without_db(settings).list_dedications(buyer_user())

    assert len(items) == 1
    item = items[0]
    assert item.purchaseId == purchase.id
    assert item.letterId == letter.id
    assert item.state == "published"
    assert item.title == LETTER["title"]
    assert item.recipientName == "Ana"
    assert item.theme == "classic"
    assert item.publicSlug == "slug-de-prueba"
    assert item.publicUrl == f"{settings.public_base_url}/carta/slug-de-prueba"
    assert item.paidAt == purchase.paid_at
    assert item.publishedAt == letter.published_at
    assert item.updatedAt == letter.updated_at


async def test_sad_path_abandoned_purchase_is_a_draft_slot(settings, monkeypatch):
    """Sin carta no hay título ni enlace: solo el hueco que el editor va a llenar."""
    purchase = purchase_row()
    rows_returning(monkeypatch, [(purchase, None)])

    item = (await service_without_db(settings).list_dedications(buyer_user()))[0]

    assert item.state == "draft"
    assert item.letterId is None
    assert item.title is None
    assert item.recipientName is None
    assert item.theme is None
    assert item.publicSlug is None
    assert item.publicUrl is None
    assert item.publishedAt is None
    assert item.paidAt == purchase.paid_at
    assert item.updatedAt == purchase.updated_at


async def test_edge_draft_letter_has_no_public_link_yet(settings, monkeypatch):
    purchase = purchase_row()
    letter = letter_row(purchase, "draft")
    rows_returning(monkeypatch, [(purchase, letter)])

    item = (await service_without_db(settings).list_dedications(buyer_user()))[0]

    assert item.state == "draft"
    assert item.letterId == letter.id
    assert item.title == LETTER["title"]
    assert item.publicSlug is None
    assert item.publicUrl is None


async def test_edge_order_is_kept_and_non_post_sale_rows_are_dropped(settings, monkeypatch):
    """La consulta ya filtra, pero la regla vive en el dominio: se aplica de nuevo aquí."""
    newest, older, pending = purchase_row(), purchase_row(), purchase_row("pending")
    rows_returning(monkeypatch, [(newest, None), (pending, None), (older, letter_row(older))])

    items = await service_without_db(settings).list_dedications(buyer_user())

    assert [item.purchaseId for item in items] == [newest.id, older.id]
    assert [item.state for item in items] == ["draft", "published"]
    rows_returning(monkeypatch, [])
    assert await service_without_db(settings).list_dedications(buyer_user()) == []


async def test_exception_database_failure_propagates_to_the_http_handler(settings, monkeypatch):
    """Una base caída se traduce en 503 DATABASE_UNAVAILABLE por el manejador global."""
    from app.services import commerce as service_module

    async def broken(db, user_id):
        raise SQLAlchemyError("connection reset")

    monkeypatch.setattr(service_module.repo, "dedications_of_user", broken)
    with pytest.raises(SQLAlchemyError):
        await service_without_db(settings).list_dedications(buyer_user())


def test_response_schema_only_admits_the_two_dashboard_states():
    base = {
        "purchaseId": uuid.uuid4(),
        "letterId": None,
        "title": None,
        "recipientName": None,
        "theme": None,
        "publicSlug": None,
        "publicUrl": None,
        "paidAt": NOW,
        "publishedAt": None,
        "updatedAt": NOW,
    }
    assert DedicationResponse(state="draft", **base).state == "draft"
    assert DedicationResponse(state="published", **base).state == "published"
    with pytest.raises(ValidationError):
        DedicationResponse(state="pending_payment", **base)


# --- Endpoint (integración, requiere TEST_DATABASE_URL) -----------------------------------


async def dedications_of(client):
    response = await client.get("/api/v1/me/dedications")
    assert response.status_code == 200, response.text
    return response.json()


async def test_endpoint_requires_a_session(client):
    assert (await client.get("/api/v1/me/dedications")).status_code == 401


async def test_endpoint_is_empty_for_a_new_buyer(client, buyer):
    assert await dedications_of(client) == []


async def test_endpoint_shows_the_paid_purchase_as_a_draft_and_hides_the_pending_one(
    client, paid_purchase, gateway
):
    await new_purchase(client, "idem-key-0002")  # pendiente de pago: no es posventa
    items = await dedications_of(client)
    assert len(items) == 1
    assert items[0]["purchaseId"] == paid_purchase["id"]
    assert items[0]["state"] == "draft"
    assert items[0]["letterId"] is None
    assert items[0]["publicUrl"] is None
    assert items[0]["paidAt"]


async def test_endpoint_shows_the_letter_as_published_after_the_editor_finishes(
    client, paid_purchase
):
    created = await client.post(
        "/api/v1/letters", json={**LETTER, "purchaseId": paid_purchase["id"]}
    )
    assert created.status_code == 201, created.text
    items = await dedications_of(client)
    assert len(items) == 1
    assert items[0]["state"] == "published"
    assert items[0]["letterId"] == created.json()["id"]
    assert items[0]["title"] == LETTER["title"]
    assert items[0]["publicSlug"] == created.json()["publicSlug"]
    assert items[0]["publicUrl"] == created.json()["publicUrl"]
    assert items[0]["publishedAt"]


async def test_endpoint_shows_a_draft_letter_and_nothing_of_other_accounts(
    client, paid_purchase, app
):
    created = await client.post(
        "/api/v1/letters",
        json={**LETTER, "purchaseId": paid_purchase["id"], "autoPublish": False},
    )
    assert created.status_code == 201, created.text
    items = await dedications_of(client)
    assert [(item["state"], item["letterId"]) for item in items] == [
        ("draft", created.json()["id"])
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as other:
        other.headers["X-CSRF-Token"] = (await other.get("/api/v1/auth/csrf")).json()["csrfToken"]
        await other.post("/api/v1/auth/register", json={**BUYER, "email": "otra@example.com"})
        await other.post(
            "/api/v1/auth/login", json={"email": "otra@example.com", "password": BUYER["password"]}
        )
        assert await dedications_of(other) == []
