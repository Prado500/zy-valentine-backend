from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.core.dsn import DsnError, split_ssl_query, strongest

# Variables heredadas del proyecto de referencia que esta API NO usa.
# Se documentan explícitamente para que su presencia en Azure no se interprete
# como que existe autenticación por JWT: la sesión es opaca y revocable.
LEGACY_UNUSED_VARIABLES = ("JWT_SECRET_KEY", "ALGORITHM", "ACCESS_TOKEN_EXPIRE_MINUTES")


def resolve_database_url(raw_url: str, app_env: str, configured_mode: str) -> tuple[str, str]:
    """Valida la URL y devuelve (url_sin_query, modo_tls_definitivo).

    Compartida por la configuración completa de la aplicación y por la
    configuración reducida de migraciones, para que ambas apliquen exactamente
    las mismas reglas de TLS.
    """
    try:
        clean_url, url_ssl_mode = split_ssl_query(raw_url, app_env)
    except DsnError as error:
        raise ValueError(str(error)) from None
    try:
        url = make_url(clean_url)
    except Exception:
        raise ValueError("DATABASE_URL must be a valid PostgreSQL URL") from None
    if url.drivername != "postgresql+asyncpg" or not url.host or not url.database:
        raise ValueError("DATABASE_URL must use postgresql+asyncpg with host and database")
    if url.query:
        raise ValueError("Use DB_SSL_MODE/DB_SSL_CA_FILE instead of URL query parameters")
    return clean_url, strongest(configured_mode, url_ssl_mode)


