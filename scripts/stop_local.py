"""Request graceful shutdown of the local testing API and its own PostgreSQL instance."""

from pathlib import Path

root = Path(__file__).resolve().parents[1]
runtime = root / ".local-validation"
runtime.mkdir(exist_ok=True)
(runtime / "local-server.stop").touch()
print("Shutdown requested for the isolated local testing server.")
