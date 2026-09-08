"""La suite provisiona su propia base de pruebas y nunca toca una real.

Contexto: en los builds 51 y 52 de CI el paso "Aplicar migraciones" del pipeline no
dejó tablas en la base desechable y las 112 pruebas de integración cayeron con
``relation "users" does not exist``. Estas pruebas cubren la pieza que evita repetirlo:
``tests/conftest.py`` aplica las migraciones por su cuenta y, si Alembic falla, lo
dice con su salida completa. Ninguna necesita PostgreSQL: la primera no llega a
ejecutar nada y la segunda usa un puerto cerrado para provocar el fallo.
"""

import pytest

from tests.conftest import NOT_DISPOSABLE, apply_migrations, is_disposable


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://postgres:local@127.0.0.1:5432/zy_auth_validation",
        "postgresql+asyncpg://postgres:local@localhost:5432/zy_test",
        "postgresql://postgres@[::1]:5432/integration_test",
    ],
)
def test_disposable_databases_are_recognised(url):
    assert is_disposable(url)


@pytest.mark.parametrize(
    "url",
    [
        # Una URL de Azure heredada del entorno: jamás debe migrarse ni truncarse.
        "postgresql+asyncpg://zv@zv-dev.postgres.database.azure.com:5432/zy_auth_validation",
        # Loopback, pero un nombre que no delata una base desechable.
        "postgresql+asyncpg://postgres:local@127.0.0.1:5432/zyvalentine",
        # Ni siquiera es PostgreSQL.
        "sqlite:///zy_test.db",
    ],
)
def test_migrations_refuse_anything_that_is_not_disposable(url):
    assert not is_disposable(url)
    with pytest.raises(RuntimeError, match="desechable en loopback"):
        apply_migrations(url)


def test_migration_failure_is_reported_with_alembic_output():
    """Puerto cerrado: Alembic no conecta y el error debe traer su salida, no un 'users'."""
    with pytest.raises(RuntimeError) as failure:
        apply_migrations("postgresql+asyncpg://postgres:local@127.0.0.1:1/zy_auth_validation")
    message = str(failure.value)
    assert "alembic upgrade head" in message
    assert "zy_auth_validation en loopback" in message
    # La salida real del subproceso viaja en el mensaje: es lo que faltó en CI.
    assert "Error" in message or "error" in message
    assert NOT_DISPOSABLE not in message
