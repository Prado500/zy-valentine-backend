from anyio import to_thread
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import auth_limit, csrf_guard, current_user, get_db
from app.core.errors import ApiError
from app.core.security import digest, issue_csrf
from app.models.user import AuthSession, User
from app.schemas.auth import (
    CsrfResponse,
    GoogleLogin,
    Login,
    MessageResponse,
    Register,
    UserResponse,
)
from app.services import auth

router = APIRouter(prefix="/api/v1", tags=["Authentication"])


def set_cookie(response, settings, name, value, max_age):
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        secure=settings.secure_cookies,
        samesite=settings.cookie_samesite,
        path="/",
    )


@router.get("/auth/csrf", response_model=CsrfResponse)
async def csrf(request: Request, response: Response):
    settings = request.app.state.settings
    token = issue_csrf(settings.session_secret.get_secret_value())
    set_cookie(response, settings, settings.csrf_cookie, token, settings.csrf_seconds)
    return {"csrfToken": token}


@router.post(
    "/auth/register",
    response_model=UserResponse,
    status_code=201,
    dependencies=[Depends(csrf_guard), Depends(auth_limit)],
)
async def register(payload: Register, request: Request, db: AsyncSession = Depends(get_db)):
    return await auth.register(db, payload, request.app.state.hash_limiter)


async def finish_login(user, request, response, db):
    settings = request.app.state.settings
    token = await auth.new_session(
        db, user, request.cookies.get(settings.session_cookie), settings.session_minutes
    )
    set_cookie(response, settings, settings.session_cookie, token, settings.session_minutes * 60)
    return user


@router.post(
    "/auth/login",
    response_model=UserResponse,
    dependencies=[Depends(csrf_guard), Depends(auth_limit)],
)
async def login(
    payload: Login, request: Request, response: Response, db: AsyncSession = Depends(get_db)
):
    user = await auth.login(db, payload, request.app.state.hash_limiter)
    return await finish_login(user, request, response, db)


@router.post("/auth/google", response_model=UserResponse, dependencies=[Depends(auth_limit)])
async def google(
    payload: GoogleLogin,
    request: Request,
    response: Response,
    nonce: str = Depends(csrf_guard),
    db: AsyncSession = Depends(get_db),
):
    settings = request.app.state.settings
    if not settings.google_client_id:
        raise ApiError(503, "GOOGLE_NOT_CONFIGURED", "Google todavía no está configurado.")
    claims = await to_thread.run_sync(
        request.app.state.google_verifier.verify,
        payload.credential,
        settings.google_client_id,
        nonce,
        limiter=request.app.state.google_limiter,
    )
    user = await auth.google_user(db, claims)
    return await finish_login(user, request, response, db)


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(current_user)):
    return user


@router.post("/auth/logout", response_model=MessageResponse, dependencies=[Depends(csrf_guard)])
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    settings = request.app.state.settings
    token = request.cookies.get(settings.session_cookie)
    if token:
        await db.execute(delete(AuthSession).where(AuthSession.token_hash == digest(token)))
        await db.commit()
    response.delete_cookie(
        settings.session_cookie,
        path="/",
        secure=settings.secure_cookies,
        httponly=True,
        samesite=settings.cookie_samesite,
    )
    return {"message": "Sesión cerrada."}
