"""El texto legal es una fuente de verdad única, verificable y pública."""

import hashlib

import httpx

from app import legal


def test_checksum_matches_the_served_text():
    expected = hashlib.sha256(legal.TERMS_TEXT.encode("utf-8")).hexdigest()
    assert legal.TERMS_CHECKSUM == expected
    assert len(legal.TERMS_CHECKSUM) == 64


def test_text_is_normalized_so_the_checksum_is_platform_independent():
    """Git puede sacar CRLF en Windows; el checksum no puede depender de eso."""
    assert "\r" not in legal.TERMS_TEXT
    assert legal.TERMS_TEXT.strip()


def test_the_two_critical_clauses_are_present():
    lowered = legal.TERMS_TEXT.lower()
    assert "fotograf" in lowered
    assert "carta html" in lowered


def test_version_is_declared_and_matches_the_text():
    assert legal.TERMS_VERSION
    assert legal.TERMS_VERSION in legal.TERMS_TEXT
    assert legal.TERMS_KIND == "terms_and_privacy"


async def test_terms_endpoint_serves_the_same_text_that_gets_hashed(client):
    response = await client.get("/api/v1/public/legal/terms")
    assert response.status_code == 200
    assert response.json() == {
        "version": legal.TERMS_VERSION,
        "checksum": legal.TERMS_CHECKSUM,
        "content": legal.TERMS_TEXT,
    }


async def test_terms_are_public_and_cacheable(app):
    """Sin sesión ni CSRF, y sin el ``no-store`` que llevan las rutas con datos personales."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as anonymous:
        response = await anonymous.get("/api/v1/public/legal/terms")
    assert response.status_code == 200
    assert "public" in response.headers["cache-control"]
    assert "no-store" not in response.headers["cache-control"]


async def test_terms_endpoint_is_read_only(client):
    assert (await client.post("/api/v1/public/legal/terms")).status_code == 405


def test_legal_text_travels_with_the_package():
    """Como las fuentes: ``package-data`` lo declara o una instalación normal lo perdería."""
    from importlib.resources import files  # noqa: PLC0415

    from tests.conftest import REPO_ROOT  # noqa: PLC0415

    assert files("app.legal").joinpath("terms_v1.md").is_file()
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"app.legal"' in pyproject
