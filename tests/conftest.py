import os
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import text

# Tests must never inherit an arbitrary deployed DATABASE_URL.
test_url = os.environ.get("TEST_DATABASE_URL", "")
parsed = urlsplit(test_url)
if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path != "/zy_auth_validation":
    raise RuntimeError(
        "Use scripts/validate_local.py: dedicated loopback TEST_DATABASE_URL required"
    )
os.environ["DATABASE_URL"] = test_url
os.environ["APP_ENV"] = "local"
os.environ["DB_SSL_MODE"] = "disable"
os.environ["SESSION_SECRET"] = "test-only-secret-000000000000000000000"
from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        database_url=test_url,
        session_secret="test-only-secret-000000000000000000000",
        cors_origins=["http://localhost:5173"],
        auth_rate_limit=100,
    )


@pytest.fixture
async def app(settings):
    instance = create_app(settings)
    async with instance.router.lifespan_context(instance):
        async with instance.state.engine.begin() as conn:
            await conn.execute(text("TRUNCATE users CASCADE"))
        yield instance


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        token = (await client.get("/api/v1/auth/csrf")).json()["csrfToken"]
        client.headers["X-CSRF-Token"] = token
        yield client
