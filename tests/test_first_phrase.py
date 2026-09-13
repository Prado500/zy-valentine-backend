"""La frase de la dedicatoria que se asoma en la postal.

Port de ``firstPhrase`` del frontend, así que las pruebas cubren las mismas reglas: el
saludo no cuenta, los puntos suspensivos no cierran y lo largo se recorta.
"""

import pytest

from app.services.first_phrase import MAX_CHARS, first_phrase


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Te quiero mucho. Y más.", "Te quiero mucho"),
        ("Hola mi amor,\nTe quiero mucho. Y más.", "Te quiero mucho"),
        ("Querida Ana:\nGracias por todo.", "Gracias por todo"),
        ("Mi amor... no tengo palabras. Nunca.", "Mi amor... no tengo palabras"),
        ("¿Sabes qué? Eres todo.", "¿Sabes qué?"),
        ("¡Feliz aniversario!! Te amo.", "¡Feliz aniversario!!"),
        ("Sin cierre ninguno", "Sin cierre ninguno"),
        ("", ""),
        ("\n\n   \n", ""),
    ],
)
def test_happy_path_the_first_sentence_is_the_one_that_says_something(message, expected):
    assert first_phrase(message) == expected


def test_edge_a_greeting_alone_is_all_there_is():
    """Si solo hay saludo no se salta: quedarse sin frase sería peor."""
    assert first_phrase("Hola mi amor,") == "Hola mi amor,"


def test_edge_a_long_line_is_cut_at_the_last_space():
    phrase = first_phrase("palabra " * 40)

    assert phrase.endswith("…")
    assert len(phrase) <= MAX_CHARS + 1
    assert phrase.count("palabra") == len(phrase.split())


def test_edge_a_single_endless_word_is_cut_mid_word():
    phrase = first_phrase("x" * 200)

    assert phrase == "x" * MAX_CHARS + "…"


def test_the_line_breaks_and_double_spaces_collapse():
    assert first_phrase("Te   quiero\n\n   mucho") == "Te quiero mucho"
