FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml requirements.lock README.md ./
COPY app ./app
# worker.py forma parte del artefacto: es el consumidor de la cola de cartas.
COPY worker.py ./
# El guion de arranque: levanta el worker junto a la API y vigila que no muera.
COPY entrypoint.sh ./
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
    && chmod +x entrypoint.sh \
    && useradd --create-home --uid 10001 appuser
USER appuser
EXPOSE 8000
# `CMD` y no `ENTRYPOINT`. El pipeline de CD aplica las migraciones con
# `docker run <imagen> alembic upgrade head`, y esa forma sustituye al CMD. Con
# ENTRYPOINT, "alembic upgrade head" llegaria como argumentos a entrypoint.sh, que
# los ignoraria y arrancaria uvicorn: las migraciones dejarian de aplicarse sin que
# nadie lo note y el pipeline seguiria en verde. No cambiar a ENTRYPOINT sin
# arreglar antes el paso de migraciones de .azure-pipelines/cd-pipeline.yml.
CMD ["./entrypoint.sh"]
