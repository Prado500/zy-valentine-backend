import ssl

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    tls = (
        ssl.create_default_context(cafile=settings.db_ssl_ca_file)
        if settings.db_ssl_mode == "verify-full"
        else False
    )
    return create_async_engine(
        settings.database_url.get_secret_value(),
        echo=False,
        hide_parameters=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_pre_ping=True,
        connect_args={"ssl": tls, "timeout": 10, "command_timeout": 10},
    )


def session_factory(engine: AsyncEngine):
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
