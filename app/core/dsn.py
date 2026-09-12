"""Normalización segura de DATABASE_URL.

Azure entrega cadenas con parámetros TLS en la query (``?sslmode=require`` o
``?ssl=require``). El motor asyncpg no acepta esos parámetros y la configuración
de esta aplicación describe TLS mediante DB_SSL_MODE/DB_SSL_CA_FILE. En vez de
rechazar la cadena o de pasarla tal cual, se acepta una lista blanca reducida de
parámetros y se traduce a configuración TLS **verificada**.

Reglas:

- Solo se aceptan las claves ``sslmode`` y ``ssl``. Cualquier otra clave se
  rechaza (no se ignora en silencio).
- Los valores que piden cifrado (``require``, ``verify-ca``, ``verify-full``,
  ``true``, ``on``, ``1``) se traducen a ``verify-full``. ``require`` en
  PostgreSQL cifra sin verificar la cadena ni el hostname; aquí se eleva
  deliberadamente a verificación completa, nunca se degrada.
- Los valores que desactivan o hacen opcional TLS (``disable``, ``allow``,
  ``prefer``, ``false``, ``off``, ``0``) solo se aceptan en APP_ENV=local; en
  develop/staging/production se rechaza el arranque.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ALLOWED_QUERY_KEYS = {"sslmode", "ssl"}
VERIFIED_VALUES = {"require", "verify-ca", "verify-full", "true", "on", "1"}
INSECURE_VALUES = {"disable", "allow", "prefer", "false", "off", "0"}


class DsnError(ValueError):
    """Error de DSN sin revelar la cadena de conexión ni su contraseña."""


def split_ssl_query(raw_url: str, app_env: str) -> tuple[str, str | None]:
    """Devuelve (url_sin_query, modo_tls_pedido_por_la_url | None).

    Nunca incluye la contraseña ni la cadena completa en el mensaje de error.
    """
    parts = urlsplit(raw_url)
    if not parts.query:
        return raw_url, None
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    unknown = sorted({key for key, _ in pairs} - ALLOWED_QUERY_KEYS)
    if unknown:
        raise DsnError(
            "DATABASE_URL solo admite los parámetros sslmode/ssl; "
            f"parámetros no permitidos: {', '.join(unknown)}"
        )
    requested: str | None = None
    for key, value in pairs:
        normalized = value.strip().casefold()
        if normalized in VERIFIED_VALUES:
            requested = "verify-full"
        elif normalized in INSECURE_VALUES:
            if app_env != "local":
                raise DsnError(
                    f"DATABASE_URL pide {key}={normalized}; fuera de local se exige TLS verificado"
                )
            requested = requested or "disable"
        else:
            raise DsnError(f"Valor no soportado para {key} en DATABASE_URL")
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode([]), parts.fragment))
    return clean, requested


def strongest(*modes: str | None) -> str:
    """Combina modos TLS quedándose con el más estricto (verify-full > disable)."""
    return "verify-full" if any(mode == "verify-full" for mode in modes) else "disable"
