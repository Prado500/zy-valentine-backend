"""Own ephemeral PostgreSQL only. Never reads .env or uses a remote database."""

import os
import secrets
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(args, env, **kwargs):
    subprocess.run([str(arg) for arg in args], cwd=ROOT, env=env, check=True, **kwargs)


def main():
    pg_bin = Path(os.environ.get("LOCAL_POSTGRES_BIN", r"C:\Program Files\PostgreSQL\18\bin"))
    for executable in ("initdb.exe", "pg_ctl.exe", "createdb.exe"):
        if not (pg_bin / executable).is_file():
            raise SystemExit("Set LOCAL_POSTGRES_BIN to a local PostgreSQL bin directory")
    runtime = ROOT / ".local-validation"
    runtime.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="pg-", dir=runtime))
    password = secrets.token_urlsafe(32)
    pwfile = work / "password"
    pwfile.write_text(password, encoding="ascii")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            ("DATABASE_", "DB_", "APP_", "SESSION_", "CORS_", "GOOGLE_", "WEB_CONCURRENCY")
        )
    }
    env.update(
        APP_ENV="local",
        SESSION_SECRET=secrets.token_urlsafe(48),
        DB_SSL_MODE="disable",
        PGPASSWORD=password,
        PGHOST="127.0.0.1",
        PGPORT=str(port),
        PGUSER="postgres",
    )
    data = work / "data"
    run(
        [
            pg_bin / "initdb.exe",
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
                pg_bin / "pg_ctl.exe",
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
        run([pg_bin / "createdb.exe", "zy_auth_validation"], env)
        env["DATABASE_URL"] = (
            f"postgresql+asyncpg://postgres:{password}@127.0.0.1:{port}/zy_auth_validation"
        )
        env["TEST_DATABASE_URL"] = env["DATABASE_URL"]
        env["ENV_FILE"] = ""  # Settings in tests explicitly avoids unrelated .env.
        for command in (
            ["alembic", "upgrade", "head"],
            ["alembic", "check"],
            ["alembic", "downgrade", "base"],
            ["alembic", "upgrade", "head"],
            ["pytest", "-q", "-p", "no:cacheprovider"],
        ):
            run([sys.executable, "-m", *command], env)
        if "--postman" in sys.argv:
            run([sys.executable, "scripts/run_postman.py"], env)
    finally:
        if started:
            run([pg_bin / "pg_ctl.exe", "-D", data, "-m", "fast", "-w", "stop"], env)
        # Keep isolated artifacts for inspection. No recursive deletion.


if __name__ == "__main__":
    main()
