"""HTTP de autenticación: CSRF, registro, sesión y cierre de sesión.

Igual que el router comercial, aquí no entra ni la sesión de base de datos ni los
repositorios: eso lo lleva ``AccountService``. Lo que sí se queda es la gestión de
cookies, que es protocolo puro —``HttpOnly``, ``Secure``, ``SameSite`` y el prefijo
``__Host-``— y no tiene sentido fuera de HTTP.
"""

from fastapi import APIRouter, Depends, Request, Response

from app.api.dependencies import account_service, auth_limit, csrf_guard, current_user
from app.core.security import issue_csrf
from app.models.user import User
from app.schemas.auth import (
    CsrfResponse,
    GoogleLogin,
    Login,
    MessageResponse,
    Register,
    UserResponse,
)
from app.services.accounts import AccountService

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


async def finish_login(user: User, request: Request, response: Response, service: AccountService):
    """Abre la sesión y deja su token en la cookie. El token nunca viaja en el cuerpo."""
    settings = request.app.state.settings
    token = await service.open_session(user, request.cookies.get(settings.session_cookie))
    set_cookie(response, settings, settings.session_cookie, token, settings.session_minutes * 60)
    return user


@router.get("/auth/csrf", response_model=CsrfResponse)
async def csrf(request: Request, response: Response):
    settings = request.app.state.settings
    token = issue_csrf(settings.session_secret.get_secret_value())
    set_cookie(response, settings, settings.csrf_cookie, token, settings.csrf_seconds)
    return {"csrfToken": token}


def audit_trail(request: Request) -> tuple[str | None, str | None]:
    """IP y agente para la prueba de autorización (Decreto 1377 de 2013, art. 7).

    **Limitación conocida:** uvicorn corre con ``--no-proxy-headers``, así que detrás de
    Azure esta IP es la del ingress, no la del titular. Arreglarlo toca el rate limiter
    y va en un PR aparte; mientras tanto se guarda lo que hay, que es mejor que nada.
    """
    client = request.client.host if request.client else None
    return client, request.headers.get("user-agent")


@router.post(
    "/auth/register",
    response_model=UserResponse,
    status_code=201,
    dependencies=[Depends(csrf_guard), Depends(auth_limit)],
)
async def register(
    payload: Register,
    request: Request,
    service: AccountService = Depends(account_service),
):
    ip, user_agent = audit_trail(request)
    return await service.register(payload, ip, user_agent)


@router.post(
    "/auth/login",
    response_model=UserResponse,
    dependencies=[Depends(csrf_guard), Depends(auth_limit)],
)
async def login(
    payload: Login,
    request: Request,
    response: Response,
    service: AccountService = Depends(account_service),
):
    return await finish_login(await service.login(payload), request, response, service)


@router.post("/auth/google", response_model=UserResponse, dependencies=[Depends(auth_limit)])
async def google(
    payload: GoogleLogin,
    request: Request,
    response: Response,
    nonce: str = Depends(csrf_guard),
    service: AccountService = Depends(account_service),
):
    user = await service.google_login(payload, nonce)
    return await finish_login(user, request, response, service)


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(current_user)):
    return user


@router.post("/auth/logout", response_model=MessageResponse, dependencies=[Depends(csrf_guard)])
async def logout(
    request: Request,
    response: Response,
    service: AccountService = Depends(account_service),
):
    settings = request.app.state.settings
    await service.close_session(request.cookies.get(settings.session_cookie))
    response.delete_cookie(
        settings.session_cookie,
        path="/",
        secure=settings.secure_cookies,
        httponly=True,
        samesite=settings.cookie_samesite,
    )
    return {"message": "Sesión cerrada."}
