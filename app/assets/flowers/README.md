# Flores de la postal

Las mismas imágenes que adornan la postal del QR en el frontend
(`src/assets/flores-web`, versiones WebP a 360 px del juego original en PNG). Se copian
aquí porque la postal también se dibuja en el servidor, para el correo y para el PDF.

**El número de carpeta es el tema, y el orden no coincide con el de la lista de estilos.**
Equivocarlo pone claveles donde iban girasoles:

| Carpeta | Tema |
| --- | --- |
| `tema 1` | `classic` |
| `tema 2` | `pastel-pink` |
| `tema 3` | `sunset` |
| `tema 4` | `starry` |
| `tema 5` | `lavender` |
| `tema 6` | `emerald` |
| `tema 7` | `midnight` |
| `tema 8` | `vintage` |

Son las mismas tres flores por tema (`flor_1`, `flor_2`, `flor_3`) y la postal usa cuatro
posiciones, así que una se repite: lo decide `CARD_FLOWERS` en
`app/services/qr_card.py`, igual que en el front.
