"""Consejos para acompañar la carta: flores, algo dulce y un detalle, por estilo.

Texto de negocio, no de código: vive aquí para que cambiarlo sea editar una tabla y
nada más. Cada entrada sigue el carácter del tema (el dorado clásico pide rosas rojas;
el jardín esmeralda, eucalipto) y nombra productos fáciles de conseguir en Colombia.
Un tema desconocido usa los consejos de ``classic``, igual que la paleta.
"""

from dataclasses import dataclass

from app.services.themes import palette_for

DEFAULT_THEME = "classic"

# Mensaje general que abre la sección, igual para todos los estilos.
GENERAL_MESSAGE = (
    "Una carta se recuerda más cuando llega con algo en la mano. No hace falta gastar "
    "mucho: un ramo pequeño, algo dulce y la tarjeta con el código bastan para que el "
    "momento se sienta preparado."
)

# Cómo entregarla. Va después de los consejos del tema.
HOW_TO_DELIVER = (
    "Imprime la tarjeta adjunta, recórtala por el borde y ponla dentro del ramo o de la "
    "caja: el código se escanea con la cámara del celular, sin ninguna aplicación.",
    "El enlace no caduca. Si prefieres enviarlo por mensaje, comparte la dirección de la "
    "carta o reenvía este correo.",
)


@dataclass(frozen=True, slots=True)
class GiftTips:
    flowers: str
    sweets: str
    touch: str


TIPS: dict[str, GiftTips] = {
    "classic": GiftTips(
        flowers="Rosas rojas o un ramo clásico de tallo largo.",
        sweets="Trufas o bombones de chocolate oscuro.",
        touch="Una vela y una nota escrita a mano.",
    ),
    "pastel-pink": GiftTips(
        flowers="Peonías, rosas rosadas o tulipanes.",
        sweets="Macarons o fresas con chocolate.",
        touch="Un peluche pequeño o una cinta de raso en la caja.",
    ),
    "starry": GiftTips(
        flowers="Flores blancas: lisianthus, astromelias o baby's breath.",
        sweets="Chocolate con caramelo salado.",
        touch="Luces cálidas para leerla de noche.",
    ),
    "sunset": GiftTips(
        flowers="Girasoles o rosas naranjas.",
        sweets="Brownies o alfajores.",
        touch="Un plan al aire libre al atardecer.",
    ),
    "lavender": GiftTips(
        flowers="Lavanda seca, lilas o tulipanes morados.",
        sweets="Galletas de vainilla o chocolate blanco.",
        touch="Una vela o sales de baño de lavanda.",
    ),
    "emerald": GiftTips(
        flowers="Eucalipto con rosas blancas o un ramo de hojas verdes.",
        sweets="Chocolate con menta, o té con galletas.",
        touch="Una planta pequeña que dure.",
    ),
    "midnight": GiftTips(
        flowers="Flores azules: hortensias o delphinium.",
        sweets="Chocolate al 70 % o café de especialidad.",
        touch="Una canción compartida y una cena tranquila.",
    ),
    "vintage": GiftTips(
        flowers="Rosas té, flores secas o baby's breath.",
        sweets="Dulces tradicionales: cocadas u obleas.",
        touch="Papel kraft, lacre o un marco antiguo.",
    ),
}


def tips_for(theme: str | None) -> GiftTips:
    return TIPS.get(palette_for(theme).slug, TIPS[DEFAULT_THEME])
