"""Primera frase de la dedicatoria, la que se asoma en la postal del QR.

Port de ``src/utils/firstPhrase.ts``. Reglas, tal cual las fijó el frontend:

- Si el mensaje abre con un saludo corto en su propia línea ("Hola mi amor,",
  "Querida Ana:"), se salta: la frase que se enseña debe decir algo.
- La frase termina en el primer ``!``, ``?`` o punto simple. Los puntos suspensivos no
  cierran: "Mi amor... no tengo palabras" es una sola frase.
- Si pasa de 90 caracteres se corta en el último espacio y se cierra con "…".
- El punto final se quita —en cursiva sobra—; ``!`` y ``?`` se quedan.
"""

MAX_CHARS = 90
# Por debajo de esto no vale la pena cortar en un espacio.
MIN_CUT = 40
# Largo máximo de una línea para considerarla saludo.
GREETING_MAX = 32

_TRAILING = ",;:."


def _is_greeting(line: str) -> bool:
    return len(line) <= GREETING_MAX and line.endswith((",", ":"))


def _sentence_end(text: str) -> int:
    """Índice justo después del cierre de la primera frase, o -1 si no cierra."""
    index = 0
    total = len(text)
    while index < total:
        character = text[index]
        if character in "!?":
            last = index
            while last + 1 < total and text[last + 1] in "!?":
                last += 1
            if last + 1 >= total or text[last + 1] == " ":
                return last + 1
            index = last + 1
            continue
        if character == ".":
            if index + 1 < total and text[index + 1] == ".":
                # Puntos suspensivos: no cierran la frase.
                while index + 1 < total and text[index + 1] == ".":
                    index += 1
                index += 1
                continue
            if index + 1 >= total or text[index + 1] == " ":
                return index + 1
        index += 1
    return -1


def first_phrase(message: str) -> str:
    lines = [" ".join(line.split()) for line in (message or "").split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return ""

    start = 0
    while start < len(lines) - 1 and _is_greeting(lines[start]):
        start += 1
    text = " ".join(lines[start:])

    end = _sentence_end(text)
    phrase = text[:end] if end > 0 else text

    if len(phrase) > MAX_CHARS:
        cut = phrase[:MAX_CHARS]
        space = cut.rfind(" ")
        phrase = (cut[:space] if space > MIN_CUT else cut).rstrip(_TRAILING) + "…"

    return phrase.removesuffix(".").strip()
