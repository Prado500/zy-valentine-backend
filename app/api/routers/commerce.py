"""HTTP del dominio comercial: identidad privada, compras, cartas, fotos, QR y correo.

Esta capa **solo** traduce HTTP. Recibe la petición, se la pasa a
``CommerceService`` y devuelve el esquema Pydantic que este arma. No conoce la
sesión de base de datos, no llama a repositorios y no decide reglas de negocio: si
un endpoint necesita saber algo de la base, lo pide al servicio.

Lo único que se queda aquí es lo que es HTTP de verdad: el código de estado, las
cabeceras, el tipo de contenido de las respuestas binarias y las dependencias de
sesión y CSRF.
"""

import unicodedata
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile

from app.api.dependencies import commerce_service, csrf_guard, current_user
from app.models.user import User
from app.schemas.commerce import (
    CommerceHealth,
    DedicationResponse,
    DeliveryResponse,
    EagerPhotoResponse,
    IdentityDocumentInput,
    IdentityDocumentResponse,
    LetterCreate,
    LetterQueued,
    LetterResponse,
    LetterUpdate,
    PaymentVerifyInput,
    PhotoResponse,
    PublicLetterResponse,
    PurchaseCreate,
    PurchaseResponse,
    PurchaseVerification,
    ResendInput,
)
from app.services.commerce import BinaryContent, CommerceService

router = APIRouter(prefix="/api/v1", tags=["Commerce"])
public_router = APIRouter(prefix="/api/v1/public", tags=["Public"])
webhook_router = APIRouter(prefix="/api/v1/webhooks", tags=["Webhooks"])


def content_disposition(filename: str) -> str:
    """``attachment`` con el nombre en ASCII y, aparte, en UTF-8 (RFC 6266 / 5987).

    Las cabeceras viajan en Latin-1: un título con caracteres fuera de ese rango
    rompería la respuesta entera. El nombre ya pasó por ``safe_file_name`` (sin comillas
    ni saltos); aquí solo se reparte entre ``filename`` y ``filename*``.
    """
    ascii_name = (
        unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode() or "archivo"
    )
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def binary(content: BinaryContent) -> Response:
    """Respuesta de bytes. Es lo único que no puede ser un esquema Pydantic."""
    headers = {}
    if content.filename:
        headers["Content-Disposition"] = content_disposition(content.filename)
    if content.cache_control:
        headers["Cache-Control"] = content.cache_control
    return Response(content=content.content, media_type=content.media_type, headers=headers)


# --- Identidad privada ---------------------------------------------------------------


