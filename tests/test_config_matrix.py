"""Compatibilidad entre las variables de Azure y app/core/config.py.

Cubre la incompatibilidad concreta: las cadenas guardadas en Azure llevan el modo
TLS en la query (``?sslmode=require`` / ``?ssl=require``) y la configuración lo
expresa en variables separadas. Aquí se comprueba que la traducción existe, que
eleva a verificación completa y que nunca degrada TLS fuera de local.
"""

import pytest
from pydantic import ValidationError

from app.core.config import (
    LEGACY_UNUSED_VARIABLES,
    ConfigurationError,
    MigrationSettings,
    Settings,
    get_migration_settings,
    get_settings,
)
from app.core.dsn import DsnError, split_ssl_query
from tests.conftest import BUYER

BASE = "postgresql+asyncpg://user:pass@srv.postgres.database.azure.com:5432/db_zv_develop"
REMOTE = {
    "app_env": "develop",
    "cors_origins": ["https://frontend.example.com"],
    "frontend_url": "https://frontend.example.com",
    "storage_backend": "azure",
    "azure_storage_connection_string": "UseDevelopmentStorage=true",
    "azure_container_name": "letters",
}


def build(settings, **changes):
    return Settings(_env_file=None, **{**settings.model_dump(), **changes})


# --- Traducción de la query TLS -----------------------------------------------------


@pytest.mark.parametrize("query", ["sslmode=require", "ssl=require", "sslmode=verify-full"])
def test_azure_query_becomes_verified_tls(settings, query):
    config = build(settings, **REMOTE, database_url=f"{BASE}?{query}")
    assert config.db_ssl_mode == "verify-full"
    # La URL que recibe asyncpg queda sin parámetros de consulta.
    assert "?" not in config.database_url.get_secret_value()


def test_url_without_query_keeps_configured_mode(settings):
    config = build(settings, **REMOTE, database_url=BASE, db_ssl_mode="verify-full")
    assert config.db_ssl_mode == "verify-full"


@pytest.mark.parametrize("query", ["sslmode=disable", "ssl=false", "sslmode=prefer"])
def test_remote_environment_rejects_tls_downgrade(settings, query):
    with pytest.raises(ValidationError):
        build(settings, **REMOTE, database_url=f"{BASE}?{query}")


def test_local_may_disable_tls(settings):
    clean, mode = split_ssl_query(f"{BASE}?sslmode=disable", "local")
    assert (clean, mode) == (BASE, "disable")
    with pytest.raises(DsnError):
        split_ssl_query(f"{BASE}?sslmode=disable", "production")


@pytest.mark.parametrize(
    "query", ["options=-csearch_path%3Dpublic", "sslmode=maybe", "sslrootcert=/tmp/ca.pem"]
)
def test_unexpected_query_parameters_are_rejected(query):
    with pytest.raises(DsnError):
        split_ssl_query(f"{BASE}?{query}", "develop")


def test_dsn_error_never_leaks_the_password():
    with pytest.raises(DsnError) as error:
        split_ssl_query(f"{BASE}?options=x", "develop")
    assert "pass" not in str(error.value)


# --- Matriz por entorno ---------------------------------------------------------------


@pytest.mark.parametrize("environment", ["develop", "staging", "production"])
def test_remote_environments_require_tls_https_and_durable_storage(settings, environment):
    remote = {**REMOTE, "app_env": environment, "database_url": f"{BASE}?sslmode=require"}
    config = build(settings, **remote)
    assert config.secure_cookies
    assert config.storage_backend == "azure"
    assert config.public_base_url == "https://frontend.example.com"
    with pytest.raises(ValidationError):
        build(settings, **{**remote, "storage_backend": "local"})
    with pytest.raises(ValidationError):
        build(settings, **{**remote, "frontend_url": "http://inseguro.example.com"})


def test_local_environment_defaults(settings):
    assert settings.app_env == "local"
    assert settings.secure_cookies is False
    assert settings.storage_backend == "local"
    assert settings.mail_backend == "console"
    assert settings.payment_provider == "none"
    assert settings.freeze_letter_after_publish is True


# --- Presupuesto de conexiones ----------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"web_concurrency": 4, "app_replicas": 3, "db_pool_size": 2},  # 24 + reserva > 20
        {"db_pool_size": 10, "db_max_overflow": 10},  # 20 + reserva > 20
        {"app_replicas": 10},
    ],
)
def test_connection_budget_is_enforced(settings, changes):
    with pytest.raises(ValidationError):
        build(settings, **changes)


