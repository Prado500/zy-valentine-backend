# Arranque supervisado del worker de la cola — Plan de implementación

> **Para Claude:** ejecutar tarea por tarea, en orden. Cada tarea termina en commit.
> No empezar sin luz verde explícita del usuario.

**Objetivo:** que el consumidor de la cola (`worker.py`) arranque desde la imagen, con
control de versiones, y que su muerte tumbe el contenedor en vez de dejar una API que
responde 202 sin que nadie vacíe la cola.

**Arquitectura:** un `entrypoint.sh` en la raíz del repositorio pasa a ser el `CMD` de la
imagen. Arranca `worker.py` en segundo plano **solo** cuando las dos variables de Service
Bus están presentes —conservando la degradación elegante que ya tiene la aplicación—, lanza
un vigilante que termina el proceso principal si el worker desaparece, y hace `exec` de
uvicorn para seguir siendo PID 1. No se toca la lógica de negocio, ni el worker, ni el
publicador: solo el arranque del contenedor y un campo de diagnóstico que hoy miente.

**Stack:** Docker, POSIX `sh`, FastAPI, uvicorn, pytest, ruff. Sin dependencias nuevas.

---

## 0. Estado de partida y hallazgos de la inspección

Rama efímera creada: `feat/service-bus-worker-entrypoint`, desde `origin/develop` en
`96e59c9`. Árbol limpio salvo dos ficheros sin seguimiento que **no forman parte de este
cambio**: `docs/INFRAESTRUCTURA_Y_CAPACIDAD.md` y `.claude/`.

### 0.1 Lo que ya funciona y no se toca

| Pieza | Estado |
| --- | --- |
| `app/services/service_bus.py` | Publicador con degradación elegante. Correcto, no se toca |
| `worker.py` | Consumidor con reconexión, dead-letter y tope de intentos. Correcto, no se toca |
| `app/core/config.py` | `service_bus_enabled` y el presupuesto de conexiones. Correcto, no se toca |
| `tests/test_service_bus.py`, `tests/test_worker.py` | Cobertura existente. No se modifican |

### 0.2 El hueco

`Dockerfile:21` solo arranca uvicorn. Nadie lanza `worker.py`. Con las variables puestas
—como ya están en dev, stg y main— la API responde **202** y los mensajes se acumulan sin
consumidor. El fallo es silencioso: no hay error, no hay log, solo cartas que no llegan.

### 0.3 Cuatro hallazgos que condicionan el diseño

**a) `CMD` sí, `ENTRYPOINT` no.** El pipeline de CD ejecuta migraciones así
(`.azure-pipelines/cd-pipeline.yml`):

```bash
docker run --rm -e DATABASE_URL="$FIXED_URL" $(imageRepository):develop alembic upgrade head
```

Esa forma **sustituye al `CMD`**. Si se usara `ENTRYPOINT`, `alembic upgrade head` llegaría
como argumentos al script, que los ignoraría y arrancaría uvicorn: **las migraciones
dejarían de aplicarse y nadie se enteraría**. Es la clase de "mejora" que un revisor puede
sugerir de buena fe, así que va documentada en el propio Dockerfile.

**b) `worker.py` no termina por caídas de Service Bus.** Su bucle `while not stop.is_set()`
captura la excepción, registra `El consumidor cayó` y reconecta indefinidamente
(`worker.py:238-243`). Solo termina por señal, por `ConfigurationError` o porque falte
`azure-servicebus`. Eso hace **seguro** tumbar el contenedor cuando el worker sale: no es
una reacción exagerada a un fallo transitorio, significa que algo está roto de verdad.

**c) Finales de línea.** `git config core.autocrlf` = `true` y no existe `.gitattributes`.
`cat -A Dockerfile` confirma que el árbol de trabajo ya tiene `^M`. Docker tolera CRLF en
las instrucciones del Dockerfile, pero **un script con `#!/bin/sh\r` no se ejecuta en
Linux**. El agente de CI es Ubuntu y respetaría el LF del repositorio, así que el pipeline
no se rompería; lo que se rompe es el `docker build` local de cualquiera en Windows. Se
arregla con `.gitattributes` y se blinda con una prueba estática.

**d) `/api/v1/health/commerce` miente.** `app/services/commerce.py:438` deriva `letterQueue`
de `self.settings.service_bus_enabled`, o sea **solo de que las variables existan**.
Informa `"service-bus"` aunque el cliente no se haya podido construir. El objeto correcto ya
está inyectado en el servicio (`self.queue.enabled`), así que es un cambio de una línea.

