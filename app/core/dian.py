"""Catálogo oficial de tipos de documento de la DIAN.

La interfaz muestra etiquetas ("Cédula de ciudadanía"); la base de datos guarda
**solo** el código numérico oficial. Este módulo es el único sitio donde vive esa
correspondencia, para que no se escriba dos veces y acabe divergiendo.

El ``fingerprint_token`` merece explicación. El HMAC del número se calcula sobre
``"{token}:{numero}"``. Antes de la DIAN ese token era la sigla ("CC", "CE", "PA",
"NIT"), y como el número en claro no se persistía **las huellas ya emitidas no se
pueden recalcular**. Por eso los cuatro tipos heredados conservan su sigla: el
token es un espacio de nombres, no el tipo. Los cinco nuevos usan su código.
"""

import re
from enum import IntEnum


class DocumentType(IntEnum):
    """Códigos oficiales DIAN. El nombre es nuestro; el número es normativo."""

    REGISTRO_CIVIL = 11
    TARJETA_IDENTIDAD = 12
    CEDULA_CIUDADANIA = 13
    TARJETA_EXTRANJERIA = 21
    CEDULA_EXTRANJERIA = 22
    NIT = 31
    PASAPORTE = 41
    DOCUMENTO_EXTRANJERO = 42
    NUIP = 91


CODES: tuple[int, ...] = tuple(sorted(int(member) for member in DocumentType))

# Etiquetas exactas pedidas por el equipo legal. El frontend tiene su propia copia
# (fe/src/modules/legal/documentTypes.ts): si cambias una, cambia la otra.
LABELS: dict[int, str] = {
    11: "Registro civil",
    12: "Tarjeta de identidad",
    13: "Cédula de ciudadanía",
    21: "Tarjeta de extranjería",
    22: "Cédula de extranjería",
    31: "NIT",
    41: "Pasaporte",
    42: "Documento de identificación extranjero",
    91: "NUIP",
}

# Siglas heredadas de 0002_commerce. NO se tocan: ver el docstring del módulo.
_LEGACY_TOKENS: dict[int, str] = {13: "CC", 22: "CE", 41: "PA", 31: "NIT"}

# Pasaporte y documento extranjero llevan letras; el resto son solo dígitos.
_ALPHANUMERIC: frozenset[int] = frozenset({41, 42})

MIN_LENGTH = 5
MAX_LENGTH = 20

# ASCII a propósito: ``str.isdigit`` e ``isalnum`` aceptan dígitos y letras de
# cualquier alfabeto, y un número de documento nunca los lleva.
_DIGITS = re.compile(r"[0-9]+")
_LETTERS_AND_DIGITS = re.compile(r"[A-Z0-9]+")


def fingerprint_token(code: int) -> str:
    """Espacio de nombres del HMAC. Estable de por vida para cada código."""
    return _LEGACY_TOKENS.get(int(code), str(int(code)))


def normalize_number(code: int, number: str) -> str:
    """Limpia y valida el número según su tipo. Devuelve la forma canónica."""
    if code not in LABELS:
        raise ValueError("Tipo de documento desconocido")
    value = number.strip().upper()
    if not MIN_LENGTH <= len(value) <= MAX_LENGTH:
        raise ValueError(
            f"El número de documento debe tener entre {MIN_LENGTH} y {MAX_LENGTH} caracteres"
        )
    if code in _ALPHANUMERIC:
        if not _LETTERS_AND_DIGITS.fullmatch(value):
            raise ValueError("El número solo admite letras y dígitos")
    elif not _DIGITS.fullmatch(value):
        raise ValueError("Este tipo de documento solo admite dígitos")
    return value
