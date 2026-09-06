"""Start an isolated local DB/API until stop_local.py is called. No remote resources."""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".local-validation"


def main():
    RUNTIME.mkdir(exist_ok=True)
    stop = RUNTIME / "local-server.stop"
    state = RUNTIME / "local-server.json"
    if state.exists():
        previous = json.loads(state.read_text())
        if previous.get("status") == "running":
            try:
                with urlopen(previous["baseUrl"] + "/health/live", timeout=2):
                    raise SystemExit("Local server already running: " + previous["baseUrl"])
            except OSError:
                pass
    stop.unlink(missing_ok=True)
    pg_bin = Path(os.environ.get("LOCAL_POSTGRES_BIN", r"C:\Program Files\PostgreSQL\18\bin"))
    work = Path(tempfile.mkdtemp(prefix="manual-", dir=RUNTIME))

    def port(preferred=0):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", preferred))
            except OSError:
                sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    db_port, api_port = port(), port(8000)
    password = secrets.token_urlsafe(32)
    env = dict(os.environ)
    env.update(
        APP_ENV="local",
        SESSION_SECRET=secrets.token_urlsafe(48),
        DATABASE_URL=f"postgresql+asyncpg://postgres:{password}@127.0.0.1:{db_port}/zy_auth_validation",
        DB_SSL_MODE="disable",
        DB_POOL_SIZE="2",
        DB_MAX_OVERFLOW="0",
        DB_CONNECTION_BUDGET="20",
        DB_RESERVED_CONNECTIONS="2",
        WEB_CONCURRENCY="1",
        APP_REPLICAS="1",
        GOOGLE_CLIENT_ID="",
        COOKIE_SAMESITE="lax",
        CORS_ORIGINS='["http://localhost:5173","http://127.0.0.1:5173"]',
        PGHOST="127.0.0.1",
        PGPORT=str(db_port),
        PGUSER="postgres",
        PGPASSWORD=password,
    )

    def run(args, **kwargs):
        subprocess.run([str(a) for a in args], cwd=ROOT, env=env, check=True, **kwargs)

    pwfile = work / "password"
    pwfile.write_text(password, encoding="ascii")
    data = work / "data"
    started = False
    api = None
    try:
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
            stdout=subprocess.DEVNULL,
        )
        pwfile.unlink()
        run(
            [
                pg_bin / "pg_ctl.exe",
                "-D",
                data,
                "-l",
                work / "postgres.log",
                "-o",
                f"-h 127.0.0.1 -p {db_port} -c max_connections=20",
                "-w",
                "start",
            ]
        )
        started = True
        run([pg_bin / "createdb.exe", "zy_auth_validation"])
        run([sys.executable, "-m", "alembic", "upgrade", "head"])
        log = (work / "api.log").open("w", encoding="utf8")
        api = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
                "--no-proxy-headers",
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        base = f"http://127.0.0.1:{api_port}"
        for _ in range(100):
            if api.poll() is not None:
                raise RuntimeError("API failed: inspect " + str(work / "api.log"))
            try:
                with urlopen(base + "/health/ready", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("Readiness timed out")
        template = json.loads(
            (ROOT / "postman/local.postman_environment.json").read_text(encoding="utf-8")
        )
        template["values"][0]["value"] = base
        actual_env = RUNTIME / "running.postman_environment.json"
        actual_env.write_text(json.dumps(template, indent=2), encoding="utf8")
        run(
            [
                "node",
                RUNTIME / "tools/node_modules/newman/bin/newman.js",
                "run",
                ROOT / "postman/zyvalentine.postman_collection.json",
                "-e",
                actual_env,
                "--folder",
                "Automated local",
                "--reporters",
                "cli",
                "--bail",
            ]
        )
        for route in ("/health/live", "/health/ready"):
            with urlopen(base + route, timeout=2) as response:
                print(route, response.status, response.read().decode(), flush=True)
        state.write_text(
            json.dumps(
                {
                    "status": "running",
                    "baseUrl": base,
                    "apiPid": api.pid,
                    "dataPath": str(data),
                    "workPath": str(work),
                }
            ),
            encoding="utf8",
        )
        print("READY_FOR_MANUAL_TESTING " + base + "/docs", flush=True)
        print("STOP: .venv/Scripts/python.exe scripts/stop_local.py", flush=True)
        while not stop.exists():
            if api.poll() is not None:
                raise RuntimeError("API exited")
            time.sleep(1)
    finally:
        if api and api.poll() is None:
            api.terminate()
            try:
                api.wait(timeout=10)
            except subprocess.TimeoutExpired:
                api.kill()
                api.wait()
        if started:
            run([pg_bin / "pg_ctl.exe", "-D", data, "-m", "fast", "-w", "stop"])
        pwfile.unlink(missing_ok=True)
        state.write_text(json.dumps({"status": "stopped", "workPath": str(work)}), encoding="utf8")


if __name__ == "__main__":
    main()
