"""Real local RSA signatures with mocked Google certificate transport, never live Google."""

import time
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import crypt, jwt
from google.auth.exceptions import TransportError

from app.core.errors import ApiError
from app.services.google import GoogleVerifier


@pytest.fixture
def signing_material():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return crypt.RSASigner.from_string(private, key_id="local-test"), {"local-test": public}


@pytest.mark.parametrize(
    "variation", ["valid", "aud", "iss", "expired", "signature", "unicode_nonce"]
)
def test_real_verification(signing_material, variation):
    signer, certs = signing_material
    now = int(time.time())
    claims = {
        "sub": "local-test-user",
        "aud": "expected-client",
        "iss": "https://accounts.google.com",
        "iat": now - 60,
        "exp": now + 60,
        "email_verified": True,
        "nonce": "expected",
    }
    if variation == "aud":
        claims["aud"] = "another-client"
    if variation == "iss":
        claims["iss"] = "https://attacker.invalid"
    if variation == "expired":
        claims.update(iat=now - 120, exp=now - 60)
    if variation == "unicode_nonce":
        claims["nonce"] = "ñ"
    token = jwt.encode(signer, claims).decode()
    if variation == "signature":
        parts = token.split(".")
        parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
        token = ".".join(parts)
    verifier = GoogleVerifier()
    try:
        with patch("google.oauth2.id_token._fetch_certs", return_value=certs):
            if variation == "valid":
                assert (
                    verifier.verify(token, "expected-client", "expected")["sub"]
                    == "local-test-user"
                )
            else:
                with pytest.raises(ApiError) as error:
                    verifier.verify(token, "expected-client", "expected")
                assert error.value.status_code == 401
    finally:
        verifier.close()


def test_google_transport_failure():
    verifier = GoogleVerifier()
    try:
        with patch("google.oauth2.id_token._fetch_certs", side_effect=TransportError("offline")):
            with pytest.raises(ApiError) as error:
                verifier.verify("token", "client", "nonce")
            assert error.value.status_code == 503
    finally:
        verifier.close()
