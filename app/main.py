import uuid
from contextlib import asynccontextmanager

from anyio import CapacityLimiter
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from app.api.routers.auth import router
from app.api.routers.commerce import public_router, webhook_router
from app.api.routers.commerce import router as commerce_router
from app.core.config import Settings, get_settings
from app.core.security import RateLimiter
from app.db.database import create_engine, session_factory
from app.services.google import GoogleVerifier
from app.services.mailer import build_mailer
from app.services.payments import build_gateway
from app.services.service_bus import build_publisher
from app.services.storage import build_storage

EXPECTED_REVISION = "0003_dian_and_consent"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.engine = create_engine(settings)
        app.state.sessions = session_factory(app.state.engine)
        app.state.hash_limiter = CapacityLimiter(2)
        app.state.google_limiter = CapacityLimiter(1)
        app.state.google_verifier = GoogleVerifier()
        app.state.payments = build_gateway(settings)
        app.state.storage = build_storage(settings)
        app.state.mailer = build_mailer(settings)
        # Publicador de cartas. Sin cola configurada devuelve un MockPublisher y la
        # API sigue escribiendo de forma síncrona: el arranque nunca depende de él.
        app.state.letter_queue = build_publisher(settings)
        try:
            yield
        finally:
            app.state.google_verifier.close()
            await app.state.letter_queue.aclose()
            await app.state.payments.aclose()
            await app.state.engine.dispose()

    app = FastAPI(title="Zyvencore Valentine API", version="0.3.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.rate_limiter = RateLimiter(settings.auth_rate_limit, settings.auth_rate_window)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        # `no-store` por defecto: casi todo lleva sesión o datos personales. Las pocas
        # rutas de contenido público y congelado (QR y tarjeta de una carta publicada)
        # fijan su propia cabecera y aquí se respeta.
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def error_response(request, status, code, message, fields=None):
        return JSONResponse(
            status_code=status,
            content={
                "code": code,
                "message": message,
                "fieldErrors": fields or [],
                "requestId": getattr(request.state, "request_id", None),
            },
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        return error_response(
            request,
            exc.status_code,
            detail.get("code", "HTTP_ERROR"),
            detail.get("message", "Solicitud no disponible."),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo input/password/credential through Pydantic's full errors.
        fields = [{"field": ".".join(map(str, e["loc"])), "type": e["type"]} for e in exc.errors()]
        return error_response(
            request, 422, "VALIDATION_ERROR", "Revisa los campos enviados.", fields
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return error_response(
            request, 503, "DATABASE_UNAVAILABLE", "Servicio temporalmente no disponible."
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return error_response(request, 500, "INTERNAL_ERROR", "No se pudo completar la solicitud.")

    @app.get("/")
    @app.get("/health/live")
    async def live():
        return {"status": "ok", "service": "zyvalentine"}

    @app.get("/health/ready")
    async def ready():
        try:
            async with app.state.engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != EXPECTED_REVISION:
                raise RuntimeError("Migration pending")
        except Exception:
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return {"status": "ready"}

    app.include_router(router)
    app.include_router(commerce_router)
    app.include_router(public_router)
    app.include_router(webhook_router)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )
    return app


app = create_app()
