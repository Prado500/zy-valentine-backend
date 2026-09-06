"""Validación local con PostgreSQL efímero propio. Nunca lee .env ni una base remota.

Dos motores intercambiables:

- ``--docker`` (o automático si no hay binarios de PostgreSQL): contenedor
  ``postgres:15-alpine`` publicado solo en loopback y eliminado al terminar.
- binarios locales: ``initdb``/``pg_ctl``/``createdb`` en LOCAL_POSTGRES_BIN
  (Windows usa los ejecutables ``.exe``).

Ejecuta upgrade/check/downgrade/upgrade, pytest y, con ``--postman``, Newman.
"""

import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_NAME = "zy_auth_validation"
COMMANDS = (
    ["alembic", "upgrade", "head"],
    ["alembic", "check"],
    ["alembic", "downgrade", "base"],
    ["alembic", "upgrade", "head"],
    ["pytest", "-q", "-p", "no:cacheprovider"],
)


def run(args, env, **kwargs):
    subprocess.run([str(arg) for arg in args], cwd=ROOT, env=env, check=True, **kwargs)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_postgres(port: int, password: str, attempts: int = 60) -> bool:
    probe = (
        "import asyncio,sys,asyncpg\n"
        "async def main():\n"
        f"    conn = await asyncpg.connect(host='127.0.0.1', port={port}, user='postgres',"
        f" password={password!r}, database='{DB_NAME}')\n"
        "    await conn.close()\n"
        "asyncio.run(main())\n"
    )
    for _ in range(attempts):
        if subprocess.run([sys.executable, "-c", probe], capture_output=True).returncode == 0:
            return True
        time.sleep(0.5)
    return False


def base_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            (
                "DATABASE_",
                "DB_",
                "APP_",
                "SESSION_",
                "CORS_",
                "GOOGLE_",
                "WEB_CONCURRENCY",
                "STORAGE_",
                "AZURE_",
                "MAIL_",
                "MERCADOPAGO_",
                "PAYMENT_",
                "PII_",
            )
        )
    }
    env.update(
        APP_ENV="local",
        SESSION_SECRET=secrets.token_urlsafe(48),
        DB_SSL_MODE="disable",
        STORAGE_BACKEND="local",
        MAIL_BACKEND="console",
        PAYMENT_PROVIDER="none",
        ENV_FILE="",
    )
    return env


def suite(env: dict[str, str]) -> None:
    for command in COMMANDS:
        run([sys.executable, "-m", *command], env)
    if "--postman" in sys.argv:
        run([sys.executable, "scripts/run_postman.py"], env)


def with_docker() -> None:
    if not shutil.which("docker"):
        raise SystemExit("Docker no está disponible; instala PostgreSQL o el motor Docker")
    port, password = free_port(), secrets.token_urlsafe(24).replace("-", "x").replace("_", "y")
    name = f"zy-validation-{secrets.token_hex(4)}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-e",
            f"POSTGRES_DB={DB_NAME}",
            "-p",
            f"127.0.0.1:{port}:5432",
            "postgres:15-alpine",
            "-c",
            "max_connections=20",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    try:
        # pg_isready responde durante la fase de inicialización interna del contenedor;
        # se comprueba una conexión real por el puerto publicado en loopback.
        if not wait_for_postgres(port, password):
            raise SystemExit("El PostgreSQL efímero no quedó listo")
        env = base_env()
        env["DATABASE_URL"] = f"postgresql+asyncpg://postgres:{password}@127.0.0.1:{port}/{DB_NAME}"
        env["TEST_DATABASE_URL"] = env["DATABASE_URL"]
        suite(env)
    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, check=False)


def postgres_bin() -> Path | None:
    configured = os.environ.get("LOCAL_POSTGRES_BIN")
    suffix = ".exe" if os.name == "nt" else ""
    candidates = [Path(configured)] if configured else []
    if os.name == "nt":
        candidates.append(Path(r"C:\Program Files\PostgreSQL\18\bin"))
    else:
        located = shutil.which("initdb")
        if located:
            candidates.append(Path(located).parent)
    for candidate in candidates:
        if all(
            (candidate / f"{tool}{suffix}").is_file() for tool in ("initdb", "pg_ctl", "createdb")
        ):
            return candidate
    return None


def with_local_binaries(pg_bin: Path) -> None:
    suffix = ".exe" if os.name == "nt" else ""
    runtime = ROOT / ".local-validation"
    runtime.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="pg-", dir=runtime))
    password = secrets.token_urlsafe(32)
    pwfile = work / "password"
    pwfile.write_text(password, encoding="ascii")
    port = free_port()
    env = base_env()
    env.update(PGPASSWORD=password, PGHOST="127.0.0.1", PGPORT=str(port), PGUSER="postgres")
    data = work / "data"
    run(
        [
            pg_bin / f"initdb{suffix}",
            "-D",
            data,
            "-U",
            "postgres",
            "-A",
            "scram-sha-256",
            "--pwfile",
            pwfile,
            "--encoding=UTF8",
            "--locale=C",
        ],
        env,
        stdout=subprocess.DEVNULL,
    )
    pwfile.unlink()
    started = False
    try:
        run(
            [
                pg_bin / f"pg_ctl{suffix}",
                "-D",
                data,
                "-l",
                work / "server.log",
                "-o",
                f"-h 127.0.0.1 -p {port} -c max_connections=20",
                "-w",
                "start",
            ],
            env,
        )
        started = True
        run([pg_bin / f"createdb{suffix}", DB_NAME], env)
        env["DATABASE_URL"] = f"postgresql+asyncpg://postgres:{password}@127.0.0.1:{port}/{DB_NAME}"
        env["TEST_DATABASE_URL"] = env["DATABASE_URL"]
        suite(env)
    finally:
        if started:
            run([pg_bin / f"pg_ctl{suffix}", "-D", data, "-m", "fast", "-w", "stop"], env)
        # Se conservan los artefactos aislados para inspección; sin borrado recursivo.


def main() -> None:
    pg_bin = None if "--docker" in sys.argv else postgres_bin()
    if pg_bin:
        with_local_binaries(pg_bin)
    else:
        with_docker()


if __name__ == "__main__":
    main()
