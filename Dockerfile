FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml requirements.lock README.md ./
COPY app ./app
# worker.py forma parte del artefacto: es el consumidor de la cola de cartas.
COPY worker.py ./
COPY alembic ./alembic
COPY alembic.ini ./
# El extra azure se instala siempre: develop/staging/production exigen
# STORAGE_BACKEND=azure, así que el SDK forma parte del artefacto de despliegue.
# El `-e` no es un detalle de estilo: sin él, pip deja una *segunda* copia de
# `app/` en site-packages, además de la que ya trajo el COPY de arriba. Las dos
# compiten por `import app` —gana la que anteceda en `sys.path`, y eso depende de
# cómo se arranque el proceso—, así que basta con que una se quede atrás para
# servir código viejo sin que nada falle a la vista. En editable solo hay /app/app.
RUN pip install --no-cache-dir -c requirements.lock -e ".[azure]" \
    && useradd --create-home --uid 10001 appuser
USER appuser
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --no-proxy-headers"]