@router.put(
    "/me/identity-document",
    response_model=IdentityDocumentResponse,
    dependencies=[Depends(csrf_guard)],
)
async def set_identity_document(
    payload: IdentityDocumentInput,
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    return await service.set_identity_document(user, payload)


@router.get("/me/identity-document", response_model=IdentityDocumentResponse)
async def get_identity_document(
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    return await service.identity_document(user)


# --- Compras y pagos -----------------------------------------------------------------


@router.post("/purchases", response_model=PurchaseResponse, dependencies=[Depends(csrf_guard)])
async def create_purchase(
    payload: PurchaseCreate,
    response: Response,
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    """201 con una compra nueva; 200 si la clave de idempotencia ya tenía la suya."""
    outcome = await service.create_purchase(user, payload)
    response.status_code = outcome.status
    return outcome.body


@router.get("/purchases", response_model=list[PurchaseResponse])
async def list_purchases(
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    return await service.list_purchases(user)


@router.get("/purchases/{purchase_id}", response_model=PurchaseResponse)
async def get_purchase(
    purchase_id: uuid.UUID,
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    return await service.get_purchase(user, purchase_id)


@router.post(
    "/purchases/{purchase_id}/verify",
    response_model=PurchaseVerification,
    dependencies=[Depends(csrf_guard)],
)
async def verify_purchase(
    purchase_id: uuid.UUID,
    payload: PaymentVerifyInput,
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    """IOP #3: el navegador solo aporta el identificador; el estado lo confirma el servidor."""
    return await service.verify_purchase(user, purchase_id, payload)


@webhook_router.post("/mercadopago")
async def mercadopago_webhook(
    request: Request, service: CommerceService = Depends(commerce_service)
):
    """Sin CSRF (no viene de un navegador) pero con firma HMAC obligatoria."""
    result = await service.handle_webhook(
        await request.body(),
        {name.lower(): value for name, value in request.headers.items()},
        request.query_params.get("data.id"),
    )
    return {"result": result}


# --- Cartas --------------------------------------------------------------------------


@router.post(
    "/letters",
    response_model=LetterResponse | LetterQueued,
    dependencies=[Depends(csrf_guard)],
)
async def create_letter(
    payload: LetterCreate,
    response: Response,
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    """IOP #4 a #6: recibe la carta, corta el fraude y delega la escritura.

    Con Azure Service Bus configurado el INSERT sale del camino de la petición: se
    valida la compra en caliente (IOP #5: existe, es de esta cuenta, está pagada y no
    tiene carta publicada), se publica el mensaje con las fotos temporales y se responde
    **202** sin escribir, para no atar la latencia del comprador a los 240 IOPS del
    disco. Un segundo intento sobre una carta ya **publicada** responde **409
    LETTER_ALREADY_EXISTS**: quien retrocede en el navegador no consigue una segunda
    carta. Sobre un borrador la orden sí entra: es el retome desde "Mis dedicatorias" y
    el worker sobrescribe la carta con lo enviado.

    Sin cola —o si la cola falla— se conserva el comportamiento síncrono de siempre:
    201 con la carta creada y 200 con la existente (un borrador se sobrescribe con lo
    enviado y se publica; una publicada se devuelve tal cual), que es lo que espera el
    frontend desplegado hoy.
    """
    outcome = await service.create_letter(user, payload)
    response.status_code = outcome.status
    return outcome.body


@router.get("/letters", response_model=list[LetterResponse])
async def my_letters(
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    """ "Mis cartas": estado de pago, entrega y borradores pendientes del comprador."""
    return await service.list_letters(user)


@router.get("/letters/{letter_id}", response_model=LetterResponse)
async def get_letter(
    letter_id: uuid.UUID,
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    return await service.get_letter(user, letter_id)


@router.patch(
    "/letters/{letter_id}", response_model=LetterResponse, dependencies=[Depends(csrf_guard)]
)
async def update_letter(
    letter_id: uuid.UUID,
    payload: LetterUpdate,
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    return await service.update_letter(user, letter_id, payload)


# --- Mis dedicatorias ------------------------------------------------------------------


@router.get("/me/dedications", response_model=list[DedicationResponse])
async def my_dedications(
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    """Panel posventa: cartas publicadas y borradores por retomar, en una sola consulta.

    Un borrador es una compra pagada sin carta (el editor no autoguarda) o una carta
    que quedó sin publicar. Para retomarlo el frontend abre el editor con el
    ``purchaseId`` y envía ``POST /letters`` como siempre: si había borrador, se
    sobrescribe con lo nuevo y se publica.
    """
    return await service.list_dedications(user)


@router.post(
    "/letters/{letter_id}/publish",
    response_model=LetterResponse,
    dependencies=[Depends(csrf_guard)],
)
async def publish_letter(
    letter_id: uuid.UUID,
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    """IOP #7: publica, genera enlace/QR y envía el correo con estado persistido."""
    return await service.publish_letter(user, letter_id)


@router.post(
    "/letters/{letter_id}/deliveries",
    response_model=DeliveryResponse,
    status_code=202,
    dependencies=[Depends(csrf_guard)],
)
async def resend_letter(
    letter_id: uuid.UUID,
    payload: ResendInput,
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    """Reenviar no consume otra compra ni crea otra carta: solo otro intento de envío."""
    return await service.resend_letter(user, letter_id, payload)


# --- Fotos ---------------------------------------------------------------------------


@router.post(
    "/letters/photos/eager",
    response_model=EagerPhotoResponse,
    status_code=201,
    dependencies=[Depends(csrf_guard)],
)
async def eager_photo(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    """Sube una foto en caliente, antes de que exista la carta (eager upload).

    Va al contenedor efímero (``AZURE_TEMPORAL_CONTAINER_NAME``, o una carpeta local en
    desarrollo) y no toca PostgreSQL: mientras el comprador elige fotos no se gasta ni
    una escritura. El ``tempId`` devuelto es lo que se adjunta luego en ``temp_photos``
    al crear la carta; el worker lo traslada al contenedor permanente.

    La ruta va antes de ``/letters/{letter_id}/photos`` a propósito: si se declarara
    después, ``photos`` se interpretaría como un ``letter_id`` y nunca se alcanzaría.

    Se lee un byte más del máximo permitido para poder distinguir "justo en el límite"
    de "se pasó", sin cargar en memoria un archivo entero que se va a rechazar.
    """
    limit = request.app.state.settings.max_photo_bytes
    return await service.eager_photo(
        user, await file.read(limit + 1), file.content_type, file.filename
    )


@router.post(
    "/letters/{letter_id}/photos",
    response_model=PhotoResponse,
    status_code=201,
    dependencies=[Depends(csrf_guard)],
)
async def upload_photo(
    letter_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    caption: str | None = Form(default=None, max_length=200),
    position: int | None = Form(default=None, ge=0, le=99),
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    limit = request.app.state.settings.max_photo_bytes
    return await service.add_photo(
        user, letter_id, await file.read(limit + 1), file.content_type, caption, position
    )


@router.get("/letters/{letter_id}/photos/{photo_id}/content")
async def photo_content(
    letter_id: uuid.UUID,
    photo_id: uuid.UUID,
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    return binary(await service.photo_content(user, letter_id, photo_id))


@router.delete("/letters/{letter_id}/photos/{photo_id}", dependencies=[Depends(csrf_guard)])
async def delete_photo(
    letter_id: uuid.UUID,
    photo_id: uuid.UUID,
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    await service.delete_photo(user, letter_id, photo_id)
    return {"message": "Foto eliminada."}


@router.get("/letters/{letter_id}/qr.png")
async def letter_qr(
    letter_id: uuid.UUID,
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    return binary(await service.letter_qr(user, letter_id))


@router.get("/letters/{letter_id}/card.pdf")
async def letter_card(
    letter_id: uuid.UUID,
    user: User = Depends(current_user),
    service: CommerceService = Depends(commerce_service),
):
    """Tarjeta QR imprimible (PDF), la misma que viaja adjunta en el correo."""
    return binary(await service.letter_card(user, letter_id))


# --- Visor público (sin sesión, sin datos personales del comprador) ------------------


@public_router.get("/letters/{slug}", response_model=PublicLetterResponse)
async def public_letter(slug: str, service: CommerceService = Depends(commerce_service)):
    return await service.public_letter(slug)


@public_router.get("/letters/{slug}/photos/{position}")
async def public_photo(
    slug: str, position: int, service: CommerceService = Depends(commerce_service)
):
    return binary(await service.public_photo(slug, position))


@public_router.get("/letters/{slug}/qr.png")
async def public_qr(slug: str, service: CommerceService = Depends(commerce_service)):
    return binary(await service.public_qr(slug))


@public_router.get("/letters/{slug}/card.pdf")
async def public_card(slug: str, service: CommerceService = Depends(commerce_service)):
    return binary(await service.public_card(slug))


@router.get("/health/commerce", response_model=CommerceHealth, include_in_schema=False)
async def commerce_health(service: CommerceService = Depends(commerce_service)):
    """Diagnóstico sin secretos: qué integraciones están configuradas."""
    return service.health()
