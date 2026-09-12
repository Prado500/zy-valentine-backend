"""``LetterUpdate``: un ``null`` explícito significa "no tocar", nunca un 500.

Regresión del hallazgo CWE-20 de la auditoría previa a "Mis dedicatorias": los
validadores heredados de ``LetterInput`` recibían ``None`` en los campos opcionales de
``LetterUpdate`` y morían en ``str.replace`` con ``AttributeError``, que el manejador
global convierte en 500 ``INTERNAL_ERROR``. Un cuerpo válido por tipo no puede tumbar la
petición: o se acepta, o es un 422 con el campo señalado.
"""

import pytest
from pydantic import ValidationError

from app.schemas.commerce import LetterInput, LetterUpdate

LETTER = {
    "title": "Para ti 💌",
    "recipientName": "Ana",
    "recipientEmail": "ana@example.com",
    "body": "Primera línea\nSegunda línea con emoji 🌹",
    "theme": "classic",
}


def errors_of(error: ValidationError) -> list[tuple[str, str]]:
    return [(".".join(map(str, item["loc"])), item["type"]) for item in error.errors()]


def test_happy_path_partial_update_only_carries_what_it_mentions():
    update = LetterUpdate(body="Texto corregido 🌙\nsegunda")
    assert update.model_dump(exclude_none=True) == {"body": "Texto corregido 🌙\nsegunda"}
    assert update.title is None
    assert update.theme is None


def test_sad_path_blank_text_is_a_422_not_a_crash():
    with pytest.raises(ValidationError) as error:
        LetterUpdate(title="   ")
    assert ("title", "value_error") in errors_of(error.value)
    with pytest.raises(ValidationError):
        LetterUpdate(body="\n\n")


@pytest.mark.parametrize("field", ["title", "recipientName", "body"])
def test_edge_explicit_null_means_do_not_touch(field):
    """Regresión: antes ``{"title": null}`` reventaba con ``AttributeError`` → 500."""
    update = LetterUpdate.model_validate({field: None})
    assert getattr(update, field) is None
    assert update.model_dump(exclude_none=True) == {}


def test_edge_control_characters_are_refused_and_windows_newlines_normalised():
    assert LetterUpdate(body="uno\r\ndos").body == "uno\ndos"
    with pytest.raises(ValidationError):
        LetterUpdate(title="mal\x00")
    with pytest.raises(ValidationError):
        LetterUpdate(recipientName="Ana\x07")


def test_exception_required_fields_still_reject_null_and_unknown_keys():
    """En ``LetterInput`` los campos siguen siendo obligatorios: ``null`` es 422, no 500."""
    with pytest.raises(ValidationError) as error:
        LetterInput.model_validate({**LETTER, "title": None})
    assert ("title", "string_type") in errors_of(error.value)
    with pytest.raises(ValidationError) as error:
        LetterUpdate.model_validate({"title": "ok", "purchaseId": "x"})
    assert ("purchaseId", "extra_forbidden") in errors_of(error.value)


# --- HTTP (integración, requiere TEST_DATABASE_URL) ----------------------------------------


async def test_patch_with_nulls_answers_200_and_changes_nothing(client, paid_purchase):
    created = await client.post(
        "/api/v1/letters", json={**LETTER, "purchaseId": paid_purchase["id"], "autoPublish": False}
    )
    assert created.status_code == 201, created.text
    letter = created.json()

    response = await client.patch(
        f"/api/v1/letters/{letter['id']}",
        json={"title": None, "body": None, "recipientName": None},
    )

    assert response.status_code == 200, response.text
    assert response.json()["title"] == letter["title"]
    assert response.json()["body"] == letter["body"]
    assert response.json()["recipientName"] == letter["recipientName"]
