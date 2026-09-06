"""HTTP del dominio comercial: identidad privada, compras, cartas, fotos, QR y correo."""

import uuid

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import csrf_guard, current_user, get_db
from app.core.errors import ApiError
from app.models.commerce import Letter
from app.models.user import User
from app.repositories import commerce as repo
from app.schemas.commerce import (
    DeliveryResponse,
    IdentityDocumentInput,
    IdentityDocumentResponse,
    LetterCreate,
    LetterResponse,
    LetterUpdate,
    PaymentResponse,
    PaymentVerifyInput,
    PhotoResponse,
    PublicLetterResponse,
    PublicPhoto,
    PurchaseCreate,
    PurchaseResponse,
    PurchaseVerification,
    ResendInput,
)
from app.services import deliveries, identity, letters, purchases, webhooks

router = APIRouter(prefix="/api/v1", tags=["Commerce"])
public_router = APIRouter(prefix="/api/v1/public", tags=["Public"])
webhook_router = APIRouter(prefix="/api/v1/webhooks", tags=["Webhooks"])


def settings_of(request: Request):
    return request.app.state.settings


def purchase_payload(purchase, has_letter: bool) -> PurchaseResponse:
    return PurchaseResponse(
        id=purchase.id,
        status=purchase.status,
        amountCents=purchase.amount_cents,
        currency=purchase.currency,
        externalReference=purchase.external_reference,
        checkoutUrl=purchase.checkout_url,
        hasLetter=has_letter,
        paidAt=purchase.paid_at,
        expiresAt=purchase.expires_at,
        createdAt=purchase.created_at,
    )


async def letter_payload(db: AsyncSession, settings, letter: Letter) -> LetterResponse:
    photos = await repo.photos_of_letter(db, letter.id)
    sent = await repo.deliveries_of_letter(db, letter.id)
    url = letters.public_url(settings, letter)
    return LetterResponse(
        id=letter.id,
        purchaseId=letter.purchase_id,
        status=letter.status,
        title=letter.title,
        recipientName=letter.recipient_name,
        recipientEmail=letter.recipient_email,
        body=letter.body,
        theme=letter.theme,
        publicSlug=letter.public_slug if letter.status == "published" else None,
        publicUrl=url,
        qrUrl=f"{settings.public_base_url}/api/v1/letters/{letter.id}/qr.png" if url else None,
        publishedVersion=letter.published_version,
        publishedAt=letter.published_at,
        frozen=letters.is_frozen(letter, settings),
        photos=[
            PhotoResponse(
                id=photo.id,
                position=photo.position,
                caption=photo.caption,
                contentType=photo.content_type,
                byteSize=photo.byte_size,
                url=f"/api/v1/letters/{letter.id}/photos/{photo.id}/content",
            )
            for photo in photos
        ],
        deliveries=[
            DeliveryResponse(
                id=item.id,
                recipientEmail=item.recipient_email,
                status=item.status,
                attempts=item.attempts,
                letterVersion=item.letter_version,
                lastError=item.last_error,
                sentAt=item.sent_at,
                createdAt=item.created_at,
            )
            for item in sent
        ],
        createdAt=letter.created_at,
        updatedAt=letter.updated_at,
    )


async def owned_letter(db: AsyncSession, letter_id: uuid.UUID, user: User) -> Letter:
    letter = await repo.owned_letter(db, letter_id, user.id)
    if letter is None:
        raise ApiError(404, "LETTER_NOT_FOUND", "La carta no existe para esta cuenta.")
    return letter


# --- Identidad privada ---------------------------------------------------------------


