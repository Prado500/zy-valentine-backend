from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.security import RateLimiter, issue_csrf, verify_csrf
from app.main import EXPECTED_REVISION
from app.services.google import GoogleVerifier


async def test_health_and_readiness(client, app):
    assert (await client.get("/")).status_code == 200
    assert (await client.get("/health/live")).status_code == 200
    assert (await client.get("/health/ready")).status_code == 200
    async with app.state.engine.begin() as conn:
        await conn.execute(text("UPDATE alembic_version SET version_num='wrong'"))
    try:
        assert (await client.get("/health/ready")).status_code == 503
        assert (await client.get("/health/live")).status_code == 200
    finally:
        async with app.state.engine.begin() as conn:
            await conn.execute(
                text("UPDATE alembic_version SET version_num=:revision"),
                {"revision": EXPECTED_REVISION},
            )


async def test_cors_and_openapi(client):
    response = await client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-csrf-token",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    denied = await client.options(
        "/api/v1/auth/login",
        headers={"Origin": "https://attacker.invalid", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in denied.headers
    schema = (await client.get("/openapi.json")).json()
    assert "/api/v1/auth/google" in schema["paths"]
    assert "/api/v1/me" in schema["paths"]


@pytest.mark.parametrize(
    "changes",
    [
        {"session_secret": "short"},
        {"cors_origins": ["*"]},
        {"cors_origins": ["https://example.com/path"]},
        {"cors_origins": []},
        {"app_env": "staging"},
        {"database_url": "sqlite:///test.db"},
        {"database_url": "postgresql+asyncpg://localhost/test?options=-csearch_path%3Dx"},
        {"database_url": "postgresql+asyncpg://localhost/test?sslmode=maybe"},
        {"web_concurrency": 10},
        {"cookie_samesite": "none"},
    ],
)
def test_settings_reject_unsafe_config(settings, changes):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{**settings.model_dump(), **changes})


REMOTE = {
    "app_env": "staging",
    "db_ssl_mode": "verify-full",
    "cors_origins": ["https://frontend.example.com"],
    "storage_backend": "azure",
    "azure_storage_connection_string": "UseDevelopmentStorage=true",
    "azure_container_name": "letters",
}


def test_remote_cookies_and_tls(settings):
    config = Settings(_env_file=None, **{**settings.model_dump(), **REMOTE})
    assert config.secure_cookies
    assert config.session_cookie.startswith("__Host-")
    assert config.csrf_cookie.startswith("__Host-")


def test_csrf_expiry(settings):
    secret = settings.session_secret.get_secret_value()
    with patch("time.time", return_value=1000):
        token = issue_csrf(secret)
    with patch("time.time", return_value=5000):
        assert not verify_csrf(secret, token, 60)


def test_rate_window_reset():
    limiter = RateLimiter(1, 60)
    with patch("time.monotonic", return_value=0):
        limiter.check("client")
        with pytest.raises(ApiError):
            limiter.check("client")
    with patch("time.monotonic", return_value=61):
        limiter.check("client")


@pytest.mark.parametrize(
    "changes",
    [
        {"nonce": "wrong"},
        {"email_verified": False},
        {"sub": ""},
        {"nonce": None},
    ],
)
def test_google_claim_validation(changes):
    verifier = GoogleVerifier()
    claims = {"sub": "sub", "email_verified": True, "nonce": "expected"}
    try:
        with patch(
            "app.services.google.id_token.verify_oauth2_token", return_value={**claims, **changes}
        ):
            with pytest.raises(ApiError) as result:
                verifier.verify("token", "client-id", "expected")
            assert result.value.status_code == 401
    finally:
        verifier.close()


def test_google_library_validation_and_audience():
    verifier = GoogleVerifier()
    try:
        with patch(
            "app.services.google.id_token.verify_oauth2_token",
            return_value={
                "sub": "sub",
                "email_verified": True,
                "nonce": "expected",
            },
        ) as mocked:
            assert verifier.verify("token", "client-id", "expected")["sub"] == "sub"
            assert mocked.call_args.args[0] == "token"
            assert mocked.call_args.args[2] == "client-id"
        with patch(
            "app.services.google.id_token.verify_oauth2_token",
            side_effect=ValueError("invalid signature/aud/exp"),
        ):
            with pytest.raises(ApiError):
                verifier.verify("invalid", "client-id", "expected")
    finally:
        verifier.close()
