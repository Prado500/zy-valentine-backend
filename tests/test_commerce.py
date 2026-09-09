"""Compras, pagos, cartas, fotos, enlace público, QR y correo.

Ninguna prueba usa credenciales reales de Mercado Pago, Azure, Google ni correo:
las integraciones se inyectan por puerto (ver ``FakeGateway`` en conftest).
"""

import asyncio

import httpx
import pytest
from sqlalchemy import func, select

from app.models.commerce import Letter, LetterDelivery, Purchase
from app.services.mailer import ConsoleMailer
from tests.conftest import BUYER, new_purchase, pay

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
LETTER = {
    "title": "Para ti 💌",
    "recipientName": "Ana",
    "recipientEmail": "ana@example.com",
    "body": "Primera línea\nSegunda línea con emoji 🌹\nTercera",
    "theme": "classic",
}


async def create_letter(client, purchase, **changes):
    # Estas pruebas ejercitan el contrato explícito borrador → editar → publicar, así
    # que piden la carta sin auto-publicar. El despacho por defecto (publicar y enviar
    # en el mismo acto) tiene sus propias pruebas en ``tests/test_sync_dispatch.py``.
    return await client.post(
        "/api/v1/letters",
        json={**LETTER, "purchaseId": purchase["id"], "autoPublish": False, **changes},
    )


# --- Compras y verificación de pago ---------------------------------------------------


async def test_purchase_is_idempotent_per_key(client, gateway, buyer, app):
    first = await client.post("/api/v1/purchases", json={"idempotencyKey": "idem-key-0001"})
    second = await client.post("/api/v1/purchases", json={"idempotencyKey": "idem-key-0001"})
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Purchase)) == 1


async def test_concurrent_purchase_same_key_creates_one(client, gateway, buyer, app):
    results = await asyncio.gather(
        *[
            client.post("/api/v1/purchases", json={"idempotencyKey": "idem-tab-0001"})
            for _ in range(2)
        ]
    )
    assert sorted(r.status_code for r in results) == [200, 201], [r.text for r in results]
    assert {response.json()["id"] for response in results} == {results[0].json()["id"]}
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Purchase)) == 1


async def test_user_may_have_many_purchases(client, gateway, buyer):
    await new_purchase(client, "idem-key-0001")
    await new_purchase(client, "idem-key-0002")
    listed = await client.get("/api/v1/purchases")
    assert len(listed.json()) == 2


async def test_browser_return_alone_does_not_confirm_payment(client, gateway, buyer):
    purchase = await new_purchase(client)
    # El navegador "vuelve" sin identificador verificable: la compra sigue pendiente.
    response = await client.post(f"/api/v1/purchases/{purchase['id']}/verify", json={})
    assert response.status_code == 200
    assert response.json()["purchase"]["status"] == "pending"
    assert response.json()["canCreateLetter"] is False
    assert (await create_letter(client, purchase)).status_code == 409


async def test_unpaid_payment_status_keeps_purchase_pending(client, gateway, buyer):
    purchase = await new_purchase(client)
    gateway.set_status("900002", purchase["externalReference"], "rejected")
    response = await client.post(
        f"/api/v1/purchases/{purchase['id']}/verify", json={"paymentId": "900002"}
    )
    assert response.json()["purchase"]["status"] == "pending"
    assert response.json()["payment"]["status"] == "rejected"
    assert (await create_letter(client, purchase)).status_code == 409


