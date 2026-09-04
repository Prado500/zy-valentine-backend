import os
import sys
import asyncio
from logging.config import fileConfig
from dotenv import load_dotenv

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# 1. Inyectar la raíz del proyecto al PATH para que reconozca el paquete "app"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# 2. Cargar las credenciales de la base de datos desde .env
load_dotenv()

# 3. Importar Base y los Modelos (Crucial para que autogenerate detecte las tablas)
from app.models.base import Base
from app.models.reserva import ReservaMaquinaria

# Este es el objeto de configuración de Alembic (lee el alembic.ini)
config = context.config

# 4. Sobrescribir la URL quemada en alembic.ini con la de nuestro .env
database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise ValueError("¡ALERTA! No se encontró DATABASE_URL en el archivo .env")

config.set_main_option("sqlalchemy.url", database_url)

# Configurar logs
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 5. Apuntar a los metadatos de nuestros modelos
target_metadata = Base.metadata

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()

def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()

async def run_async_migrations() -> None:
    """Run migrations in 'online' mode."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()

def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