class MigrationSettings(BaseSettings):
    """Configuración mínima para ejecutar `alembic upgrade` en un contenedor.

    Una migración solo necesita llegar a la base de datos: no atiende peticiones,
    no emite cookies, no sube fotos ni envía correo. Exigirle `SESSION_SECRET`,
    `CORS_ORIGINS` o el almacenamiento de Azure obligaría a inyectar secretos que
    no usa. Las reglas de TLS son **las mismas** que en `Settings`.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    app_env: Literal["local", "develop", "staging", "production"] = "local"
    database_url: SecretStr
    db_ssl_mode: Literal["disable", "verify-full"] = "disable"
    db_ssl_ca_file: str | None = None
    db_pool_size: int = Field(default=1, ge=1, le=20)
    db_max_overflow: int = Field(default=0, ge=0, le=20)
    db_pool_timeout: int = Field(default=10, ge=1, le=60)

    @model_validator(mode="after")
    def validate_runtime(self):
        clean_url, ssl_mode = resolve_database_url(
            self.database_url.get_secret_value(), self.app_env, self.db_ssl_mode
        )
        self.database_url = SecretStr(clean_url)
        self.db_ssl_mode = ssl_mode
        if self.app_env != "local" and self.db_ssl_mode != "verify-full":
            raise ValueError("Remote environments require verified TLS for migrations")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    # --- Entorno y base de datos -------------------------------------------------
    app_env: Literal["local", "develop", "staging", "production"] = "local"
    database_url: SecretStr = SecretStr("postgresql+asyncpg://postgres@127.0.0.1:5432/zyvalentine")
    session_secret: SecretStr
    cors_origins: list[str] = ["http://localhost:5173"]
    frontend_url: str | None = None
    db_ssl_mode: Literal["disable", "verify-full"] = "disable"
    db_ssl_ca_file: str | None = None
    db_pool_size: int = Field(default=2, ge=1, le=20)
    db_max_overflow: int = Field(default=0, ge=0, le=20)
    db_pool_timeout: int = Field(default=10, ge=1, le=60)
    db_connection_budget: int = Field(default=20, ge=3, le=100)
    db_reserved_connections: int = Field(default=2, ge=1)
    web_concurrency: int = Field(default=1, ge=1)
    app_replicas: int = Field(default=1, ge=1)
    port: int = Field(default=8000, ge=1, le=65535)

    # --- Sesión y autenticación --------------------------------------------------
    session_minutes: int = Field(default=30, ge=1, le=1440)
    csrf_seconds: int = Field(default=3600, ge=60)
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    google_client_id: str | None = None
    auth_rate_limit: int = Field(default=30, ge=1, le=1000)
    auth_rate_window: int = Field(default=60, ge=1)

    # --- Datos personales sensibles ----------------------------------------------
    # Clave dedicada para el HMAC de la cédula. La cédula nunca es credencial ni
    # identificador público; solo se guarda su HMAC y los últimos dígitos.
    pii_hmac_key: SecretStr | None = None

    # --- Compras y pagos ----------------------------------------------------------
    payment_provider: Literal["none", "mercadopago"] = "none"
    mercadopago_access_token: SecretStr | None = None
    mercadopago_webhook_secret: SecretStr | None = None
    mercadopago_api_base: str = "https://api.mercadopago.com"
    purchase_amount_cents: int = Field(default=0, ge=0)
    purchase_currency: str = Field(default="COP", min_length=3, max_length=3)
    purchase_pending_minutes: int = Field(default=60, ge=5, le=1440)

    # --- Almacenamiento de fotos ---------------------------------------------------
    storage_backend: Literal["local", "azure"] = "local"
    local_storage_dir: str = ".local-storage"
    azure_storage_connection_string: SecretStr | None = None
    azure_container_name: str | None = None
    azure_temporal_container_name: str | None = None
    max_photo_bytes: int = Field(default=3_000_000, ge=1024, le=20_000_000)
    max_photos_per_letter: int = Field(default=6, ge=1, le=20)

    # --- Correo ---------------------------------------------------------------------
    mail_backend: Literal["console", "smtp"] = "console"
    mail_host: str = "smtp.gmail.com"
    mail_port: int = Field(default=587, ge=1, le=65535)
    mail_username: str | None = None
    mail_password: SecretStr | None = None
    mail_from: str | None = None
    mail_timeout: int = Field(default=10, ge=1, le=60)
    mail_max_attempts: int = Field(default=3, ge=1, le=10)

    # --- Reglas de negocio -----------------------------------------------------------
    freeze_letter_after_publish: bool = True

    @model_validator(mode="after")
    def validate_runtime(self):
        if len(self.session_secret.get_secret_value()) < 32:
            raise ValueError("SESSION_SECRET must contain at least 32 characters")
        # La URL almacenada queda sin query; TLS vive en DB_SSL_MODE y nunca se degrada.
        clean_url, ssl_mode = resolve_database_url(
            self.database_url.get_secret_value(), self.app_env, self.db_ssl_mode
        )
        self.database_url = SecretStr(clean_url)
        self.db_ssl_mode = ssl_mode

        if not self.cors_origins:
            raise ValueError("CORS_ORIGINS cannot be empty")
        for origin in self.cors_origins:
            _validate_origin(origin, "CORS_ORIGINS")
        if self.frontend_url:
            _validate_origin(self.frontend_url, "FRONTEND_URL")
        if self.secure_cookies:
            if self.db_ssl_mode != "verify-full" or any(
                not o.startswith("https://") for o in self.cors_origins
            ):
                raise ValueError("Remote environments require verified TLS and HTTPS origins")
            if self.frontend_url and not self.frontend_url.startswith("https://"):
                raise ValueError("Remote environments require an HTTPS FRONTEND_URL")
        elif self.cookie_samesite == "none":
            raise ValueError("SameSite=None requires secure cookies")

        used = self.web_concurrency * self.app_replicas * (self.db_pool_size + self.db_max_overflow)
        if used + self.db_reserved_connections > self.db_connection_budget:
            raise ValueError("Configured workers/replicas/pools exceed connection budget")

        if self.payment_provider == "mercadopago" and not self.mercadopago_access_token:
            raise ValueError(
                "MERCADOPAGO_ACCESS_TOKEN is required when PAYMENT_PROVIDER=mercadopago"
            )
        if self.payment_provider == "mercadopago" and not self.mercadopago_webhook_secret:
            raise ValueError("MERCADOPAGO_WEBHOOK_SECRET is required to verify webhooks")
        if self.storage_backend == "azure" and not (
            self.azure_storage_connection_string and self.azure_container_name
        ):
            raise ValueError("Azure storage requires connection string and container name")
        if self.mail_backend == "smtp" and not (self.mail_username and self.mail_password):
            raise ValueError("SMTP mail backend requires MAIL_USERNAME and MAIL_PASSWORD")
        if self.app_env != "local" and self.storage_backend == "local":
            raise ValueError("Remote environments must use durable storage (STORAGE_BACKEND=azure)")
        return self

    @property
    def secure_cookies(self) -> bool:
        return self.app_env != "local"

    @property
    def session_cookie(self) -> str:
        return "__Host-zy_session" if self.secure_cookies else "zy_session"

    @property
    def csrf_cookie(self) -> str:
        return "__Host-zy_csrf" if self.secure_cookies else "zy_csrf"

    @property
    def public_base_url(self) -> str:
        """Base del visor público de cartas; el correo y el QR apuntan aquí."""
        return (self.frontend_url or self.cors_origins[0]).rstrip("/")

    @property
    def pii_key(self) -> bytes:
        """Clave del HMAC de cédula; deriva del secreto de sesión si no se define."""
        secret = self.pii_hmac_key or self.session_secret
        return secret.get_secret_value().encode()

    @property
    def sender_address(self) -> str:
        return self.mail_from or self.mail_username or "no-reply@zyvencore.local"


def _validate_origin(origin: str, field: str) -> None:
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise ValueError(f"{field} must contain exact origins without paths")


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_migration_settings() -> MigrationSettings:
    return MigrationSettings()