async def test_amount_mismatch_is_rejected(client, gateway, buyer):
    purchase = await new_purchase(client)
    gateway.approve("900003", purchase["externalReference"], purchase["amountCents"] - 1)
    response = await client.post(
        f"/api/v1/purchases/{purchase['id']}/verify", json={"paymentId": "900003"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "PAYMENT_AMOUNT_MISMATCH"


async def test_payment_of_another_purchase_is_rejected(client, gateway, buyer):
    mine = await new_purchase(client, "idem-key-0001")
    other = await new_purchase(client, "idem-key-0002")
    gateway.approve("900004", other["externalReference"], other["amountCents"])
    response = await client.post(
        f"/api/v1/purchases/{mine['id']}/verify", json={"paymentId": "900004"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "PAYMENT_MISMATCH"


async def test_payments_not_configured_returns_503(client, buyer):
    purchase = await new_purchase(client)
    response = await client.post(
        f"/api/v1/purchases/{purchase['id']}/verify", json={"paymentId": "900005"}
    )
    assert response.status_code == 503
    assert response.json()["code"] == "PAYMENTS_NOT_CONFIGURED"


# --- Webhooks --------------------------------------------------------------------------


async def send_webhook(client, event_id, payment_id, signature="valid"):
    return await client.post(
        "/api/v1/webhooks/mercadopago",
        json={"id": event_id, "action": "payment.updated", "data": {"id": payment_id}},
        headers={"x-signature": signature},
    )


async def test_webhook_requires_signature(client, gateway, buyer):
    response = await send_webhook(client, 1, "900010", signature="forged")
    assert response.status_code == 401


async def test_repeated_webhook_is_processed_once(client, gateway, buyer):
    purchase = await new_purchase(client)
    gateway.approve("900011", purchase["externalReference"], purchase["amountCents"])
    first = await send_webhook(client, 111, "900011")
    second = await send_webhook(client, 111, "900011")
    assert first.json()["result"] == "applied"
    assert second.json()["result"] == "duplicate"
    assert gateway.calls.count("900011") == 1
    state = await client.get(f"/api/v1/purchases/{purchase['id']}")
    assert state.json()["status"] == "paid"


async def test_out_of_order_webhook_never_unpays(client, gateway, buyer):
    purchase = await new_purchase(client)
    gateway.approve("900012", purchase["externalReference"], purchase["amountCents"])
    assert (await send_webhook(client, 121, "900012")).json()["result"] == "applied"
    # Notificación antigua que llega tarde con un estado anterior.
    gateway.set_status("900012", purchase["externalReference"], "pending", purchase["amountCents"])
    assert (await send_webhook(client, 122, "900012")).json()["result"] == "applied"
    state = await client.get(f"/api/v1/purchases/{purchase['id']}")
    assert state.json()["status"] == "paid"


async def test_webhook_for_unknown_purchase_is_reported(client, gateway, buyer):
    gateway.approve("900013", "zv-inexistente", 1)
    assert (await send_webhook(client, 131, "900013")).json()["result"] == "unknown_purchase"


# --- Una compra pagada, exactamente una carta -------------------------------------------


async def test_paid_purchase_creates_exactly_one_letter(client, paid_purchase, app):
    created = await create_letter(client, paid_purchase)
    assert created.status_code == 201
    # Volver a enviar el formulario sobre un borrador lo retoma: misma carta (mismo id,
    # una sola fila) con el contenido nuevo. El detalle está en tests/test_retake_draft.py.
    again = await create_letter(client, paid_purchase, title="Intento nuevo")
    assert again.status_code == 200
    assert again.json()["id"] == created.json()["id"]
    assert again.json()["title"] == "Intento nuevo"
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Letter)) == 1


async def test_double_click_and_tabs_create_one_letter(client, paid_purchase, app):
    results = await asyncio.gather(*[create_letter(client, paid_purchase) for _ in range(3)])
    assert {response.json()["id"] for response in results} == {results[0].json()["id"]}
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Letter)) == 1


async def test_second_letter_requires_second_purchase(client, gateway, buyer, app):
    first = await new_purchase(client, "idem-key-0001")
    await pay(client, gateway, first, "900021")
    assert (await create_letter(client, first)).status_code == 201
    second = await new_purchase(client, "idem-key-0002")
    assert (await create_letter(client, second)).status_code == 409
    await pay(client, gateway, second, "900022")
    assert (await create_letter(client, second)).status_code == 201
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Letter)) == 2


async def test_letter_of_another_user_is_invisible(client, paid_purchase, app):
    letter = (await create_letter(client, paid_purchase)).json()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as other:
        token = (await other.get("/api/v1/auth/csrf")).json()["csrfToken"]
        other.headers["X-CSRF-Token"] = token
        await other.post("/api/v1/auth/register", json={**BUYER, "email": "otra@example.com"})
        await other.post(
            "/api/v1/auth/login", json={"email": "otra@example.com", "password": BUYER["password"]}
        )
        assert (await other.get(f"/api/v1/letters/{letter['id']}")).status_code == 404
        assert (await other.get("/api/v1/letters")).json() == []
        assert (
            await other.post("/api/v1/letters", json={**LETTER, "purchaseId": paid_purchase["id"]})
        ).status_code == 404


# --- Borradores y contenido --------------------------------------------------------------


async def test_draft_is_recoverable_and_editable(client, paid_purchase):
    letter = (await create_letter(client, paid_purchase)).json()
    assert letter["status"] == "draft"
    assert letter["publicUrl"] is None
    listed = (await client.get("/api/v1/letters")).json()
    assert [item["id"] for item in listed] == [letter["id"]]
    updated = await client.patch(
        f"/api/v1/letters/{letter['id']}", json={"body": "Texto corregido 🌙\nsegunda"}
    )
    assert updated.status_code == 200
    assert updated.json()["body"] == "Texto corregido 🌙\nsegunda"