@router.put(
    "/me/identity-document",
    response_model=IdentityDocumentResponse,
    dependencies=[Depends(csrf_guard)],
)
async def set_identity_document(
    payload: IdentityDocumentInput,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    record = await identity.set_document(db, settings_of(request), user, payload)
    return IdentityDocumentResponse(
        documentType=record.document_type,
        documentLast4=record.document_last4,
        createdAt=record.created_at,
    )


@router.get("/me/identity-document", response_model=IdentityDocumentResponse)
async def get_identity_document(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    record = await repo.identity_by_user(db, user.id)
    if record is None:
        raise ApiError(404, "DOCUMENT_NOT_FOUND", "No hay documento registrado.")
    return IdentityDocumentResponse(
        documentType=record.document_type,
        documentLast4=record.document_last4,
        createdAt=record.created_at,
    )


# --- Compras y pagos -----------------------------------------------------------------


@router.post("/purchases", response_model=PurchaseResponse, dependencies=[Depends(csrf_guard)])
async def create_purchase(
    payload: PurchaseCreate,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    purchase, created = await purchases.create_intent(
        db, settings_of(request), request.app.state.payments, user, payload
    )
    response.status_code = 201 if created else 200
    letter = await repo.letter_of_purchase(db, purchase.id)
    return purchase_payload(purchase, letter is not None)


@router.get("/purchases", response_model=list[PurchaseResponse])
async def list_purchases(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    items = await repo.purchases_of_user(db, user.id)
    with_letter = await repo.letters_with_purchase(db, [item.id for item in items])
    return [purchase_payload(item, item.id in with_letter) for item in items]


@router.get("/purchases/{purchase_id}", response_model=PurchaseResponse)
async def get_purchase(
    purchase_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    purchase = await repo.owned_purchase(db, purchase_id, user.id)
    if purchase is None:
        raise ApiError(404, "PURCHASE_NOT_FOUND", "La compra no existe para esta cuenta.")
    letter = await repo.letter_of_purchase(db, purchase.id)
    return purchase_payload(purchase, letter is not None)


@router.post(
    "/purchases/{purchase_id}/verify",
    response_model=PurchaseVerification,
    dependencies=[Depends(csrf_guard)],
)
async def verify_purchase(
    purchase_id: uuid.UUID,
    payload: PaymentVerifyInput,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """IOP #3: el navegador solo aporta el identificador; el estado lo confirma el servidor."""
    purchase = await repo.owned_purchase(db, purchase_id, user.id)
    if purchase is None:
        raise ApiError(404, "PURCHASE_NOT_FOUND", "La compra no existe para esta cuenta.")
    if payload.paymentId:
        await purchases.verify(db, request.app.state.payments, purchase, payload.paymentId)
    payment = await repo.payment_of_purchase(db, purchase.id)
    letter = await repo.letter_of_purchase(db, purchase.id)
    return PurchaseVerification(
        purchase=purchase_payload(purchase, letter is not None),
        payment=(
            PaymentResponse(
                status=payment.status,
                statusDetail=payment.status_detail,
                providerPaymentId=payment.provider_payment_id,
                verifiedAt=payment.verified_at,
            )
            if payment
            else None
        ),
        canCreateLetter=purchase.status == "paid" and letter is None,
    )


@webhook_router.post("/mercadopago")
async def mercadopago_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Sin CSRF (no viene de un navegador) pero con firma HMAC obligatoria."""
    body = await request.body()
    if len(body) > 64_000:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "Notificación demasiado grande.")
    gateway = request.app.state.payments
    gateway.verify_webhook(body, {k.lower(): v for k, v in request.headers.items()})
    try:
        payload = await request.json()
    except Exception:
        raise ApiError(422, "INVALID_WEBHOOK", "Cuerpo de notificación inválido.") from None
    if not isinstance(payload, dict):
        raise ApiError(422, "INVALID_WEBHOOK", "Cuerpo de notificación inválido.")
    result = await webhooks.handle(db, gateway, payload, request.query_params.get("data.id"))
    return {"result": result}


# --- Cartas --------------------------------------------------------------------------


@router.post("/letters", response_model=LetterResponse, dependencies=[Depends(csrf_guard)])
async def create_letter(
    payload: LetterCreate,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """IOP #4..#6. Doble clic o varias pestañas devuelven la misma carta con 200."""
    settings = settings_of(request)
    letter, created = await letters.create(db, settings, user, payload)
    response.status_code = 201 if created else 200
    return await letter_payload(db, settings, letter)


@router.get("/letters", response_model=list[LetterResponse])
async def my_letters(
    request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """ "Mis cartas": estado de pago, entrega y borradores pendientes del comprador."""
    settings = settings_of(request)
    return [
        await letter_payload(db, settings, letter)
        for letter in await repo.letters_of_user(db, user.id)
    ]


@router.get("/letters/{letter_id}", response_model=LetterResponse)
async def get_letter(
    letter_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    letter = await owned_letter(db, letter_id, user)
    return await letter_payload(db, settings_of(request), letter)


@router.patch(
    "/letters/{letter_id}", response_model=LetterResponse, dependencies=[Depends(csrf_guard)]
)
async def update_letter(
    letter_id: uuid.UUID,
    payload: LetterUpdate,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    settings = settings_of(request)
    letter = await owned_letter(db, letter_id, user)
    await letters.update(db, settings, letter, payload)
    return await letter_payload(db, settings, letter)


@router.post(
    "/letters/{letter_id}/publish",
    response_model=LetterResponse,
    dependencies=[Depends(csrf_guard)],
)
async def publish_letter(
    letter_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """IOP #7: publica, genera enlace/QR y envía el correo con estado persistido."""
    settings = settings_of(request)
    letter = await owned_letter(db, letter_id, user)
    await letters.publish(db, settings, letter)
    await deliveries.deliver(db, settings, request.app.state.mailer, letter)
    return await letter_payload(db, settings, letter)


@router.post(
    "/letters/{letter_id}/deliveries",
    response_model=DeliveryResponse,
    status_code=202,
    dependencies=[Depends(csrf_guard)],
)
async def resend_letter(
    letter_id: uuid.UUID,
    payload: ResendInput,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reenviar no consume otra compra ni crea otra carta: solo otro intento de envío."""
    settings = settings_of(request)
    letter = await owned_letter(db, letter_id, user)
    recipient = str(payload.recipientEmail) if payload.recipientEmail else None
    delivery = await deliveries.deliver(db, settings, request.app.state.mailer, letter, recipient)
    return DeliveryResponse(
        id=delivery.id,
        recipientEmail=delivery.recipient_email,
        status=delivery.status,
        attempts=delivery.attempts,
        letterVersion=delivery.letter_version,
        lastError=delivery.last_error,
        sentAt=delivery.sent_at,
        createdAt=delivery.created_at,
    )


# --- Fotos ---------------------------------------------------------------------------


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
    db: AsyncSession = Depends(get_db),
):
    settings = settings_of(request)
    letter = await owned_letter(db, letter_id, user)
    data = await file.read(settings.max_photo_bytes + 1)
    photo = await letters.add_photo(
        db,
        settings,
        request.app.state.storage,
        letter,
        data,
        file.content_type,
        caption,
        position,
    )
    return PhotoResponse(
        id=photo.id,
        position=photo.position,
        caption=photo.caption,
        contentType=photo.content_type,
        byteSize=photo.byte_size,
        url=f"/api/v1/letters/{letter.id}/photos/{photo.id}/content",
    )


@router.get("/letters/{letter_id}/photos/{photo_id}/content")
async def photo_content(
    letter_id: uuid.UUID,
    photo_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    letter = await owned_letter(db, letter_id, user)
    photo = await repo.photo_of_letter(db, letter.id, photo_id)
    if photo is None:
        raise ApiError(404, "PHOTO_NOT_FOUND", "La foto no existe.")
    data = await request.app.state.storage.get(photo.storage_key)
    return Response(content=data, media_type=photo.content_type)


@router.delete("/letters/{letter_id}/photos/{photo_id}", dependencies=[Depends(csrf_guard)])
async def delete_photo(
    letter_id: uuid.UUID,
    photo_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    settings = settings_of(request)
    letter = await owned_letter(db, letter_id, user)
    photo = await repo.photo_of_letter(db, letter.id, photo_id)
    if photo is None:
        raise ApiError(404, "PHOTO_NOT_FOUND", "La foto no existe.")
    await letters.remove_photo(db, settings, request.app.state.storage, letter, photo)
    return {"message": "Foto eliminada."}


@router.get("/letters/{letter_id}/qr.png")
async def letter_qr(
    letter_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services.qr import qr_png  # noqa: PLC0415 - importación local, uso puntual

    settings = settings_of(request)
    letter = await owned_letter(db, letter_id, user)
    url = letters.public_url(settings, letter)
    if not url:
        raise ApiError(409, "LETTER_NOT_PUBLISHED", "Publica la carta para obtener su QR.")
    return Response(content=qr_png(url), media_type="image/png")


# --- Visor público (sin sesión, sin datos personales del comprador) ------------------


async def published_letter(db: AsyncSession, slug: str) -> Letter:
    letter = await repo.letter_by_slug(db, slug)
    if letter is None or letter.status != "published":
        raise ApiError(404, "LETTER_NOT_FOUND", "La carta no está disponible.")
    return letter


@public_router.get("/letters/{slug}", response_model=PublicLetterResponse)
async def public_letter(slug: str, db: AsyncSession = Depends(get_db)):
    letter = await published_letter(db, slug)
    photos = await repo.photos_of_letter(db, letter.id)
    return PublicLetterResponse(
        letterId=letter.id,
        publishedVersion=letter.published_version,
        title=letter.title,
        recipientName=letter.recipient_name,
        body=letter.body,
        theme=letter.theme,
        photos=[
            PublicPhoto(
                position=photo.position,
                caption=photo.caption,
                url=f"/api/v1/public/letters/{slug}/photos/{photo.position}",
            )
            for photo in photos
        ],
        publishedAt=letter.published_at,
    )


@public_router.get("/letters/{slug}/photos/{position}")
async def public_photo(
    slug: str, position: int, request: Request, db: AsyncSession = Depends(get_db)
):
    letter = await published_letter(db, slug)
    photo = await repo.photo_at_position(db, letter.id, position)
    if photo is None:
        raise ApiError(404, "PHOTO_NOT_FOUND", "La foto no existe.")
    data = await request.app.state.storage.get(photo.storage_key)
    return Response(content=data, media_type=photo.content_type)


@public_router.get("/letters/{slug}/qr.png")
async def public_qr(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.qr import qr_png  # noqa: PLC0415 - importación local, uso puntual

    settings = settings_of(request)
    letter = await published_letter(db, slug)
    return Response(content=qr_png(letters.public_url(settings, letter)), media_type="image/png")


@router.get("/health/commerce", include_in_schema=False)
async def commerce_health(request: Request):
    """Diagnóstico sin secretos: qué integraciones están configuradas."""
    settings = settings_of(request)
    return JSONResponse(
        {
            "paymentProvider": settings.payment_provider,
            "storageBackend": settings.storage_backend,
            "mailBackend": settings.mail_backend,
            "freezeAfterPublish": settings.freeze_letter_after_publish,
        }
    )
