"""Escape de HTML para todo lo que escribe una persona.

El correo y el documento adjunto se arman concatenando cadenas, así que cada valor
que venga del formulario —título, cuerpo, nombre, pie de foto— cruza una frontera
de confianza al entrar en el HTML. Este módulo es el único punto donde se cruza.

Dos funciones, no una, porque el contexto manda:

- :func:`escape_text` para nodos de texto (``<p>aquí</p>``).
- :func:`escape_attr` para atributos (``href="aquí"``).

Ambas escapan **también** las comillas. ``html.escape`` deja fuera ``"`` y ``'``
salvo que se le pase ``quote=True``, y ese parámetro por defecto es la forma más
común de abrir un XSS por descuido: basta con que alguien mueva un valor de un
párrafo a un atributo y el escape deja de bastar. Aquí no existe ese matiz.

Ninguna de las dos intenta "limpiar" HTML ni permitir etiquetas seguras: el
contenido de una carta es texto plano y se escapa entero. El backend no construye
HTML a partir de datos de usuario en ningún otro sitio.
"""

from html import escape as _escape

# Separadores de línea Unicode (U+2028 y U+2029). Son inofensivos en HTML pero
# rompen literales de JavaScript. El documento no lleva scripts; se neutralizan
# igualmente para que añadir uno en el futuro no reabra el agujero. Se declaran
# por punto de código, no como carácter literal, para que ningún editor los
# convierta en un salto de línea real dentro de este archivo.
_LINE_SEPARATORS = {0x2028: "\n", 0x2029: "\n"}

# Controles C0/C1 que ningún texto de una carta necesita. El salto de línea y el
# tabulador se conservan porque el cuerpo de la carta los usa.
_STRIPPED = {code: None for code in [*range(0, 9), 11, 12, *range(14, 32), *range(127, 160)]}

# Lo que rompe una cabecera de correo: retorno, salto de línea y tabulador.
_HEADER_BREAKS = {0x0D: " ", 0x0A: " ", 0x09: " "}


def sanitize(value: object) -> str:
    """Normaliza a texto imprimible: sin controles y sin separadores exóticos."""
    text = "" if value is None else str(value)
    return text.translate(_LINE_SEPARATORS).translate(_STRIPPED)


def escape_text(value: object) -> str:
    """Valor listo para un nodo de texto. Escapa ``& < > " '``."""
    return _escape(sanitize(value), quote=True)


def escape_attr(value: object) -> str:
    """Valor listo para un atributo entrecomillado. Escapa ``& < > " '``."""
    return _escape(sanitize(value), quote=True)


def safe_header(value: object) -> str:
    """Valor para una cabecera de correo, como el asunto.

    Colapsa retornos, saltos de línea y tabuladores en un espacio. Un salto dentro
    del asunto cerraría la cabecera y permitiría añadir otras —un ``Bcc:``, por
    ejemplo—: es inyección de cabeceras SMTP. ``sanitize`` no sirve aquí porque
    conserva los saltos a propósito, que el cuerpo de la carta sí necesita.
    """
    return " ".join(sanitize(value).translate(_HEADER_BREAKS).split())


def safe_file_name(value: object, *, fallback: str, limit: int = 60) -> str:
    """Nombre de archivo para una cabecera MIME: sin rutas, sin comillas, sin saltos.

    Se construye con una lista blanca (alfanuméricos, espacio, guion y guion bajo)
    en lugar de eliminando lo peligroso: así un carácter nuevo o exótico se descarta
    por defecto en vez de colarse por no estar en la lista negra.
    """
    text = sanitize(value)
    kept = "".join(character for character in text if character.isalnum() or character in " -_")
    return kept.strip()[:limit] or fallback