async def test_emojis_newlines_and_photo_order_are_preserved(client, paid_purchase):
    letter = (await create_letter(client, paid_purchase)).json()
    for index in range(3):
        response = await client.post(
            f"/api/v1/letters/{letter['id']}/photos",
            files={"file": (f"foto{index}.png", PNG, "image/png")},
            data={"caption": f"Recuerdo {index} ✨"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["position"] == index
    detail = (await client.get(f"/api/v1/letters/{letter['id']}")).json()
    assert [photo["position"] for photo in detail["photos"]] == [0, 1, 2]
    assert detail["photos"][2]["caption"] == "Recuerdo 2 ✨"
    assert detail["body"] == LETTER["body"]
    content = await client.get(detail["photos"][0]["url"])
    assert content.status_code == 200
    assert content.content == PNG


@pytest.mark.parametrize(
    "payload",
    [
        {"file": ("mal.txt", b"no soy una imagen", "text/plain")},
        {"file": ("vacio.png", b"", "image/png")},
    ],
)
async def test_photo_validation(client, paid_purchase, payload):
    letter = (await create_letter(client, paid_purchase)).json()
    response = await client.post(f"/api/v1/letters/{letter['id']}/photos", files=payload)
    assert response.status_code in (415, 422)


async def test_photo_limit_and_size(client, paid_purchase, app):
    app.state.settings.max_photos_per_letter = 1
    letter = (await create_letter(client, paid_purchase)).json()
    first = await client.post(
        f"/api/v1/letters/{letter['id']}/photos", files={"file": ("a.png", PNG, "image/png")}
    )
    assert first.status_code == 201
    second = await client.post(
        f"/api/v1/letters/{letter['id']}/photos", files={"file": ("b.png", PNG, "image/png")}
    )
    assert second.status_code == 409
    app.state.settings.max_photo_bytes = 1024
    big = b"\x89PNG\r\n\x1a\n" + b"0" * 4096
    app.state.settings.max_photos_per_letter = 6
    too_big = await client.post(
        f"/api/v1/letters/{letter['id']}/photos", files={"file": ("c.png", big, "image/png")}
    )
    assert too_big.status_code == 413


# --- Publicación, enlace público, QR y correo ---------------------------------------------


async def publish(client, letter_id):
    return await client.post(f"/api/v1/letters/{letter_id}/publish")


async def test_publish_sends_email_with_link_qr_and_version(client, paid_purchase, app):
    letter = (await create_letter(client, paid_purchase)).json()
    published = await publish(client, letter["id"])
    assert published.status_code == 200
    body = published.json()
    assert body["status"] == "published"
    assert body["publishedVersion"] == 1
    assert body["publicUrl"].endswith(f"/carta/{body['publicSlug']}")
    assert body["deliveries"][0]["status"] == "sent"
    assert body["deliveries"][0]["letterVersion"] == 1

    message = app.state.mailer.sent[-1]
    assert message.to == "ana@example.com"
    assert body["publicUrl"] in message.html
    # El QR ya no se incrusta como data: URI (Gmail y Outlook lo descartan): viaja
    # como parte relacionada y el cuerpo solo lleva la referencia cid:.
    assert "data:image/png;base64," not in message.html
    qr = message.inline[0]
    assert f'src="cid:{qr.cid[1:-1]}"' in message.html
    assert (qr.maintype, qr.subtype) == ("image", "png")
    assert letter["id"] in message.html  # identificador de carta
    assert "versión publicada 1" in message.html


async def test_public_viewer_hides_buyer_data(client, paid_purchase, app):
    letter = (await create_letter(client, paid_purchase)).json()
    await client.post(
        f"/api/v1/letters/{letter['id']}/photos",
        files={"file": ("a.png", PNG, "image/png")},
        data={"caption": "Nuestra foto"},
    )
    slug = (await publish(client, letter["id"])).json()["publicSlug"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as anonymous:
        page = await anonymous.get(f"/api/v1/public/letters/{slug}")
        assert page.status_code == 200
        data = page.json()
        assert data["title"] == LETTER["title"]
        assert data["body"] == LETTER["body"]
        assert set(data) == {
            "letterId",
            "publishedVersion",
            "title",
            "recipientName",
            "body",
            "theme",
            "photos",
            "publishedAt",
        }
        assert BUYER["email"] not in page.text
        photo = await anonymous.get(data["photos"][0]["url"])
        assert photo.content == PNG
        qr = await anonymous.get(f"/api/v1/public/letters/{slug}/qr.png")
        assert qr.status_code == 200
        assert qr.content[:8] == b"\x89PNG\r\n\x1a\n"


async def test_draft_has_no_public_page(client, paid_purchase, app):
    letter = (await create_letter(client, paid_purchase)).json()
    assert (await client.get(f"/api/v1/letters/{letter['id']}/qr.png")).status_code == 409
    async with app.state.sessions() as db:
        slug = await db.scalar(select(Letter.public_slug))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as anonymous:
        assert (await anonymous.get(f"/api/v1/public/letters/{slug}")).status_code == 404


async def test_published_letter_is_frozen(client, paid_purchase):
    letter = (await create_letter(client, paid_purchase)).json()
    await publish(client, letter["id"])
    assert (
        await client.patch(f"/api/v1/letters/{letter['id']}", json={"body": "otro texto"})
    ).status_code == 409
    assert (
        await client.post(
            f"/api/v1/letters/{letter['id']}/photos", files={"file": ("a.png", PNG, "image/png")}
        )
    ).status_code == 409
    assert (await publish(client, letter["id"])).status_code == 409


async def test_mail_failure_then_retry_without_duplicating(client, paid_purchase, app):
    class FailingMailer(ConsoleMailer):
        async def send(self, message):
            raise TimeoutError("smtp timeout")

    app.state.mailer = FailingMailer()
    letter = (await create_letter(client, paid_purchase)).json()
    published = await publish(client, letter["id"])
    assert published.status_code == 200
    assert published.json()["deliveries"][0]["status"] == "failed"
    assert published.json()["deliveries"][0]["lastError"] == "TimeoutError"

    app.state.mailer = ConsoleMailer()
    retry = await client.post(f"/api/v1/letters/{letter['id']}/deliveries", json={})
    assert retry.status_code == 202
    assert retry.json()["status"] == "sent"
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(LetterDelivery)) == 2
        assert await db.scalar(select(func.count()).select_from(Letter)) == 1
        assert await db.scalar(select(func.count()).select_from(Purchase)) == 1


async def test_resend_to_another_address_keeps_one_purchase(client, paid_purchase, app):
    letter = (await create_letter(client, paid_purchase)).json()
    await publish(client, letter["id"])
    resend = await client.post(
        f"/api/v1/letters/{letter['id']}/deliveries", json={"recipientEmail": "otra@example.com"}
    )
    assert resend.status_code == 202
    assert resend.json()["recipientEmail"] == "otra@example.com"
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Purchase)) == 1


