import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import select, update

from app.core.errors import ApiError
from app.models.user import AuthSession, User

ACCOUNT = {"email": "buyer@example.com", "password": "pw-buyer1", "name": "Buyer 💌"}


async def test_concurrent_registration_has_one_account(client, app):
    results = await asyncio.gather(
        *[client.post("/api/v1/auth/register", json=ACCOUNT) for _ in range(2)]
    )
    assert sorted(r.status_code for r in results) == [201, 409]
    async with app.state.sessions() as db:
        assert len((await db.scalars(select(User))).all()) == 1


async def test_concurrent_google_first_login_has_one_identity(client, app):
    setup_google(app)
    results = await asyncio.gather(
        *[client.post("/api/v1/auth/google", json={"credential": "mocked"}) for _ in range(2)]
    )
    assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
    assert results[0].json()["id"] == results[1].json()["id"]


async def register_login(client):
    assert (await client.post("/api/v1/auth/register", json=ACCOUNT)).status_code == 201
    return await client.post(
        "/api/v1/auth/login", json={k: ACCOUNT[k] for k in ("email", "password")}
    )


async def test_register_login_profile_logout(client, app):
    response = await register_login(client)
    assert response.status_code == 200
    profile = response.json()
    assert profile["emailVerified"] is False
    assert set(profile) == {"id", "email", "name", "emailVerified", "createdAt"}
    assert "HttpOnly" in response.headers["set-cookie"]
    token = client.cookies.get("zy_session")
    async with app.state.sessions() as db:
        user = await db.scalar(select(User))
        session = await db.scalar(select(AuthSession))
        assert user.password_hash.startswith("$argon2")
        assert ACCOUNT["password"] not in user.password_hash
        assert token != session.token_hash
    assert (await client.get("/api/v1/me")).json()["id"] == profile["id"]
    assert (await client.post("/api/v1/auth/logout")).status_code == 200
    assert (await client.get("/api/v1/me")).status_code == 401
    client.cookies.set("zy_session", token)
    assert (await client.get("/api/v1/me")).status_code == 401
    assert (await client.post("/api/v1/auth/logout")).status_code == 200


async def test_duplicate_email_casefold(client):
    assert (await client.post("/api/v1/auth/register", json=ACCOUNT)).status_code == 201
    result = await client.post(
        "/api/v1/auth/register", json={**ACCOUNT, "email": "BUYER@example.com"}
    )
    assert result.status_code == 409


@pytest.mark.parametrize(
    "changes",
    [
        {"password": "abc"},
        {"email": "not-email"},
        {"role": "admin"},
        {"name": "   "},
        {"name": "x" * 121},
        {"password": "x" * 11},
    ],
)
async def test_register_validation_never_echoes_input(client, changes):
    response = await client.post("/api/v1/auth/register", json={**ACCOUNT, **changes})
    assert response.status_code == 422
    assert ACCOUNT["password"] not in response.text
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert response.json()["requestId"] == response.headers["x-request-id"]


async def test_login_generic_error(client):
    await client.post("/api/v1/auth/register", json=ACCOUNT)
    wrong = await client.post(
        "/api/v1/auth/login", json={"email": ACCOUNT["email"], "password": "wrong"}
    )
    absent = await client.post(
        "/api/v1/auth/login", json={"email": "absent@example.com", "password": "wrong"}
    )
    assert wrong.status_code == absent.status_code == 401
    assert wrong.json()["code"] == absent.json()["code"] == "INVALID_CREDENTIALS"


async def test_deactivated_user_cannot_use_session_or_login(client, app):
    await register_login(client)
    async with app.state.sessions() as db:
        await db.execute(update(User).values(is_active=False))
        await db.commit()
    assert (await client.get("/api/v1/me")).status_code == 401
    assert (
        await client.post("/api/v1/auth/login", json={k: ACCOUNT[k] for k in ("email", "password")})
    ).status_code == 401


