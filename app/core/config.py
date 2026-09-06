from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    app_env: Literal["local", "develop", "staging", "production"] = "local"
    database_url: SecretStr = SecretStr("postgresql+asyncpg://postgres@127.0.0.1:5432/zyvalentine")
    session_secret: SecretStr
    cors_origins: list[str] = ["http://localhost:5173"]
    db_ssl_mode: Literal["disable", "verify-full"] = "disable"
    db_ssl_ca_file: str | None = None
    db_pool_size: int = Field(default=2, ge=1, le=20)
    db_max_overflow: int = Field(default=0, ge=0, le=20)
    db_pool_timeout: int = Field(default=10, ge=1, le=60)
    db_connection_budget: int = Field(default=20, ge=3, le=100)
    db_reserved_connections: int = Field(default=2, ge=1)
    web_concurrency: int = Field(default=1, ge=1)
    app_replicas: int = Field(default=1, ge=1)
    session_minutes: int = Field(default=30, ge=1, le=1440)
    csrf_seconds: int = Field(default=3600, ge=60)
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    google_client_id: str | None = None
    auth_rate_limit: int = Field(default=30, ge=1, le=1000)
    auth_rate_window: int = Field(default=60, ge=1)

    @model_validator(mode="after")
    def validate_runtime(self):
        if len(self.session_secret.get_secret_value()) < 32:
            raise ValueError("SESSION_SECRET must contain at least 32 characters")
        try:
            url = make_url(self.database_url.get_secret_value())
        except Exception:
            raise ValueError("DATABASE_URL must be a valid PostgreSQL URL") from None
        if url.drivername != "postgresql+asyncpg" or not url.host or not url.database:
            raise ValueError("DATABASE_URL must use postgresql+asyncpg with host and database")
        if url.query:
            raise ValueError("Use DB_SSL_MODE/DB_SSL_CA_FILE instead of URL query parameters")
        for origin in self.cors_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
            ):
                raise ValueError("CORS_ORIGINS must contain exact origins without paths")
        if not self.cors_origins:
            raise ValueError("CORS_ORIGINS cannot be empty")
        if self.secure_cookies:
            if self.db_ssl_mode != "verify-full" or any(
                not o.startswith("https://") for o in self.cors_origins
            ):
                raise ValueError("Remote environments require verified TLS and HTTPS origins")
        elif self.cookie_samesite == "none":
            raise ValueError("SameSite=None requires secure cookies")
        used = self.web_concurrency * self.app_replicas * (self.db_pool_size + self.db_max_overflow)
        if used + self.db_reserved_connections > self.db_connection_budget:
            raise ValueError("Configured workers/replicas/pools exceed connection budget")
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
