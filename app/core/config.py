import hashlib
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.core.dsn import DsnError, split_ssl_query, strongest

# Variables heredadas del proyecto de referencia que esta API NO usa.
# Se documentan explícitamente para que su presencia en Azure no se interprete
# como que existe autenticación por JWT: la sesión es opaca y revocable.
LEGACY_UNUSED_VARIABLES = ("JWT_SECRET_KEY", "ALGORITHM", "ACCESS_TOKEN_EXPIRE_MINUTES")

# Pistas accionables por variable, para que un fallo de arranque se resuelva sin leer
# el código. Nunca incluyen valores: solo qué es la variable y cómo obtenerla.
CONFIG_HELP = {
    "session_secret": (
        "Secreto aleatorio de al menos 32 caracteres, idéntico en todas las réplicas. "
        'Genera uno con: python -c "import secrets; print(secrets.token_urlsafe(48))". '
        "Esta API no usa JWT: JWT_SECRET_KEY no lo sustituye."
    ),
    "database_url": (
        "Cadena postgresql+asyncpg://usuario:clave@servidor:5432/base. Admite "
        "?sslmode=require o ?ssl=require y los traduce a TLS verificado."
    ),
    "cors_origins": (
        'Array JSON de orígenes exactos, por ejemplo ["https://mi-frontend.example.com"]. '
        "Sin comodines ni rutas; en entornos remotos deben ser HTTPS."
    ),
    "frontend_url": (
        "Base del visor público; de aquí salen el enlace del correo y el código QR. "
        "En entornos remotos debe ser HTTPS."
    ),
    "app_env": "Uno de: local, develop, staging, production. La rama main es production.",
    "payment_provider": (
        "none (sin pagos, /verify responde 503), mercadopago (real) o fake (laboratorio "
        "que aprueba cualquier pago; solo con APP_ENV=local)."
    ),
    "azure_service_bus_connection_string": (
        "Cadena 'Endpoint=sb://...;SharedAccessKeyName=...;SharedAccessKey=...' de la "
        "política de la cola. Es OPCIONAL: sin ella la API escribe la carta de forma "
        "síncrona, como hasta ahora, y el worker no arranca."
    ),
    "service_bus_queue_name": (
        "Nombre de la cola que recibe las cartas. OPCIONAL: solo se usa junto con "
        "AZURE_SERVICE_BUS_CONNECTION_STRING; si falta una de las dos, la API degrada "
        "a escritura síncrona sin fallar el arranque."
    ),
    "pii_encryption_key": (
        "Clave AES-256 del número de documento: 32 caracteres o más. OPCIONAL, pero "
        "fíjala en staging y production antes del primer registro: si falta se deriva "
        "de SESSION_SECRET, y rotar ese secreto dejaría ilegibles los números ya "
        "cifrados, que son los que exige la factura electrónica de la DIAN."
    ),
    "storage_backend": (
        "En entornos remotos debe ser 'azure' con AZURE_STORAGE_CONNECTION_STRING y "
        "AZURE_CONTAINER_NAME: el disco del App Service no es durable."
    ),
}


class ConfigurationError(RuntimeError):
    """Fallo de configuración con un mensaje legible, sin volcado de pydantic."""


