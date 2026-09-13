#!/bin/sh
# Punto de entrada del contenedor: la API y, cuando hay cola configurada, el
# consumidor que la vacia.
#
# Por que vive en la imagen y no en el "Startup Command" del App Service: el campo
# del portal es configuracion invisible. No esta en el repositorio, no pasa por
# revision, y puede divergir entre dev, staging y main sin que nada lo delate; un
# App Service recreado arrancaria sin worker y nadie lo notaria. Aqui queda
# versionado y es identico en local, en el pipeline y en Azure.
#
# Por que vigila al worker: `python worker.py &` sin supervision deja el peor fallo
# posible, el silencioso. La API seguiria respondiendo 202 —"tu carta esta en
# camino"— mientras nadie vacia la cola. Si el worker termina, este guion detiene
# el proceso principal para que App Service reinicie el contenedor y el fallo se
# vea. Es un intercambio deliberado: un reinicio ruidoso vale mas que un 202 que
# miente en un flujo donde el comprador ya pago.
#
# Por que es seguro tumbar el contenedor: `worker.py` NO termina por una caida de
# Service Bus. Su bucle principal captura la excepcion, registra "El consumidor
# cayo" y reconecta indefinidamente. Solo termina por senal, por configuracion
# invalida o porque falte el paquete `azure-servicebus`; es decir, por errores
# permanentes que hay que ver.
#
# Patron: el "wrapper script" que documenta Docker para los casos en que un
# contenedor debe sostener mas de un proceso.
# https://docs.docker.com/engine/containers/multi-service_container/

set -eu

# PID del proceso principal. Tras el `exec` del final es el de uvicorn, y dentro
# del contenedor es 1. No se escribe `1` a mano para que el guion siga siendo
# ejecutable —y comprobable— fuera de un contenedor.
main_pid=$$

if [ -n "${AZURE_SERVICE_BUS_CONNECTION_STRING:-}" ] && [ -n "${SERVICE_BUS_QUEUE_NAME:-}" ]; then
    echo "entrypoint: cola configurada, arrancando worker.py" >&2
    python worker.py &
    worker_pid=$!

    # Vigilante. Si el worker desaparece, termina el proceso principal y con el el
    # contenedor. El intervalo es configurable solo para que las pruebas no tarden
    # diez segundos en observar el efecto.
    (
        while kill -0 "$worker_pid" 2>/dev/null; do
            sleep "${WORKER_WATCH_INTERVAL:-10}"
        done
        echo "entrypoint: FATAL worker.py termino, deteniendo el contenedor" >&2
        kill -TERM "$main_pid" 2>/dev/null || true
    ) &
else
    # Degradacion elegante, la misma que aplica el resto de la aplicacion: sin las
    # dos variables no hay cola que consumir y la API escribe de forma sincrona.
    echo "entrypoint: sin cola configurada, la API escribe de forma sincrona" >&2
fi

exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-1}" \
    --no-proxy-headers
