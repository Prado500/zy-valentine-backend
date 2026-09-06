FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml requirements.lock README.md ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
# El extra azure se instala siempre: develop/staging/production exigen
# STORAGE_BACKEND=azure, así que el SDK forma parte del artefacto de despliegue.
RUN pip install --no-cache-dir -c requirements.lock ".[azure]" \
    && useradd --create-home --uid 10001 appuser
USER appuser
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --no-proxy-headers"]