def format_settings_error(error: ValidationError, source: str) -> str:
    """Convierte el error de pydantic en un diagnóstico accionable.

    Solo se muestran nombres de variables y mensajes; nunca los valores recibidos
    (`hide_input_in_errors=True` ya los omite), así que el log del App Service no
    filtra secretos.
    """
    lines = [
        "",
        "=" * 72,
        f" La aplicación no puede arrancar: configuración inválida ({source})",
        "=" * 72,
    ]
    for item in error.errors():
        field = ".".join(str(part) for part in item["loc"])
        lines.append(f"  {field.upper() if field else 'CONFIGURACIÓN'}: {item['msg']}")
        hint = CONFIG_HELP.get(field)
        if hint:
            lines.append(f"      -> {hint}")
    lines += [
        "",
        " Define estas variables donde corre el proceso: en Azure, App Service >",
        " Configuración > Variables de entorno; en local, el archivo .env.",
        " La lista completa por entorno está en docs/MATRIZ_CONFIGURACION.md.",
        "=" * 72,
        "",
    ]
    return "\n".join(lines)


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
    # Azure App Service define WEBSITE_SITE_NAME automáticamente. Sirve para detectar
    # un despliegue real y exigir que APP_ENV se declare de forma explícita.
    website_site_name: str | None = None
    # Azure también define WEBSITE_HOSTNAME=<app>.azurewebsites.net. csrf_guard lo usa
    # como segunda fuente para reconocer el origen propio (Swagger en /docs) si el
    # Host llegara reescrito por un proxy.
    website_hostname: str | None = None

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
    # Clave AES-256-GCM del número de documento: el sobre reversible que permite
    # facturar ante la DIAN. Si falta se deriva de SESSION_SECRET; fijarla evita que
    # rotar el secreto de sesión deje ilegibles los números ya cifrados.
    pii_encryption_key: SecretStr | None = None

    # --- Compras y pagos ----------------------------------------------------------
    # "fake" es un proveedor de laboratorio que aprueba cualquier pago. El validador
    # de abajo lo prohíbe fuera de APP_ENV=local: jamás debe existir en un entorno
    # que cobre dinero de verdad.
    payment_provider: Literal["none", "mercadopago", "fake"] = "none"
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
    # Tarjeta QR en PDF adjunta al correo. El interruptor permite apagarla en caliente
    # si el render pesa demasiado en la B1ms; el tope es una red de seguridad (una
    # tarjeta normal ronda los 50 kB).
    letter_card_enabled: bool = True
    max_letter_card_bytes: int = Field(default=1_000_000, ge=100_000, le=5_000_000)
    # Origen público de esta API (https://api-….azurewebsites.net). Con él, el QR del
    # correo se carga como imagen remota desde /api/v1/public/letters/{slug}/qr.png, que
    # los clientes de correo muestran sin excepción; sin él viaja incrustado por
    # Content-ID, que algunos clientes no pintan. Opcional, pero recomendado en producción.
    api_public_url: str | None = None

    # --- Cola de eventos (Azure Service Bus) ------------------------------------------
    # Estrictamente opcionales: si faltan, `service_bus_enabled` es False y tanto la API
    # como el worker aplican degradación elegante (escritura síncrona / worker inactivo).
    # Nunca se valida su contenido en el arranque: una cadena mal formada degrada, no
    # tumba el App Service.
    azure_service_bus_connection_string: SecretStr | None = None
    service_bus_queue_name: str | None = None
    # Lote máximo por recepción. 12 escrituras por lote es el límite acordado para los
    # 240 IOPS del disco de la B1ms; subirlo satura la base antes que la cola.
    service_bus_max_batch: int = Field(default=12, ge=1, le=12)
    service_bus_max_wait: int = Field(default=5, ge=1, le=60)
    # Tope de la publicación desde la API. Publicar existe para responder rápido: si
    # la cola tarda más que esto, se degrada a escritura síncrona en vez de dejar al
    # comprador esperando por una infraestructura que no responde.
    service_bus_send_timeout: float = Field(default=3.0, ge=0.5, le=30.0)
    # Reintentos antes de mandar el mensaje a la dead-letter queue.
    service_bus_max_attempts: int = Field(default=5, ge=1, le=20)
    # Pool dedicado del worker. Tope 5 conexiones: quedan 15 del presupuesto para la API.
    worker_db_pool_size: int = Field(default=5, ge=1, le=5)
    worker_db_pool_timeout: int = Field(default=20, ge=1, le=60)
    worker_idle_backoff: int = Field(default=5, ge=1, le=60)

    # --- Reglas de negocio -----------------------------------------------------------
    freeze_letter_after_publish: bool = True

    @field_validator("cors_origins", "frontend_url", "api_public_url", mode="before")
    @classmethod
    def normalize_origins(cls, value):
        """Tolera lo que se pega a mano en el portal: espacios, barra final y mayúsculas.

        ``https://Front.example.com/`` pasa a ``https://front.example.com``, que es lo
        que el navegador envía en ``Origin``. Rutas, comodines y esquemas raros siguen
        fallando en ``_validate_origin``: solo se normaliza lo que no cambia el origen.
        """
        if isinstance(value, str):
            return _normalize_origin(value)
        if isinstance(value, list | tuple):
            return [_normalize_origin(item) for item in value]
        return value

    @model_validator(mode="after")
    def validate_runtime(self):
        if self.website_site_name and "app_env" not in self.model_fields_set:
            raise ValueError(
                "APP_ENV no está definido en un App Service. Sin él la aplicación "
                "arrancaría como 'local': cookies sin Secure ni prefijo __Host- y sin "
                "exigir TLS ni almacenamiento durable. Declara APP_ENV=develop, staging "
                "o production"
            )
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
        if self.api_public_url:
            _validate_origin(self.api_public_url, "API_PUBLIC_URL")
        if self.secure_cookies:
            # Mensajes específicos: un fallo de arranque debe decir qué variable arreglar.
            if self.db_ssl_mode != "verify-full":
                raise ValueError(
                    "DB_SSL_MODE debe ser 'verify-full' fuera de local, o DATABASE_URL "
                    "debe traer ?sslmode=require / ?ssl=require para elevarse a TLS "
                    "verificado"
                )
            insecure = sum(1 for o in self.cors_origins if not o.startswith("https://"))
            if insecure:
                raise ValueError(
                    f"CORS_ORIGINS debe contener solo orígenes HTTPS fuera de local; "
                    f"{insecure} de {len(self.cors_origins)} no lo son. ¿Quedó el valor "
                    "por defecto de desarrollo?"
                )
            if self.frontend_url and not self.frontend_url.startswith("https://"):
                raise ValueError("FRONTEND_URL debe ser HTTPS fuera de local")
        elif self.cookie_samesite == "none":
            raise ValueError("SameSite=None requires secure cookies")

        api = self.web_concurrency * self.app_replicas * (self.db_pool_size + self.db_max_overflow)
        # El worker solo existe cuando hay cola: sin ella no reserva conexiones y el
        # presupuesto de un despliegue ya en marcha no cambia.
        worker = self.worker_db_pool_size if self.service_bus_enabled else 0
        used = api + worker
        if used + self.db_reserved_connections > self.db_connection_budget:
            raise ValueError(
                f"WEB_CONCURRENCY({self.web_concurrency}) x APP_REPLICAS"
                f"({self.app_replicas}) x pool({self.db_pool_size + self.db_max_overflow})"
                f" = {api}, mas WORKER_DB_POOL_SIZE={worker} y "
                f"{self.db_reserved_connections} reservadas, supera "
                f"DB_CONNECTION_BUDGET={self.db_connection_budget}"
            )

        if self.payment_provider == "fake" and self.app_env != "local":
            raise ValueError(
                "PAYMENT_PROVIDER=fake aprueba cualquier pago sin cobrar: solo se admite "
                "con APP_ENV=local. Usa 'mercadopago' en develop, staging y production"
            )
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
            raise ValueError(
                "Fuera de local se exige almacenamiento durable: STORAGE_BACKEND=azure "
                "con AZURE_STORAGE_CONNECTION_STRING y AZURE_CONTAINER_NAME. El disco "
                "del App Service no conserva las fotos entre reinicios"
            )
        return self

    @property
    def service_bus_enabled(self) -> bool:
        """Solo con las dos variables presentes se publica en la cola.

        Es la única puerta de la arquitectura por eventos: si devuelve False, la API
        guarda la carta de forma síncrona y el worker se queda inactivo, sin que el
        arranque falle por una variable ausente.
        """
        return bool(self.azure_service_bus_connection_string and self.service_bus_queue_name)

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
    def pii_cipher_key(self) -> bytes:
        """Clave AES-256 del número de documento.

        Si no se define ``PII_ENCRYPTION_KEY`` se deriva del secreto de sesión, igual
        que hace ``pii_key``. **Ojo:** rotar ``SESSION_SECRET`` sin haber fijado antes
        ``PII_ENCRYPTION_KEY`` deja ilegibles los números ya cifrados.
        """
        secret = self.pii_encryption_key or self.session_secret
        return hashlib.sha256(b"zy-pii-aes-v1:" + secret.get_secret_value().encode()).digest()

    @property
    def sender_address(self) -> str:
        return self.mail_from or self.mail_username or "no-reply@zyvencore.local"


def _normalize_origin(origin):
    if not isinstance(origin, str):
        return origin
    return origin.strip().rstrip("/").lower()


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
    try:
        return Settings()
    except ValidationError as error:
        # `from None` evita el volcado de pydantic: en el log del App Service queda
        # el diagnóstico legible, no cincuenta líneas de traza interna.
        raise ConfigurationError(format_settings_error(error, "aplicación")) from None


@lru_cache
def get_migration_settings() -> MigrationSettings:
    try:
        return MigrationSettings()
    except ValidationError as error:
        raise ConfigurationError(format_settings_error(error, "migraciones")) from None