def test_budget_accepts_declared_capacity(settings):
    config = build(
        settings,
        web_concurrency=2,
        app_replicas=4,
        db_pool_size=2,
        db_max_overflow=0,
        db_connection_budget=20,
        db_reserved_connections=2,
    )
    used = (
        config.web_concurrency
        * config.app_replicas
        * (config.db_pool_size + config.db_max_overflow)
    )
    assert used + config.db_reserved_connections <= config.db_connection_budget


# --- Integraciones exigidas cuando se activan ----------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"payment_provider": "mercadopago"},
        {"payment_provider": "mercadopago", "mercadopago_access_token": "token"},
        {"storage_backend": "azure"},
        {"storage_backend": "azure", "azure_storage_connection_string": "x"},
        {"mail_backend": "smtp"},
        {"mail_backend": "smtp", "mail_username": "correo@example.com"},
    ],
)
def test_integrations_require_their_own_secrets(settings, changes):
    with pytest.raises(ValidationError):
        build(settings, **changes)


def test_legacy_jwt_variables_are_ignored(settings, monkeypatch):
    """JWT_SECRET_KEY/ALGORITHM/ACCESS_TOKEN_EXPIRE_MINUTES no activan JWT."""
    for name in LEGACY_UNUSED_VARIABLES:
        monkeypatch.setenv(name, "valor-heredado")
    config = Settings(
        _env_file=None,
        database_url=settings.database_url.get_secret_value(),
        session_secret="test-only-secret-000000000000000000000",
    )
    dumped = config.model_dump()
    assert not any(key.startswith(("jwt", "algorithm", "access_token")) for key in dumped)


async def test_api_never_returns_a_bearer_token(client, buyer):
    """La sesión viaja solo en cookie HttpOnly; no hay token para el cliente."""
    response = await client.post(
        "/api/v1/auth/login", json={"email": buyer["email"], "password": BUYER["password"]}
    )
    assert "authorization" not in {k.lower() for k in response.headers}
    body = response.json()
    assert not any("token" in key.lower() for key in body)
    assert "HttpOnly" in response.headers["set-cookie"]


# --- Configuración reducida de migraciones (pipeline de CD) --------------------------------


def test_migrations_only_need_the_database_url(monkeypatch):
    """El CD ejecuta `alembic upgrade` con solo DATABASE_URL en el contenedor.

    Una migración no atiende peticiones ni emite cookies: exigirle SESSION_SECRET,
    CORS_ORIGINS o el almacenamiento de Azure obligaría a inyectar secretos que no usa.
    """
    for name in ("SESSION_SECRET", "CORS_ORIGINS", "STORAGE_BACKEND", "APP_ENV"):
        monkeypatch.delenv(name, raising=False)
    config = MigrationSettings(_env_file=None, database_url=f"{BASE}?ssl=require")
    assert config.db_ssl_mode == "verify-full"
    assert "?" not in config.database_url.get_secret_value()


@pytest.mark.parametrize("query", ["sslmode=require", "ssl=require", "sslmode=verify-full"])
def test_migrations_upgrade_azure_tls(query):
    """El CD reescribe `sslmode=` a `ssl=`; ambas formas llegan a TLS verificado."""
    config = MigrationSettings(_env_file=None, database_url=f"{BASE}?{query}", app_env="develop")
    assert config.db_ssl_mode == "verify-full"


@pytest.mark.parametrize(
    "changes",
    [
        {"database_url": f"{BASE}?sslmode=disable"},
        {"database_url": f"{BASE}?options=-csearch_path%3Dx"},
        {"database_url": "sqlite+aiosqlite:///:memory:"},
        # URL sin TLS y DB_SSL_MODE=disable: nada eleva el modo, así que se rechaza.
        {"database_url": BASE, "db_ssl_mode": "disable"},
    ],
)
def test_migrations_never_degrade_tls_outside_local(changes):
    payload = {"database_url": f"{BASE}?ssl=require", "app_env": "develop", **changes}
    with pytest.raises(ValidationError):
        MigrationSettings(_env_file=None, **payload)


# --- Diagnóstico de arranque (fallo real del App Service) ------------------------------


REMOTE_ENV = {
    "APP_ENV": "develop",
    "SESSION_SECRET": "0" * 40,
    "DATABASE_URL": f"{BASE}?sslmode=require",
    "FRONTEND_URL": "https://front.example.com",
    "CORS_ORIGINS": '["https://front.example.com"]',
    "STORAGE_BACKEND": "azure",
    "AZURE_STORAGE_CONNECTION_STRING": "UseDevelopmentStorage=true",
    "AZURE_CONTAINER_NAME": "cartas",
}