async def test_expired_and_forged_session(client, app):
    await register_login(client)
    async with app.state.sessions() as db:
        await db.execute(
            update(AuthSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()
    assert (await client.get("/api/v1/me")).status_code == 401
    client.cookies.clear()
    client.cookies.set("zy_session", "forged")
    assert (await client.get("/api/v1/me")).status_code == 401


async def test_login_rotates_session(client, app):
    await register_login(client)
    previous = client.cookies.get("zy_session")
    await client.post("/api/v1/auth/login", json={k: ACCOUNT[k] for k in ("email", "password")})
    assert previous != client.cookies.get("zy_session")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies={"zy_session": previous},
    ) as other:
        assert (await other.get("/api/v1/me")).status_code == 401


async def test_other_browser_has_no_profile(client, app):
    await register_login(client)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as other:
        assert (await other.get("/api/v1/me")).status_code == 401
        assert (await other.get("/api/v1/users")).status_code == 404


@pytest.mark.parametrize("mode", ["missing", "mismatch", "untrusted_origin"])
async def test_csrf_required(client, mode):
    if mode == "missing":
        del client.headers["X-CSRF-Token"]
    elif mode == "mismatch":
        client.headers["X-CSRF-Token"] = "forged"
    else:
        client.headers["Origin"] = "https://attacker.invalid"
    response = await client.post("/api/v1/auth/register", json=ACCOUNT)
    assert response.status_code == 403


async def test_csrf_forged_cookie_and_header(client):
    client.cookies.clear()
    client.cookies.set("zy_csrf", "forged")
    client.headers["X-CSRF-Token"] = "forged"
    assert (await client.post("/api/v1/auth/register", json=ACCOUNT)).status_code == 403


async def test_rate_limit(client, app):
    app.state.rate_limiter.limit = 1
    body = {"email": ACCOUNT["email"], "password": "wrong"}
    assert (await client.post("/api/v1/auth/login", json=body)).status_code == 401
    assert (await client.post("/api/v1/auth/login", json=body)).status_code == 429


async def test_google_not_configured(client):
    result = await client.post("/api/v1/auth/google", json={"credential": "invalid"})
    assert result.status_code == 503
    assert result.json()["code"] == "GOOGLE_NOT_CONFIGURED"


def setup_google(app, claims=None):
    app.state.settings.google_client_id = "test-client"
    app.state.google_verifier.verify = Mock(
        return_value=claims
        or {
            "sub": "google-sub-test",
            "email": "google@example.com",
            "name": "Google Buyer",
            "email_verified": True,
        }
    )


async def test_google_first_and_returning_login(client, app):
    setup_google(app)
    first = await client.post("/api/v1/auth/google", json={"credential": "mocked-token"})
    assert first.status_code == 200
    assert first.json()["emailVerified"] is True
    again = await client.post("/api/v1/auth/google", json={"credential": "mocked-token"})
    assert again.json()["id"] == first.json()["id"]
    app.state.google_verifier.verify.assert_called_with(
        "mocked-token", "test-client", client.headers["X-CSRF-Token"]
    )
    assert (await client.get("/api/v1/me")).status_code == 200


async def test_google_never_auto_links_email(client, app):
    await client.post("/api/v1/auth/register", json=ACCOUNT)
    setup_google(app, {"sub": "new-google", "email": ACCOUNT["email"], "email_verified": True})
    response = await client.post("/api/v1/auth/google", json={"credential": "mocked"})
    assert response.status_code == 409
    assert response.json()["code"] == "ACCOUNT_LINK_REQUIRED"


async def test_google_invalid_and_unavailable(client, app):
    setup_google(app)
    app.state.google_verifier.verify.side_effect = ApiError(401, "INVALID_GOOGLE_TOKEN", "Invalid")
    assert (
        await client.post("/api/v1/auth/google", json={"credential": "invalid"})
    ).status_code == 401
    app.state.google_verifier.verify.side_effect = ApiError(
        503, "GOOGLE_UNAVAILABLE", "Unavailable"
    )
    assert (
        await client.post("/api/v1/auth/google", json={"credential": "invalid"})
    ).status_code == 503


async def test_google_disabled_user(client, app):
    setup_google(app)
    await client.post("/api/v1/auth/google", json={"credential": "mocked"})
    async with app.state.sessions() as db:
        await db.execute(update(User).values(is_active=False))
        await db.commit()
    assert (
        await client.post("/api/v1/auth/google", json={"credential": "mocked"})
    ).status_code == 401


async def test_google_account_cannot_login_with_password(client, app):
    setup_google(app)
    await client.post("/api/v1/auth/google", json={"credential": "mocked"})
    response = await client.post(
        "/api/v1/auth/login", json={"email": "google@example.com", "password": "arbitrary"}
    )
    assert response.status_code == 401
