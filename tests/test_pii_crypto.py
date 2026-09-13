"""Catálogo DIAN y sobre cifrado del número de documento.

Regla de 10 sobre ``app.core.dian`` y ``app.core.crypto``: camino feliz, entradas
inválidas, bordes de longitud, dos cifrados del mismo número y sobres corruptos,
ajenos o cifrados con otra clave.
"""

import uuid

import pytest
from pydantic import SecretStr

from app.core import crypto, dian
from app.core.config import Settings

NUMBER = "1098765432"


def settings_like(base: Settings, **secrets: str) -> Settings:
    """Copia de la configuración de pruebas con algún secreto cambiado."""
    return base.model_copy(update={name: SecretStr(value) for name, value in secrets.items()})


# --- Catálogo ----------------------------------------------------------------------


def test_every_label_maps_to_its_official_code():
    assert dian.CODES == (11, 12, 13, 21, 22, 31, 41, 42, 91)
    assert dian.DocumentType.CEDULA_CIUDADANIA == 13
    assert dian.DocumentType.NIT == 31
    assert dian.DocumentType.NUIP == 91
    assert set(dian.LABELS) == set(dian.CODES)


def test_legacy_types_keep_their_historical_fingerprint_token():
    """Las huellas ya emitidas deben seguir valiendo: el token no puede cambiar."""
    assert dian.fingerprint_token(13) == "CC"
    assert dian.fingerprint_token(22) == "CE"
    assert dian.fingerprint_token(41) == "PA"
    assert dian.fingerprint_token(31) == "NIT"
    # Los que nunca existieron usan su propio código.
    assert dian.fingerprint_token(12) == "12"
    assert dian.fingerprint_token(91) == "91"


def test_tokens_never_collide():
    """Dos tipos con el mismo token compartirían huellas: un CC y un TI iguales chocarían."""
    tokens = [dian.fingerprint_token(code) for code in dian.CODES]
    assert len(tokens) == len(set(tokens))


@pytest.mark.parametrize("code", [11, 12, 13, 21, 22, 31, 91])
def test_numeric_types_reject_letters(code):
    with pytest.raises(ValueError):
        dian.normalize_number(code, "12AB5678")


@pytest.mark.parametrize("code", [41, 42])
def test_alphanumeric_types_accept_letters_and_are_uppercased(code):
    assert dian.normalize_number(code, " ab12345 ") == "AB12345"


@pytest.mark.parametrize("code", [41, 42])
def test_alphanumeric_types_reject_separators(code):
    with pytest.raises(ValueError):
        dian.normalize_number(code, "AB-12345")


def test_number_is_trimmed():
    assert dian.normalize_number(13, f"  {NUMBER}\n") == NUMBER


@pytest.mark.parametrize("length", [5, 20])
def test_length_boundaries_are_inclusive(length):
    assert dian.normalize_number(13, "1" * length) == "1" * length


@pytest.mark.parametrize("length", [0, 4, 21])
def test_lengths_outside_the_range_are_rejected(length):
    with pytest.raises(ValueError):
        dian.normalize_number(13, "1" * length)


def test_unknown_code_is_rejected():
    with pytest.raises(ValueError):
        dian.normalize_number(99, NUMBER)


# --- Sobre cifrado -----------------------------------------------------------------


def test_cipher_roundtrip(settings):
    user_id = uuid.uuid4()
    sealed = crypto.seal(settings, user_id, NUMBER)
    assert NUMBER.encode() not in sealed
    assert crypto.open_sealed(settings, user_id, sealed) == NUMBER


def test_cipher_is_bound_to_its_owner(settings):
    """La AAD es el usuario: un sobre copiado a otra fila no se abre."""
    sealed = crypto.seal(settings, uuid.uuid4(), NUMBER)
    assert crypto.open_sealed(settings, uuid.uuid4(), sealed) is None


def test_cipher_detects_tampering(settings):
    user_id = uuid.uuid4()
    sealed = bytearray(crypto.seal(settings, user_id, NUMBER))
    sealed[-1] ^= 0x01
    assert crypto.open_sealed(settings, user_id, bytes(sealed)) is None


def test_two_seals_of_the_same_number_differ(settings):
    """Nonce aleatorio: el cifrado no puede servir de índice ni delatar repetidos."""
    user_id = uuid.uuid4()
    assert crypto.seal(settings, user_id, NUMBER) != crypto.seal(settings, user_id, NUMBER)


@pytest.mark.parametrize("sealed", [None, b"", b"short", b"x" * crypto.NONCE_BYTES])
def test_open_sealed_tolerates_garbage(settings, sealed):
    """Una columna vacía o corrupta nunca revienta: simplemente no hay número."""
    assert crypto.open_sealed(settings, uuid.uuid4(), sealed) is None


def test_cipher_key_is_aes_256(settings):
    assert len(settings.pii_cipher_key) == 32


def test_explicit_encryption_key_takes_precedence(settings):
    pinned = settings_like(settings, pii_encryption_key="pinned-pii-key-00000000000000000000")
    user_id = uuid.uuid4()
    sealed = crypto.seal(pinned, user_id, NUMBER)
    assert pinned.pii_cipher_key != settings.pii_cipher_key
    assert crypto.open_sealed(settings, user_id, sealed) is None
    assert crypto.open_sealed(pinned, user_id, sealed) == NUMBER


def test_rotating_session_secret_without_pinned_key_orphans_old_seals(settings):
    """El riesgo que documenta la matriz: sin PII_ENCRYPTION_KEY la clave deriva de SESSION_SECRET."""
    user_id = uuid.uuid4()
    sealed = crypto.seal(settings, user_id, NUMBER)
    rotated = settings_like(settings, session_secret="another-session-secret-000000000000")
    assert crypto.open_sealed(rotated, user_id, sealed) is None


def test_pinned_key_survives_session_secret_rotation(settings):
    user_id = uuid.uuid4()
    before = settings_like(settings, pii_encryption_key="pinned-pii-key-00000000000000000000")
    after = settings_like(before, session_secret="another-session-secret-000000000000")
    assert crypto.open_sealed(after, user_id, crypto.seal(before, user_id, NUMBER)) == NUMBER
