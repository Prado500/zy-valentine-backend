"""El alta como acto legal: documento con código DIAN y consentimiento probado.

Regla de 10 sobre ``POST /api/v1/auth/register``: camino feliz, caminos tristes,
bordes del catálogo, concurrencia sobre el mismo documento y un fallo a mitad de la
transacción. Todo se mira desde fuera, por HTTP y por las filas que quedan; nada
llama al servicio directamente.
"""

import asyncio
import hashlib
import hmac
import uuid

import pytest
from sqlalchemy import select

from app import legal
from app.core import crypto, dian
from app.models.commerce import UserConsent, UserIdentityDocument
from app.models.user import User
from app.services.identity import document_fingerprint
from tests.conftest import BUYER

REGISTER = "/api/v1/auth/register"
DOCUMENT = "/api/v1/me/identity-document"


def payload(**changes):
    return {**BUYER, **changes}


async def rows(app, model):
    async with app.state.sessions() as db:
        return (await db.scalars(select(model))).all()


# 1. Camino feliz --------------------------------------------------------------------


async def test_register_stores_dian_code_and_proof_of_consent(client, app):
    client.headers["User-Agent"] = "Mozilla/5.0 (prueba)"
    response = await client.post(REGISTER, json=payload())
    assert response.status_code == 201, response.text
    (document,) = await rows(app, UserIdentityDocument)
    (consent,) = await rows(app, UserConsent)
    assert document.user_id == uuid.UUID(response.json()["id"])
    assert document.document_type == 13  # el código oficial, no la sigla "CC"
    assert document.document_last4 == "5432"
    assert document.document_cipher is not None
    assert consent.user_id == document.user_id
    assert consent.kind == legal.TERMS_KIND
    assert consent.document_version == legal.TERMS_VERSION
    assert consent.document_checksum == legal.TERMS_CHECKSUM
    assert consent.accepted_at is not None
    assert consent.ip_address == "127.0.0.1"
    assert consent.user_agent == "Mozilla/5.0 (prueba)"


# 2. Caminos tristes -----------------------------------------------------------------


