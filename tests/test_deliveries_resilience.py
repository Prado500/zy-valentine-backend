"""La tarjeta QR es un extra: si falla, se apaga o pesa de más, el correo sale igual.

Son las mismas garantías que ya tenía el documento HTML adjunto, aplicadas al PDF:
ningún fallo del render deja una entrega en ``failed`` ni una carta sin correo.
"""

import logging

from app.services import deliveries
from tests.test_commerce import create_letter, publish


def attachment_names(app) -> list[str]:
    return [item.filename for item in app.state.mailer.sent[-1].attachments]


async def test_a_broken_card_render_does_not_break_the_delivery(
    client, paid_purchase, app, monkeypatch, caplog
):
    def explode(content):
        raise RuntimeError("fuente corrupta")

    monkeypatch.setattr(deliveries, "render_card_pdf", explode)
    letter = (await create_letter(client, paid_purchase)).json()

    with caplog.at_level(logging.WARNING, logger="app.deliveries"):
        published = await publish(client, letter["id"])

    assert published.status_code == 200
    assert published.json()["deliveries"][0]["status"] == "sent"
    assert attachment_names(app) == []  # sin tarjeta no hay adjunto, pero el correo salió
    assert "Tu tarjeta para imprimir" not in app.state.mailer.sent[-1].html  # no se promete
    assert any("tarjeta QR" in record.getMessage() for record in caplog.records)
    assert "RuntimeError" in caplog.text and "fuente corrupta" not in caplog.text  # solo el tipo


async def test_the_card_can_be_switched_off_without_a_deploy(
    client, paid_purchase, app, monkeypatch
):
    monkeypatch.setattr(app.state.settings, "letter_card_enabled", False)
    letter = (await create_letter(client, paid_purchase)).json()

    published = await publish(client, letter["id"])

    assert published.status_code == 200
    assert published.json()["deliveries"][0]["status"] == "sent"
    assert not [name for name in attachment_names(app) if name.endswith(".pdf")]
    assert "Para acompañar tu carta" in app.state.mailer.sent[-1].html  # los consejos siguen
    # Las rutas de descarga respetan el mismo interruptor.
    card = await client.get(f"/api/v1/letters/{letter['id']}/card.pdf")
    assert card.status_code == 503 and card.json()["code"] == "LETTER_CARD_DISABLED"
    postal = await client.get(f"/api/v1/letters/{letter['id']}/postal.png")
    assert postal.status_code == 503
    # El código del cuerpo no depende del interruptor: sin él no habría correo que enviar.
    assert app.state.mailer.sent[-1].inline[0].subtype == "png"


async def test_an_oversized_card_is_dropped_with_a_warning(
    client, paid_purchase, app, monkeypatch, caplog
):
    monkeypatch.setattr(app.state.settings, "max_letter_card_bytes", 10)
    letter = (await create_letter(client, paid_purchase)).json()

    with caplog.at_level(logging.WARNING, logger="app.deliveries"):
        published = await publish(client, letter["id"])

    assert published.json()["deliveries"][0]["status"] == "sent"
    assert not [name for name in attachment_names(app) if name.endswith(".pdf")]
    assert any("descartada por tamaño" in record.getMessage() for record in caplog.records)
