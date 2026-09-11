"""``parse_body``: el remitente y la canción viajan al final del cuerpo.

Es un port del ``parseBody`` del frontend, así que las pruebas cubren las mismas
reglas: marcas solo en la última línea, de atrás hacia delante, una vez cada una.
"""

import pytest

from app.services.letter_body import (
    DEFAULT_SENDER,
    ParsedBody,
    parse_body,
    sender_or_default,
    song_url_or_none,
)

SONG = "https://youtu.be/dQw4w9WgXcQ"


def test_happy_path_sender_and_song_in_the_order_the_form_writes_them():
    parsed = parse_body(f"Hola, Ana.\n\nTe quiero.\n\nDe parte de: Sebastián\n\nCanción: {SONG}")

    assert parsed == ParsedBody(
        message="Hola, Ana.\n\nTe quiero.", sender="Sebastián", song_url=SONG
    )


def test_happy_path_the_two_marks_are_found_in_either_order():
    parsed = parse_body(f"Hola\n\nCanción: {SONG}\n\nDe parte de: Ana")

    assert parsed == ParsedBody(message="Hola", sender="Ana", song_url=SONG)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Hola\n\nDe parte de: Ana", ParsedBody("Hola", "Ana", None)),
        (f"Hola\n\nCanción: {SONG}", ParsedBody("Hola", None, SONG)),
        (
            "Hola\nDe parte de:Ana",
            ParsedBody("Hola", "Ana", None),
        ),  # sin espacio ni línea en blanco
        ("Hola\r\n\r\nDe parte de: Ana\r\n", ParsedBody("Hola", "Ana", None)),  # cliente viejo
        ("De parte de: Ana", ParsedBody("", "Ana", None)),  # solo la firma
        ("Solo un mensaje", ParsedBody("Solo un mensaje", None, None)),
        ("", ParsedBody("", None, None)),
        (None, ParsedBody("", None, None)),
    ],
)
def test_edge_partial_and_empty_bodies(body, expected):
    assert parse_body(body) == expected


def test_sad_path_a_mark_in_the_middle_of_the_message_belongs_to_the_message():
    body = "De parte de: alguien\n\nEsto es lo que quería decirte.\n\nY nada más."

    assert parse_body(body) == ParsedBody(body, None, None)


def test_sad_path_each_mark_is_consumed_at_most_once():
    parsed = parse_body("Hola\n\nDe parte de: A\n\nDe parte de: B")

    assert parsed.sender == "B"
    assert parsed.message == "Hola\n\nDe parte de: A"


def test_edge_a_blank_signature_is_not_a_signature():
    """Como en el front: sin nada tras la marca no hay firma, y la línea se queda."""
    parsed = parse_body("Hola\n\nDe parte de:   ")

    assert parsed == ParsedBody("Hola\n\nDe parte de:", None, None)


def test_edge_the_sender_is_sanitized_and_bounded():
    parsed = parse_body("Hola\n\nDe parte de: A\x00na  " + "x" * 300)

    assert "\x00" not in parsed.sender
    assert parsed.sender.startswith("Ana")
    assert len(parsed.sender) == 120


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/x",
        "javascript:alert(1)",
        "http://youtube.com.evil.example/watch?v=1",
        "https://youtu.be/abc def",
        "ftp://youtube.com/x",
    ],
)
def test_exception_a_song_that_is_not_youtube_stays_as_text(url):
    parsed = parse_body(f"Hola\n\nCanción: {url}")

    assert parsed.song_url is None
    assert url in parsed.message
    assert song_url_or_none(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=abc",
        "https://m.youtube.com/watch?v=abc",
        "http://youtu.be/abc",
    ],
)
def test_happy_path_youtube_hosts_are_accepted(url):
    assert song_url_or_none(f"  {url}  ") == url


def test_sender_or_default_keeps_the_choice_of_not_signing():
    assert sender_or_default(None) == DEFAULT_SENDER
    assert sender_or_default("") == DEFAULT_SENDER
    assert sender_or_default("Ana") == "Ana"
