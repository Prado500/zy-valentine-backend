"""Mis dedicatorias: qué ve el comprador en su panel posventa y en qué estado.

No hay autoguardado en el editor. Quien pagó y cerró la pestaña no tiene fila de carta:
su "borrador" es la compra pagada sin carta, y la retoma desde el editor con
``POST /api/v1/letters`` y el ``purchaseId``. El estado se deriva en cada lectura y no se
guarda: ninguna columna nueva, ninguna migración.

- ``draft``: compra pagada sin carta, o carta todavía en borrador.
- ``published``: la carta ya se publicó. Se conserva aunque la compra se anule después
  por un reembolso: el enlace que recibió el destinatario sigue vivo.

Lo que no está pagado no es posventa y no aparece, ni siquiera como borrador.
"""

from app.models.commerce import Letter, Purchase

DEDICATION_STATES = ("draft", "published")


def state_of(purchase: Purchase, letter: Letter | None) -> str | None:
    """Estado del panel para una compra y su carta, o ``None`` si no debe mostrarse.

    La consulta del repositorio ya filtra lo mismo; la regla vive aquí para que se
    pueda probar sin base de datos y para que ninguna otra consulta la reinterprete.
    """
    if letter is not None and letter.status == "published":
        return "published"
    if purchase.status == "paid":
        return "draft"
    return None