@pytest.mark.parametrize("missing", ["acceptedTermsVersion", "documentType", "documentNumber"])
async def test_register_without_a_legal_field_creates_nothing(client, app, missing):
    body = payload()
    del body[missing]
    response = await client.post(REGISTER, json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert await rows(app, User) == []


async def test_register_with_stale_terms_version_is_rejected(client, app):
    """Aceptar una política que ya no es la nuestra no es consentimiento informado."""
    response = await client.post(REGISTER, json=payload(acceptedTermsVersion="1999-01-01"))
    assert response.status_code == 422
    assert response.json()["code"] == "TERMS_VERSION_MISMATCH"
    assert await rows(app, User) == []
    assert await rows(app, UserIdentityDocument) == []


@pytest.mark.parametrize("code", [0, 14, 99, "CC"])
async def test_codes_outside_the_catalogue_are_rejected(client, app, code):
    response = await client.post(REGISTER, json=payload(documentType=code))
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert await rows(app, User) == []


@pytest.mark.parametrize(
    ("document_type", "number"),
    [(13, "12AB5678"), (13, "1234"), (13, "1" * 21), (41, "AB-12345"), (13, "      ")],
)
async def test_document_format_depends_on_its_type(client, app, document_type, number):
    response = await client.post(
        REGISTER, json=payload(documentType=document_type, documentNumber=number)
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    if not number.strip().isdigit():
        assert number not in response.text  # la validación nunca devuelve la entrada
    assert await rows(app, User) == []


# 3. Bordes del catálogo --------------------------------------------------------------


@pytest.mark.parametrize("code", dian.CODES)
async def test_every_official_code_is_accepted(client, app, code):
    number = "AB123456" if code in (41, 42) else f"10{code}000001"
    response = await client.post(
        REGISTER,
        json=payload(email=f"u{code}@example.com", documentType=code, documentNumber=number),
    )
    assert response.status_code == 201, response.text
    (document,) = await rows(app, UserIdentityDocument)
    assert document.document_type == code


async def test_passport_is_stored_canonically(client, app):
    """Minúsculas y espacios no crean un documento distinto ni ensucian los últimos dígitos."""
    body = payload(documentType=41, documentNumber=" ab123456 ")
    response = await client.post(REGISTER, json=body)
    assert response.status_code == 201, response.text
    (document,) = await rows(app, UserIdentityDocument)
    assert document.document_last4 == "3456"
    settings = app.state.settings
    assert crypto.open_sealed(settings, document.user_id, document.document_cipher) == "AB123456"


# 4. Concurrencia ----------------------------------------------------------------------


async def test_concurrent_registration_with_same_document_keeps_one(client, app):
    results = await asyncio.gather(
        client.post(REGISTER, json=payload(email="a@example.com")),
        client.post(REGISTER, json=payload(email="b@example.com")),
    )
    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
    rejected = next(r for r in results if r.status_code == 409)
    assert rejected.json()["code"] == "REGISTRATION_CONFLICT"
    # La cuenta perdedora no queda a medias: ni usuario, ni documento, ni consentimiento.
    assert len(await rows(app, User)) == 1
    assert len(await rows(app, UserIdentityDocument)) == 1
    assert len(await rows(app, UserConsent)) == 1


# 5. Excepción a mitad de la transacción -----------------------------------------------


async def test_registration_is_all_or_nothing(client, app, monkeypatch):
    from app.services import consents

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(consents, "attach_consent", explode)
    with pytest.raises(RuntimeError):
        await client.post(REGISTER, json=payload())
    assert await rows(app, User) == []
    assert await rows(app, UserIdentityDocument) == []


# 6. El número nunca sale --------------------------------------------------------------


async def test_document_number_never_appears_in_any_response(client):
    created = await client.post(REGISTER, json=payload())
    assert created.status_code == 201
    assert BUYER["documentNumber"] not in created.text
    logged = await client.post(
        "/api/v1/auth/login", json={k: BUYER[k] for k in ("email", "password")}
    )
    assert logged.status_code == 200
    assert BUYER["documentNumber"] not in logged.text
    assert BUYER["documentNumber"] not in (await client.get("/api/v1/me")).text
    mine = await client.get(DOCUMENT)
    assert mine.status_code == 200
    assert mine.json() == {
        "documentType": 13,
        "documentLast4": "5432",
        "createdAt": mine.json()["createdAt"],
    }
    assert BUYER["documentNumber"] not in mine.text


# 7. El sobre cifrado se abre, y solo para su dueño --------------------------------------


async def test_cipher_is_readable_only_for_its_owner(client, app):
    assert (await client.post(REGISTER, json=payload())).status_code == 201
    (document,) = await rows(app, UserIdentityDocument)
    settings = app.state.settings
    opened = crypto.open_sealed(settings, document.user_id, document.document_cipher)
    assert opened == BUYER["documentNumber"]
    assert crypto.open_sealed(settings, uuid.uuid4(), document.document_cipher) is None


# 8. La huella heredada no cambia --------------------------------------------------------


def test_legacy_fingerprint_is_unchanged(settings):
    legacy = hmac.new(settings.pii_key, b"CC:1098765432", hashlib.sha256).hexdigest()
    assert document_fingerprint(settings, 13, "1098765432") == legacy
    cedula = dian.DocumentType.CEDULA_CIUDADANIA
    assert document_fingerprint(settings, cedula, "1098765432") == legacy


def test_same_number_under_another_type_is_another_document(settings):
    assert document_fingerprint(settings, 13, "1098765432") != document_fingerprint(
        settings, 12, "1098765432"
    )


# 9. Unicidad entre cuentas ----------------------------------------------------------------


async def test_second_account_with_the_same_document_is_rejected_generically(client, app):
    """El 409 no puede confirmar que esa cédula ya existe: sería un oráculo de enumeración."""
    assert (await client.post(REGISTER, json=payload())).status_code == 201
    response = await client.post(REGISTER, json=payload(email="otra@example.com"))
    assert response.status_code == 409
    assert response.json()["code"] == "REGISTRATION_CONFLICT"
    assert "documento" not in response.json()["message"].lower()
    assert len(await rows(app, User)) == 1


async def test_same_number_under_another_type_is_allowed(client, app):
    assert (await client.post(REGISTER, json=payload())).status_code == 201
    body = payload(email="otra@example.com", documentType=12)
    response = await client.post(REGISTER, json=body)
    assert response.status_code == 201, response.text
    assert len(await rows(app, UserIdentityDocument)) == 2


async def test_duplicate_email_still_answers_email_in_use(client):
    """El frontend usa este código para mandar a iniciar sesión; el contrato no cambia."""
    assert (await client.post(REGISTER, json=payload())).status_code == 201
    response = await client.post(REGISTER, json=payload(documentNumber="1055500009"))
    assert response.status_code == 409
    assert response.json()["code"] == "EMAIL_IN_USE"


# 10. Rastro de auditoría acotado, panel alineado con el alta y servicio listo -------------


async def test_consent_audit_trail_is_bounded(client, app):
    client.headers["User-Agent"] = "x" * 300
    assert (await client.post(REGISTER, json=payload())).status_code == 201
    (consent,) = await rows(app, UserConsent)
    assert len(consent.user_agent) == 255


async def test_document_endpoint_normalizes_like_registration(client, buyer, app):
    """El PUT del panel comparte esquema con el alta: mismo catálogo, misma forma canónica."""
    passport = {"documentType": 41, "documentNumber": " ab123456 "}
    response = await client.put(DOCUMENT, json=passport)
    assert response.status_code == 200, response.text
    assert response.json()["documentType"] == 41
    assert response.json()["documentLast4"] == "3456"
    invalid = await client.put(DOCUMENT, json={"documentType": 13, "documentNumber": "12AB5678"})
    assert invalid.status_code == 422
    (document,) = await rows(app, UserIdentityDocument)
    settings = app.state.settings
    assert crypto.open_sealed(settings, document.user_id, document.document_cipher) == "AB123456"


async def test_service_is_ready_after_the_migration(client):
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