def load_settings(tmp_path, monkeypatch, **environment):
    """Construye Settings como lo haría un contenedor recién arrancado.

    Sin `.env` y sin heredar nada del proceso de pruebas: se parte de cero y solo
    existen las variables que declara el escenario.
    """
    monkeypatch.chdir(tmp_path)
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        return get_settings()
    finally:
        get_settings.cache_clear()


def test_missing_session_secret_explains_what_to_do(tmp_path, monkeypatch):
    """El App Service arrancó sin SESSION_SECRET y solo mostró una traza de pydantic."""
    with pytest.raises(ConfigurationError) as error:
        load_settings(
            tmp_path,
            monkeypatch,
            APP_ENV="develop",
            DATABASE_URL=f"{BASE}?sslmode=require",
            JWT_SECRET_KEY="heredada-del-proyecto-de-referencia",
        )
    message = str(error.value)
    assert "SESSION_SECRET" in message
    assert "JWT_SECRET_KEY no lo sustituye" in message
    assert "MATRIZ_CONFIGURACION.md" in message


def test_app_service_requires_an_explicit_app_env(tmp_path, monkeypatch):
    """Sin APP_ENV, un despliegue real arrancaría como 'local': cookies sin Secure."""
    with pytest.raises(ConfigurationError) as error:
        load_settings(
            tmp_path,
            monkeypatch,
            WEBSITE_SITE_NAME="api-zv-dev",
            SESSION_SECRET="0" * 40,
            DATABASE_URL=f"{BASE}?sslmode=require",
        )
    assert "APP_ENV" in str(error.value)


def test_app_service_boots_with_the_full_configuration(tmp_path, monkeypatch):
    config = load_settings(tmp_path, monkeypatch, WEBSITE_SITE_NAME="api-zv-dev", **REMOTE_ENV)
    assert config.session_cookie.startswith("__Host-")
    assert config.db_ssl_mode == "verify-full"
    assert config.storage_backend == "azure"


def test_startup_error_never_leaks_the_secret_value(tmp_path, monkeypatch):
    with pytest.raises(ConfigurationError) as error:
        load_settings(
            tmp_path,
            monkeypatch,
            APP_ENV="develop",
            SESSION_SECRET="secreto-demasiado-corto",
            DATABASE_URL=f"{BASE}?sslmode=require",
        )
    assert "secreto-demasiado-corto" not in str(error.value)


@pytest.mark.parametrize(
    ("broken", "expected"),
    [
        ({"CORS_ORIGINS": '["http://localhost:5173"]'}, "CORS_ORIGINS"),
        ({"FRONTEND_URL": "http://front.example.com"}, "FRONTEND_URL"),
        ({"STORAGE_BACKEND": "local"}, "STORAGE_BACKEND"),
        ({"WEB_CONCURRENCY": "10"}, "WEB_CONCURRENCY"),
        ({"DATABASE_URL": f"{BASE}?sslmode=disable"}, "sslmode=disable"),
    ],
)
def test_each_failure_names_the_variable_to_fix(tmp_path, monkeypatch, broken, expected):
    """Un fallo de arranque debe decir qué variable arreglar, no solo que algo falla."""
    with pytest.raises(ConfigurationError) as error:
        load_settings(tmp_path, monkeypatch, **{**REMOTE_ENV, **broken})
    assert expected in str(error.value)


def test_migration_configuration_errors_are_also_readable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_migration_settings.cache_clear()
    try:
        with pytest.raises(ConfigurationError) as error:
            get_migration_settings()
    finally:
        get_migration_settings.cache_clear()
    assert "DATABASE_URL" in str(error.value)
    assert "migraciones" in str(error.value)


def test_azure_storage_backend_is_installable_in_the_image(settings):
    """El artefacto de despliegue debe traer el SDK de Azure.

    Todo entorno remoto exige STORAGE_BACKEND=azure, así que si el extra `[azure]`
    desaparece del Dockerfile el contenedor arranca y muere en el lifespan. Aquí se
    comprueba que el backend se construye cuando el SDK está presente.
    """
    pytest.importorskip("azure.storage.blob", reason="extra opcional [azure] no instalado")
    from app.services.storage import AzureBlobStorage, build_storage  # noqa: PLC0415

    config = build(
        settings,
        **{
            **REMOTE,
            "azure_storage_connection_string": (
                "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=eHl6;"
                "EndpointSuffix=core.windows.net"
            ),
        },
        database_url=f"{BASE}?ssl=require",
    )
    assert isinstance(build_storage(config), AzureBlobStorage)