### 0.4 Context7

`mcp__context7__*` **no está disponible**: el servidor está registrado para este proyecto
pero falla con `CONNECT_TIMEOUT` a los 30 s, verificado dos veces en esta sesión. Las
decisiones de este plan se apoyan en documentación oficial obtenida directamente:

- [Docker · Run multiple services in a container](https://docs.docker.com/engine/containers/multi-service_container/)
  — *"It's best practice to separate areas of concern by using one service per container"*, y
  el patrón de **wrapper script** que *"starts processes in the background and waits for one
  to exit"*. Es exactamente el patrón que falta hoy.
- [Azure App Service · Health check](https://learn.microsoft.com/en-us/azure/app-service/monitor-instances-health-check)
  — *"if your application depends on a database and a messaging system, the Health check
  endpoint should connect to those components"*.
- [Azure Service Bus · throttling](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-throttling)
  y el resto de fuentes ya recogidas en `docs/INFRAESTRUCTURA_Y_CAPACIDAD.md`.

Nota honesta: el ideal de Docker sería un contenedor aparte para el worker (un job de
Container Apps o un sidecar de App Service). Se descarta **a propósito**: el docstring de
`worker.py` declara *"Corre como proceso aparte, en el mismo servidor que la API"*, la
validación del presupuesto de conexiones cuenta a los dos juntos, y montar otro servicio
para un equipo de tres personas y 100 USD/mes de infraestructura no se paga. El patrón de
wrapper script es el que Docker documenta para justo este caso.

---

## 1. Alcance

### Entra

1. `.gitattributes` nuevo: finales de línea LF para scripts de shell.
2. `entrypoint.sh` nuevo: arranque de worker + uvicorn con supervisión.
3. `Dockerfile`: copiar el script, darle permisos y apuntar el `CMD`.
4. `tests/test_entrypoint.py` nuevo: estáticas sobre el script y el Dockerfile, más
   funcionales sobre el comportamiento real del script.
5. `app/services/commerce.py:438`: que `letterQueue` diga la verdad.
6. Portal: quitar el Startup Command de los tres App Services **después** de desplegar, y
   dar de alta las alertas sobre las tres colas.
7. Propagación `develop` → `staging` → `main`.

### No entra, y por qué

| Fuera de alcance | Motivo |
| --- | --- |
| `/health/ready` devolviendo 500 si la cola no responde | Cambia el comportamiento de reinicio del App Service y exige decidir umbrales. Es la "capa 2" y pertenece a la sesión de rendimiento, ya anotada en `docs/INFRAESTRUCTURA_Y_CAPACIDAD.md` §9 |
| Contenedor aparte para el worker | Ver 0.4. Decisión consciente |
| `AutoLockRenewer` en el worker | El lock de 5 min ya cubre el peor lote medido. YAGNI hasta tener datos de producción |
| Hueco de `APP_REPLICAS` en el presupuesto de conexiones | Real (`config.py:325`), inocuo con `APP_REPLICAS=1`. Anotado en el documento de capacidad §5.3; arreglarlo aquí mezclaría dos asuntos en un PR |
| Refactor de `commerce.py` | No hay código espagueti que justifique tocarlo. Ver §6 |

---

## 2. Tarea 1 · `.gitattributes`

**Ficheros:**
- Crear: `.gitattributes`

**Paso 1 — Escribir la prueba que falla.** Añadir a `tests/test_entrypoint.py` (el fichero
se crea en esta tarea, con esta única prueba por ahora):

```python
"""El contenedor arranca el worker de la cola, y su muerte no pasa desapercibida.

Son comprobaciones estáticas y funcionales del guion de arranque: no construyen la
imagen ni abren conexiones, así que corren en cualquier máquina y en el pipeline.
"""

from tests.conftest import REPO_ROOT

GITATTRIBUTES = REPO_ROOT / ".gitattributes"


def test_los_scripts_de_shell_se_versionan_con_finales_lf():
    """Un `#!/bin/sh\\r` no se ejecuta en Linux, y aquí `core.autocrlf` es `true`.

    Sin esta regla, quien clone en Windows obtiene CRLF en el árbol de trabajo y su
    `docker build` produce una imagen que no arranca. El pipeline no lo vería: el
    agente es Ubuntu y respeta el LF del repositorio.
    """
    assert GITATTRIBUTES.is_file(), "falta .gitattributes"
    reglas = GITATTRIBUTES.read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in reglas
```

**Paso 2 — Verificar que falla.**

```bash
python -m pytest tests/test_entrypoint.py -v
```
Esperado: `FAILED ... AssertionError: falta .gitattributes`

**Paso 3 — Crear `.gitattributes`:**

```
# Los scripts de shell viajan al contenedor Linux: CRLF rompe el shebang.
*.sh text eol=lf

# El Dockerfile lo tolera, pero mantenerlo homogéneo evita diffs de ruido.
Dockerfile text eol=lf
```

**Paso 4 — Verificar que pasa.**

```bash
python -m pytest tests/test_entrypoint.py -v
```
Esperado: `1 passed`

**Paso 5 — Commit.**

```bash
git add .gitattributes tests/test_entrypoint.py
git commit -m "chore(git): forzar finales LF en scripts de shell"
```

---

## 3. Tarea 2 · `entrypoint.sh` y sus pruebas estáticas

**Ficheros:**
- Crear: `entrypoint.sh`
- Modificar: `tests/test_entrypoint.py`

**Paso 1 — Escribir las pruebas que fallan.** Añadir a `tests/test_entrypoint.py`:

```python
ENTRYPOINT = REPO_ROOT / "entrypoint.sh"


def test_el_guion_de_arranque_existe_y_tiene_shebang():
    assert ENTRYPOINT.is_file(), "falta entrypoint.sh"
    assert ENTRYPOINT.read_bytes().startswith(b"#!/bin/sh\n")


def test_el_guion_no_lleva_retornos_de_carro():
    """Blindaje del .gitattributes: si alguien lo salta, esto se pone en rojo."""
    assert b"\r" not in ENTRYPOINT.read_bytes(), (
        "entrypoint.sh tiene CRLF; el contenedor Linux no podrá ejecutarlo"
    )


def test_uvicorn_se_lanza_con_exec_para_seguir_siendo_pid_1():
    """Sin `exec`, el shell queda de PID 1 y uvicorn no recibe el SIGTERM del apagado."""
    guion = ENTRYPOINT.read_text(encoding="utf-8")
    assert "exec uvicorn app.main:app" in guion


def test_el_worker_solo_arranca_con_las_dos_variables_de_la_cola():
    """Misma puerta que `Settings.service_bus_enabled`: sin cola no hay worker."""
    guion = ENTRYPOINT.read_text(encoding="utf-8")
    assert "AZURE_SERVICE_BUS_CONNECTION_STRING" in guion
    assert "SERVICE_BUS_QUEUE_NAME" in guion
    assert "python worker.py &" in guion


def test_el_vigilante_no_asume_que_el_proceso_principal_sea_el_pid_1():
    """`kill -TERM 1` haría el script inejecutable (y no comprobable) fuera del contenedor."""
    guion = ENTRYPOINT.read_text(encoding="utf-8")
    assert "kill -TERM 1" not in guion
    assert 'main_pid=$$' in guion
```

**Paso 2 — Verificar que fallan.**

```bash
python -m pytest tests/test_entrypoint.py -v
```
Esperado: 5 `FAILED` con `falta entrypoint.sh`, 1 `passed`.

**Paso 3 — Crear `entrypoint.sh`** (fichero completo, con finales LF):

```sh
#!/bin/sh
# Punto de entrada del contenedor: la API y, cuando hay cola configurada, el
# consumidor que la vacía.
#
# Por qué vive en la imagen y no en el "Startup Command" del App Service: el campo
# del portal es configuración invisible. No está en el repositorio, no pasa por
# revisión, y puede divergir entre dev, staging y main sin que nada lo delate; un
# App Service recreado arrancaría sin worker y nadie lo notaría. Aquí queda
# versionado y es idéntico en local, en el pipeline y en Azure.
#
# Por qué vigila al worker: `python worker.py &` sin supervisión deja el peor fallo
# posible, el silencioso. La API seguiría respondiendo 202 —"tu carta está en
# camino"— mientras nadie vacía la cola. Si el worker termina, este guion detiene
# el proceso principal para que App Service reinicie el contenedor y el fallo se
# vea. Es un intercambio deliberado: un reinicio ruidoso vale más que un 202 que
# miente en un flujo donde el comprador ya pagó.
#
# Por qué es seguro tumbar el contenedor: `worker.py` NO termina por una caída de
# Service Bus. Su bucle principal captura la excepción, registra "El consumidor
# cayó" y reconecta indefinidamente. Solo termina por señal, por configuración
# inválida o porque falte el paquete `azure-servicebus`; es decir, por errores
# permanentes que hay que ver.
#
# Patrón: el "wrapper script" que documenta Docker para los casos en que un
# contenedor debe sostener más de un proceso.
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

    # Vigilante. Si el worker desaparece, termina el proceso principal y con él el
    # contenedor. El intervalo es configurable solo para que las pruebas no tarden
    # diez segundos en observar el efecto.
    (
        while kill -0 "$worker_pid" 2>/dev/null; do
            sleep "${WORKER_WATCH_INTERVAL:-10}"
        done
        echo "entrypoint: FATAL worker.py terminó, deteniendo el contenedor" >&2
        kill -TERM "$main_pid" 2>/dev/null || true
    ) &
else
    # Degradación elegante, la misma que aplica el resto de la aplicación: sin las
    # dos variables no hay cola que consumir y la API escribe de forma síncrona.
    echo "entrypoint: sin cola configurada, la API escribe de forma sincrona" >&2
fi

exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-1}" \
    --no-proxy-headers
```

**Paso 4 — Verificar que pasan.**

```bash
python -m pytest tests/test_entrypoint.py -v
```
Esperado: `6 passed`

**Paso 5 — Commit.**

```bash
git add entrypoint.sh tests/test_entrypoint.py
git commit -m "feat(docker): guion de arranque que supervisa el worker de la cola"
```

---

## 4. Tarea 3 · Pruebas funcionales del guion

Las estáticas comprueban que el texto dice lo correcto; estas comprueban que **hace** lo
correcto. Se ejecuta el guion de verdad con `sh`, sustituyendo `python` y `uvicorn` por
dobles en el `PATH`. Verificado en esta sesión: `sh` y `kill -0` funcionan en Git Bash y en
el agente Ubuntu del pipeline.

**Ficheros:**
- Modificar: `tests/test_entrypoint.py`

**Paso 1 — Escribir las pruebas que fallan.** Añadir:

```python
import os
import shutil
import subprocess
import sys

import pytest

sin_sh = pytest.mark.skipif(
    shutil.which("sh") is None, reason="requiere un shell POSIX"
)


def _doble(directorio, nombre, cuerpo):
    """Crea un ejecutable de mentira en el PATH de la prueba."""
    ruta = directorio / nombre
    ruta.write_text(f"#!/bin/sh\n{cuerpo}\n", encoding="utf-8", newline="\n")
    ruta.chmod(0o755)
    return ruta


def _entorno(directorio, **extra):
    entorno = dict(os.environ)
    entorno["PATH"] = f"{directorio}{os.pathsep}{entorno['PATH']}"
    entorno.pop("AZURE_SERVICE_BUS_CONNECTION_STRING", None)
    entorno.pop("SERVICE_BUS_QUEUE_NAME", None)
    entorno.update(extra)
    return entorno


@sin_sh
def test_sin_cola_configurada_no_arranca_el_worker(tmp_path):
    """Degradación elegante: la API sola, exactamente como antes de este cambio."""
    _doble(tmp_path, "python", f'echo worker >> "{tmp_path}/huellas"')
    _doble(tmp_path, "uvicorn", f'echo api >> "{tmp_path}/huellas"')

    subprocess.run(
        ["sh", str(ENTRYPOINT)], cwd=REPO_ROOT, env=_entorno(tmp_path),
        timeout=30, check=True, capture_output=True,
    )

    huellas = (tmp_path / "huellas").read_text(encoding="utf-8").split()
    assert huellas == ["api"], f"arrancó algo que no debía: {huellas}"


@sin_sh
def test_con_cola_configurada_arranca_el_worker_y_la_api(tmp_path):
    _doble(tmp_path, "python", f'echo worker >> "{tmp_path}/huellas"; sleep 5')
    _doble(tmp_path, "uvicorn", f'echo api >> "{tmp_path}/huellas"')

    subprocess.run(
        ["sh", str(ENTRYPOINT)], cwd=REPO_ROOT,
        env=_entorno(
            tmp_path,
            AZURE_SERVICE_BUS_CONNECTION_STRING="Endpoint=sb://x/;EntityPath=q",
            SERVICE_BUS_QUEUE_NAME="q",
        ),
        timeout=30, check=True, capture_output=True,
    )

    huellas = sorted((tmp_path / "huellas").read_text(encoding="utf-8").split())
    assert huellas == ["api", "worker"]


@sin_sh
def test_si_el_worker_muere_el_guion_termina(tmp_path):
    """El caso que motiva todo esto: nada de 202 sin consumidor."""
    _doble(tmp_path, "python", "exit 1")            # el worker se cae al instante
    _doble(tmp_path, "uvicorn", "sleep 60")         # la API viviría un minuto

    resultado = subprocess.run(
        ["sh", str(ENTRYPOINT)], cwd=REPO_ROOT,
        env=_entorno(
            tmp_path,
            AZURE_SERVICE_BUS_CONNECTION_STRING="Endpoint=sb://x/;EntityPath=q",
            SERVICE_BUS_QUEUE_NAME="q",
            WORKER_WATCH_INTERVAL="1",
        ),
        timeout=30, capture_output=True,
    )

    assert resultado.returncode != 0, "el guion sobrevivió a la muerte del worker"
    assert b"FATAL" in resultado.stderr


@sin_sh
def test_un_worker_vivo_no_interrumpe_la_api(tmp_path):
    """El vigilante no debe disparar por su cuenta mientras todo va bien."""
    _doble(tmp_path, "python", "sleep 60")
    _doble(tmp_path, "uvicorn", 'sleep 4; echo api-termino-sola')

    resultado = subprocess.run(
        ["sh", str(ENTRYPOINT)], cwd=REPO_ROOT,
        env=_entorno(
            tmp_path,
            AZURE_SERVICE_BUS_CONNECTION_STRING="Endpoint=sb://x/;EntityPath=q",
            SERVICE_BUS_QUEUE_NAME="q",
            WORKER_WATCH_INTERVAL="1",
        ),
        timeout=30, capture_output=True,
    )

    assert resultado.returncode == 0
    assert b"api-termino-sola" in resultado.stdout


@sin_sh
def test_una_sola_de_las_dos_variables_no_activa_el_worker(tmp_path):
    """Misma puerta que `Settings.service_bus_enabled`: hacen falta las dos."""
    _doble(tmp_path, "python", f'echo worker >> "{tmp_path}/huellas"')
    _doble(tmp_path, "uvicorn", f'echo api >> "{tmp_path}/huellas"')

    subprocess.run(
        ["sh", str(ENTRYPOINT)], cwd=REPO_ROOT,
        env=_entorno(tmp_path, SERVICE_BUS_QUEUE_NAME="q"),
        timeout=30, check=True, capture_output=True,
    )

    assert (tmp_path / "huellas").read_text(encoding="utf-8").split() == ["api"]
```

**Paso 2 — Ejecutar.**

```bash
python -m pytest tests/test_entrypoint.py -v
```
Esperado: `11 passed`. Si alguna falla, el guion tiene un defecto real: arreglar el guion,
nunca la prueba.

**Paso 3 — Commit.**

```bash
git add tests/test_entrypoint.py
git commit -m "test(docker): comportamiento real del guion de arranque con dobles"
```

---

## 5. Tarea 4 · El `Dockerfile`

**Ficheros:**
- Modificar: `Dockerfile`
- Modificar: `tests/test_entrypoint.py`

**Paso 1 — Escribir las pruebas que fallan.** Añadir:

```python
DOCKERFILE = REPO_ROOT / "Dockerfile"


def test_la_imagen_arranca_por_el_guion():
    contenido = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY entrypoint.sh ./" in contenido
    assert 'CMD ["./entrypoint.sh"]' in contenido


def test_el_guion_recibe_permiso_de_ejecucion_antes_de_bajar_de_privilegios():
    """`chmod` después de `USER appuser` fallaría: el fichero es de root."""
    lineas = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    chmod = next(i for i, l in enumerate(lineas) if "chmod +x entrypoint.sh" in l)
    usuario = next(i for i, l in enumerate(lineas) if l.startswith("USER "))
    assert chmod < usuario


def test_la_imagen_usa_cmd_y_no_entrypoint():
    """El CD ejecuta `docker run <imagen> alembic upgrade head`, que sustituye al CMD.

    Con ENTRYPOINT, esos argumentos llegarían al guion, que los ignora y arranca
    uvicorn: las migraciones dejarían de aplicarse **en silencio**.
    """
    contenido = DOCKERFILE.read_text(encoding="utf-8")
    assert "ENTRYPOINT" not in contenido
```

**Paso 2 — Verificar que fallan.**

```bash
python -m pytest tests/test_entrypoint.py -v -k "imagen or permiso"
```
Esperado: 3 `FAILED`.

**Paso 3 — Modificar el `Dockerfile`.** Tres cambios quirúrgicos:

Tras la línea `COPY worker.py ./` (línea 7), añadir:

```dockerfile
# El guion de arranque: levanta el worker junto a la API y vigila que no muera.
COPY entrypoint.sh ./
```

En el `RUN` de instalación, encadenar el `chmod` antes del `useradd`:

```dockerfile
RUN pip install --no-cache-dir -c requirements.lock -e ".[azure]" \
    && chmod +x entrypoint.sh \
    && useradd --create-home --uid 10001 appuser
```

Y sustituir el `CMD` final por:

```dockerfile
# `CMD` y no `ENTRYPOINT`: el pipeline de CD ejecuta
# `docker run <imagen> alembic upgrade head`, y esa forma sustituye al CMD. Con
# ENTRYPOINT, "alembic upgrade head" llegaría como argumentos a entrypoint.sh, que
# los ignoraría y arrancaría uvicorn: las migraciones dejarían de aplicarse sin que
# nadie lo note. No cambiar a ENTRYPOINT sin arreglar antes el paso de migraciones.
CMD ["./entrypoint.sh"]
```

**Paso 4 — Verificar que pasan, y que no se rompió `test_packaging.py`.**

```bash
python -m pytest tests/test_entrypoint.py tests/test_packaging.py -v
```
Esperado: todo verde. `test_la_imagen_instala_el_paquete_en_modo_editable` sigue pasando
porque la línea del `pip install` conserva su `-e`.

**Paso 5 — Commit.**

```bash
git add Dockerfile tests/test_entrypoint.py
git commit -m "feat(docker): arrancar el contenedor por entrypoint.sh"
```

---

## 6. Tarea 5 · Que `/health/commerce` diga la verdad

**Ficheros:**
- Modificar: `app/services/commerce.py:438`
- Crear: `tests/test_health_commerce.py`

Hoy el endpoint informa `"service-bus"` con solo mirar si las variables existen. Si la
cadena está mal y `build_publisher` devolvió un `MockPublisher`, **el diagnóstico certifica
salud sobre un sistema que escribe de forma síncrona**. El objeto correcto ya está inyectado.

**Paso 1 — Escribir las pruebas que fallan.** Crear `tests/test_health_commerce.py`:

```python
"""El diagnóstico refleja el publicador real, no la mera presencia de variables.

Rule of 10 sobre `CommerceService.health`: es lo que mira un operador para decidir si
el asincronismo está vivo, así que no puede decir "service-bus" cuando la aplicación
está escribiendo de forma síncrona.
"""

from app.core.config import Settings
from app.services.commerce import CommerceService
from app.services.service_bus import MockPublisher

SECRET = "test-only-secret-000000000000000000000"
CONNECTION = (
    "Endpoint=sb://ejemplo.servicebus.windows.net/;SharedAccessKeyName=send;"
    "SharedAccessKey=" + "A" * 43 + "="
)


class PublicadorReal(MockPublisher):
    """Doble del publicador de Azure: lo único que importa aquí es `enabled`."""

    enabled = True


def _servicio(queue, **cambios) -> CommerceService:
    settings = Settings(_env_file=None, session_secret=SECRET, **cambios)
    return CommerceService(
        db=None, settings=settings, storage=None, mailer=None, payments=None, queue=queue
    )


def test_sin_cola_configurada_informa_sincrono():
    assert _servicio(MockPublisher()).health().letterQueue == "sync"


def test_con_publicador_real_informa_service_bus():
    servicio = _servicio(
        PublicadorReal(),
        azure_service_bus_connection_string=CONNECTION,
        service_bus_queue_name="letters-dev",
    )
    assert servicio.health().letterQueue == "service-bus"


def test_variables_presentes_pero_publicador_degradado_informa_sincrono():
    """El caso que hoy miente: la cadena existe pero el cliente no se pudo construir."""
    servicio = _servicio(
        MockPublisher("cliente no construible"),
        azure_service_bus_connection_string=CONNECTION,
        service_bus_queue_name="letters-dev",
    )
    assert servicio.health().letterQueue == "sync"


def test_el_resto_del_diagnostico_no_cambia():
    """Guarda de no-regresión: solo se toca `letterQueue`."""
    salud = _servicio(MockPublisher()).health()
    assert salud.paymentProvider == "none"
    assert salud.storageBackend == "local"
    assert salud.mailBackend == "console"
    assert salud.freezeAfterPublish is True


def test_el_diagnostico_nunca_expone_la_cadena_de_conexion():
    servicio = _servicio(
        PublicadorReal(),
        azure_service_bus_connection_string=CONNECTION,
        service_bus_queue_name="letters-dev",
    )
    volcado = servicio.health().model_dump_json()
    assert "SharedAccessKey" not in volcado
    assert "servicebus.windows.net" not in volcado
```

**Paso 2 — Verificar que falla.**

```bash
python -m pytest tests/test_health_commerce.py -v
```
Esperado: `test_variables_presentes_pero_publicador_degradado_informa_sincrono` FAILED; el
resto pasa (el comportamiento actual coincide por casualidad en los demás casos).

**Paso 3 — El cambio.** En `app/services/commerce.py`, dentro de `health()`:

```python
            # `self.queue.enabled` y no `settings.service_bus_enabled`: las variables
            # pueden estar puestas y aun así haber degradado a `MockPublisher` por una
            # cadena inválida o por falta del paquete. El diagnóstico tiene que reflejar
            # lo que la aplicación **hace**, no lo que se le pidió que hiciera.
            letterQueue="service-bus" if self.queue.enabled else "sync",
```

**Paso 4 — Verificar.**

```bash
python -m pytest tests/test_health_commerce.py -v
```
Esperado: `5 passed`

**Paso 5 — Commit.**

```bash
git add app/services/commerce.py tests/test_health_commerce.py
git commit -m "fix(health): informar el publicador real y no la presencia de variables"
```

---

## 7. Tarea 6 · Suite completa y estilo

**Paso 1 — Ruff.**

```bash
python -m ruff check .
python -m ruff format --check .
```
Esperado: `All checks passed!`

**Paso 2 — Suite completa.** Sin PostgreSQL local, las de integración se omiten con motivo
explícito; eso es lo esperado en esta máquina.

```bash
python -m pytest -q
```
Esperado: todo verde, **sin ninguna regresión** respecto a la línea base. Anotar el conteo
antes de empezar para poder compararlo.

**Paso 3 — Commit si ruff tocó algo.**

```bash
git add -A && git commit -m "style: aplicar ruff format"
```

---

## 8. Tarea 7 · Pull request a `develop`

```bash
git push -u origin feat/service-bus-worker-entrypoint
gh pr create --base develop --title "feat(docker): arranque supervisado del worker de la cola" --body "..."
```

El cuerpo del PR debe recoger, como mínimo: el hueco que se cierra, el intercambio
deliberado de tumbar el contenedor, **la advertencia de no cambiar `CMD` por `ENTRYPOINT`**,
y el recordatorio de borrar el Startup Command tras el despliegue (tarea 8).

El pipeline de CI corre `ruff check` y `pytest -v` con PostgreSQL de servicio. Las pruebas
nuevas no necesitan base de datos ni Azure, así que corren enteras.

---

## 9. Tarea 8 · Portal, después de que despliegue dev

⚠️ **Orden importante.** Mientras el Startup Command siga puesto, **sobrescribe el `CMD` de
la imagen** y el `entrypoint.sh` no se ejecuta. No hay ventana de caída: el comando viejo
sigue levantando el worker hasta que se borra el campo.

1. Esperar a que el CD despliegue `develop` (webhook del App Service de dev).
2. **App Service de dev → Configuration → General settings → Startup Command → vaciar el
   campo → Save.** El contenedor reinicia.
3. Verificar en **Log stream** las tres líneas:
   ```
   entrypoint: cola configurada, arrancando worker.py
   Service Bus activo sobre la cola configurada
   Worker activo: lotes de 12 mensajes, pool de 2 conexiones
   ```
4. `curl https://<app-dev>.azurewebsites.net/api/v1/health/commerce` → `"letterQueue": "service-bus"`.
   Con el cambio de la tarea 5, ahora esto **sí** significa que el cliente se construyó.
5. Enviar una carta de prueba y comprobar en el portal que **Active message count** de
   `letters-dev` sube a 1 y vuelve a 0 en segundos.

## 10. Tarea 9 · Alertas sobre las tres colas

Es la única capa que detecta el síntoma directamente, pase lo que pase dentro del contenedor.

Para cada cola — `letters-dev`, `letters-stg`, `letters-main`:

**`sb-zv-shared` → la cola → Alerts → Create alert rule**

| Campo | Valor |
| --- | --- |
| Signal | `Active Messages` (Count) |
| Aggregation | Maximum |
| Operator / Threshold | Greater than · `5` en dev y stg, `20` en main |
| Evaluación | cada 5 min, ventana de 15 min |
| Action group | correo del equipo |

Si entran mensajes y nadie los saca, el aviso llega en quince minutos.

## 11. Tarea 10 · Propagación

1. Merge del PR a `develop` → CD despliega dev. Ejecutar tarea 8 sobre dev y **verificar de
   punta a punta antes de seguir**.
2. PR `develop` → `staging`. Merge → CD despliega stg. Repetir tarea 8 sobre stg (el Log
   stream debe decir `pool de 2 conexiones`).
3. PR `staging` → `main`. Merge → CD despliega main. Repetir tarea 8 sobre main (`pool de 5
   conexiones`).
4. Borrar la rama efímera.

No saltar pasos: cada entorno tiene su propia cola y su propia cadena, y el fallo típico
—`EntityPath` que no coincide con `SERVICE_BUS_QUEUE_NAME`— es **silencioso**.

---

## 12. Riesgos y cómo se mitigan

| Riesgo | Mitigación |
| --- | --- |
| El guion llega con CRLF y el contenedor no arranca | `.gitattributes` (tarea 1) más `test_el_guion_no_lleva_retornos_de_carro` |
| Alguien cambia `CMD` por `ENTRYPOINT` y las migraciones dejan de correr | `test_la_imagen_usa_cmd_y_no_entrypoint` más el comentario en el propio Dockerfile |
| Un error de configuración en main tumba el sitio en vez de degradarlo | Intercambio deliberado y documentado. Se mitiga verificando dev y stg **antes** de main |
| Queda el Startup Command puesto y el cambio parece no surtir efecto | Tarea 8, paso 2, con la advertencia en el cuerpo del PR |
| El vigilante dispara por un fallo transitorio de Service Bus | No puede: `worker.py` no termina por eso (§0.3.b) |
| `chmod` después de `USER appuser` | `test_el_guion_recibe_permiso_de_ejecucion_antes_de_bajar_de_privilegios` |

---

## 13. Observaciones fuera de alcance

Encontradas durante la inspección. **Ninguna se toca en este PR**; se dejan anotadas.

1. **No hay código espagueti que justifique refactor.** Las capas están separadas conforme a
   `python-architecture`: routers sin repositorios, servicios con la lógica, repositorios con
   SQLAlchemy 2.0 async. `CommerceService` es grande pero cohesivo, y `fulfil_queued_letter`
   reutiliza deliberadamente la misma orquestación del camino síncrono para que no vuelvan a
   divergir. No hay nada que enderezar aquí.
2. **CI corre Python 3.11, la imagen es `python:3.12-slim`.** `.python-version` dice 3.11 y
   `ruff` apunta a `py311`. Se prueba en una versión y se despliega en otra. Riesgo bajo pero
   real; merece su propio ticket.
3. **`.claude/` sin seguimiento en la raíz de `be/`.** Decidir si se versiona o se añade al
   `.gitignore`, antes de que entre en un commit por accidente.
4. **Hueco de `APP_REPLICAS`** en el presupuesto de conexiones (`config.py:325`). Detallado en
   `docs/INFRAESTRUCTURA_Y_CAPACIDAD.md` §5.3.
5. **`/health/ready` no comprueba la cola.** Es la "capa 2" del esquema de detección; va con la
   sesión de rendimiento (§9 del documento de capacidad).

---

## 14. Resumen de ficheros

| Acción | Fichero | Tarea |
| --- | --- | --- |
| Crear | `.gitattributes` | 1 |
| Crear | `entrypoint.sh` | 2 |
| Crear | `tests/test_entrypoint.py` | 1, 2, 3, 4 |
| Crear | `tests/test_health_commerce.py` | 5 |
| Modificar | `Dockerfile` (3 cambios) | 4 |
| Modificar | `app/services/commerce.py:438` (1 línea) | 5 |

**Seis ficheros. Una línea de lógica de negocio tocada.** Todo lo demás es arranque del
contenedor y pruebas.
