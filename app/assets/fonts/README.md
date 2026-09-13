# Fuentes de la tarjeta QR

Las mismas familias que usa el frontend (`index.html` carga Playfair Display, Be Vietnam
Pro y Great Vibes desde Google Fonts). Aquí van vendorizadas porque el PDF se genera en
el servidor, sin red, y fpdf2 incrusta solo el subconjunto de glifos que usa cada carta.

| Archivo | Familia y peso | Origen |
| --- | --- | --- |
| `PlayfairDisplay-SemiBold.ttf` | Playfair Display 600 | Instancia estática (`wght=600`) generada con `fontTools.varLib.instancer` a partir de `ofl/playfairdisplay/PlayfairDisplay[wght].ttf` del repositorio `google/fonts` (2026-09-09). Se instancia una vez aquí y no en cada render. |
| `BeVietnamPro-Regular.ttf` | Be Vietnam Pro 400 | `ofl/bevietnampro/` de `google/fonts` (2026-09-09) |
| `BeVietnamPro-SemiBold.ttf` | Be Vietnam Pro 600 | `ofl/bevietnampro/` de `google/fonts` (2026-09-09) |
| `GreatVibes-Regular.ttf` | Great Vibes 400 | `ofl/greatvibes/` de `google/fonts` (2026-09-09) |

Las tres familias se distribuyen bajo la SIL Open Font License 1.1 (`OFL-*.txt`), que
permite incrustarlas en documentos y redistribuirlas con el software.

Ninguna incluye emojis ni el glifo `♥`: el renderizador filtra lo que la fuente no tiene
(`app/services/cards.py::printable`) y dibuja el corazón como motivo SVG.
