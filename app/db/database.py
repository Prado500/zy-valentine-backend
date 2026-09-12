import ssl

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.core.config import MigrationSettings, Settings


def create_engine(
    settings: Settings | MigrationSettings,
    *,
    pool_size: int | None = None,
    max_overflow: int | None = None,
    pool_timeout: int | None = None,
) -> AsyncEngine:
    """Motor asíncrono. Los tamaños se pueden forzar para procesos que no son la API.

    El worker corre en el mismo servidor y comparte el presupuesto de 20 conexiones de
    la B1ms, así que necesita su propio pool acotado en vez del de la API.
    """
    tls = (
        ssl.create_default_context(cafile=settings.db_ssl_ca_file)
        if settings.db_ssl_mode == "verify-full"
        else False
    )
    return create_async_engine(
        settings.database_url.get_secret_value(),
        echo=False,
        hide_parameters=True,
        pool_size=settings.db_pool_size if pool_size is None else pool_size,
        max_overflow=settings.db_max_overflow if max_overflow is None else max_overflow,
        pool_timeout=settings.db_pool_timeout if pool_timeout is None else pool_timeout,
        pool_pre_ping=True,
        connect_args={"ssl": tls, "timeout": 10, "command_timeout": 10},
    )


def session_factory(engine: AsyncEngine):
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


def create_worker_engine(settings: Settings) -> AsyncEngine:
    """Pool dedicado del worker: nunca más de 5 conexiones.

    La instancia B1ms admite 20 conexiones simultáneas. El worker se queda con 5 como
    máximo y deja las 15 restantes a la API, que es quien atiende al comprador.
    """
    return create_engine(
        settings,
        pool_size=settings.worker_db_pool_size,
        max_overflow=0,
        pool_timeout=settings.worker_db_pool_timeout,
    )
