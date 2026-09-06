"""Run Newman on a short-lived loopback API using validate_local.py's isolated DB."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def main():
    url = urlsplit(os.environ.get("TEST_DATABASE_URL", ""))
    if url.hostname != "127.0.0.1" or url.path != "/zy_auth_validation":
        raise SystemExit("Only the ephemeral validation database is accepted")
    newman = ROOT / ".local-validation/tools/node_modules/newman/bin/newman.js"
    if not newman.is_file():
        raise SystemExit(
            "Install Newman locally: npm.cmd install --prefix .local-validation/tools newman@6"
        )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ)
    env["GOOGLE_CLIENT_ID"] = ""
    log_path = ROOT / ".local-validation/api.log"
    with log_path.open("w", encoding="utf8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-proxy-headers",
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            for _ in range(60):
                if process.poll() is not None:
                    raise RuntimeError("Local API stopped; inspect .local-validation/api.log")
                try:
                    with urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=1) as response:
                        if json.load(response)["status"] == "ready":
                            break
                except Exception:
                    time.sleep(0.2)
            else:
                raise RuntimeError("Local API did not become ready")
            subprocess.run(
                [
                    "node",
                    str(newman),
                    "run",
                    "postman/zyvalentine.postman_collection.json",
                    "-e",
                    "postman/local.postman_environment.json",
                    "--env-var",
                    f"baseUrl=http://127.0.0.1:{port}",
                    "--folder",
                    "Automated local",
                    "--folder",
                    "Commerce local",
                    "--reporters",
                    "cli",
                    "--bail",
                ],
                cwd=ROOT,
                env=env,
                check=True,
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