async def test_cannot_send_unpublished_letter(client, paid_purchase):
    letter = (await create_letter(client, paid_purchase)).json()
    response = await client.post(f"/api/v1/letters/{letter['id']}/deliveries", json={})
    assert response.status_code == 409
    assert response.json()["code"] == "LETTER_NOT_PUBLISHED"


# --- Cédula privada ------------------------------------------------------------------------


async def test_document_is_private_and_never_public(client, paid_purchase, app):
    document = {"documentType": "CC", "documentNumber": "1098765432"}
    saved = await client.put("/api/v1/me/identity-document", json=document)
    assert saved.status_code == 200
    assert saved.json() == {
        "documentType": "CC",
        "documentLast4": "5432",
        "createdAt": saved.json()["createdAt"],
    }
    assert document["documentNumber"] not in saved.text
    profile = await client.get("/api/v1/me")
    assert document["documentNumber"] not in profile.text

    letter = (await create_letter(client, paid_purchase)).json()
    slug = (await publish(client, letter["id"])).json()["publicSlug"]
    assert document["documentNumber"] not in slug
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as anonymous:
        page = await anonymous.get(f"/api/v1/public/letters/{slug}")
        assert document["documentNumber"] not in page.text


async def test_document_cannot_be_reused_by_another_account(client, buyer, app):
    document = {"documentType": "CC", "documentNumber": "1098765432"}
    assert (await client.put("/api/v1/me/identity-document", json=document)).status_code == 200
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as other:
        other.headers["X-CSRF-Token"] = (await other.get("/api/v1/auth/csrf")).json()["csrfToken"]
        await other.post("/api/v1/auth/register", json={**BUYER, "email": "otra@example.com"})
        await other.post(
            "/api/v1/auth/login", json={"email": "otra@example.com", "password": BUYER["password"]}
        )
        response = await other.put("/api/v1/me/identity-document", json=document)
        assert response.status_code == 409
        assert response.json()["code"] == "DOCUMENT_IN_USE"


async def test_document_is_not_a_credential(client, buyer):
    """La cédula no inicia sesión ni sustituye la contraseña."""
    await client.put(
        "/api/v1/me/identity-document", json={"documentType": "CC", "documentNumber": "1098765432"}
    )
    response = await client.post(
        "/api/v1/auth/login", json={"email": BUYER["email"], "password": "1098765432"}
    )
    assert response.status_code == 401


# --- Sesión y CSRF sobre el dominio comercial ------------------------------------------------


async def test_commerce_requires_session(client, app):
    assert (await client.get("/api/v1/purchases")).status_code == 401
    assert (
        await client.post("/api/v1/purchases", json={"idempotencyKey": "idem-key-0001"})
    ).status_code == 401
    assert (await client.get("/api/v1/letters")).status_code == 401


async def test_commerce_writes_require_csrf(client, buyer):
    del client.headers["X-CSRF-Token"]
    assert (
        await client.post("/api/v1/purchases", json={"idempotencyKey": "idem-key-0001"})
    ).status_code == 403
