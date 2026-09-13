"""Puerto de almacenamiento de fotos, con contenedor efímero para el *eager upload*.

Local usa disco (solo APP_ENV=local); develop/staging/production exigen Azure Blob
para que las fotos sobrevivan al reinicio del App Service. El backend de Azure se
importa de forma perezosa: el paquete es un extra opcional (``pip install .[azure]``).

**Dos espacios, no dos backends.** El frontend sube la foto en caliente, antes de que
la carta exista, así que necesita un sitio donde dejarla sin ensuciar el permanente:

- *efímero*: ``AZURE_TEMPORAL_CONTAINER_NAME`` en Azure, y una carpeta local
  ``<LOCAL_STORAGE_DIR>/_temporal`` cuando ``STORAGE_BACKEND=local``. Así el entorno de
  un desarrollador no necesita ninguna credencial de nube para probar el flujo.
- *permanente*: ``AZURE_CONTAINER_NAME`` o la raíz de ``LOCAL_STORAGE_DIR``.

``move_blob`` es el único puente entre los dos y lo cruza el worker, nunca la petición
HTTP. Si no hay contenedor temporal configurado, el efímero degrada al permanente bajo
el prefijo ``temporal/``: no se inventa ninguna variable nueva ni se rompe el arranque.
"""

import uuid
from pathlib import Path

from anyio import to_thread

from app.core.config import Settings
from app.core.errors import ApiError

ALLOWED_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
# Extensión -> tipo. El worker deduce el tipo de la clave temporal en vez de fiarse de
# lo que el navegador declaró: la extensión la puso el servidor tras oler los bytes.
CONTENT_TYPE_BY_SUFFIX = {suffix: media for media, suffix in ALLOWED_CONTENT_TYPES.items()}

# Prefijo de las claves efímeras. Incluye el usuario para que una carta no pueda
# reclamar la foto que subió otra cuenta.
TEMP_PREFIX = "temporal"


def temp_key(user_id: uuid.UUID, content_type: str) -> str:
    """Clave efímera única y atribuible: ``temporal/<usuario>/<uuid><extensión>``."""
    return f"{TEMP_PREFIX}/{user_id}/{uuid.uuid4().hex}{ALLOWED_CONTENT_TYPES[content_type]}"


def temp_key_owner(key: str) -> str:
    """Usuario al que pertenece una clave efímera; cadena vacía si no tiene forma válida."""
    parts = key.split("/")
    return parts[1] if len(parts) == 3 and parts[0] == TEMP_PREFIX else ""


def content_type_of(key: str) -> str:
    media = CONTENT_TYPE_BY_SUFFIX.get(Path(key).suffix.lower())
    if media is None:
        raise ApiError(422, "UNSUPPORTED_MEDIA", "La foto temporal no tiene un tipo válido.")
    return media


class StorageBackend:
    async def put(self, key: str, data: bytes, content_type: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def get(self, key: str) -> bytes:  # pragma: no cover - puerto
        raise NotImplementedError

    async def delete(self, key: str) -> None:  # pragma: no cover - puerto
        raise NotImplementedError

    async def put_temp(self, key: str, data: bytes, content_type: str) -> None:
        """Deja la foto en el espacio efímero (eager upload, antes de existir la carta)."""
        raise NotImplementedError  # pragma: no cover - puerto

    async def move_blob(self, source_key: str, dest_key: str) -> int:
        """Traslada del espacio efímero al permanente y devuelve los bytes movidos.

        El original se borra en cuanto la copia termina: el contenedor temporal no
        debe acumular fotos de cartas ya escritas.

        **Idempotente por destino.** Un mensaje de la cola puede reentregarse después
        de haber movido la foto pero antes de guardar su fila; si el origen ya no
        está y el destino sí, se da por hecho y se devuelve su tamaño. Sin esa regla,
        el reintento perdería la foto para siempre.

        Lanza ``ApiError(404, TEMP_PHOTO_NOT_FOUND)`` si no hay ni origen ni destino.
        """
        raise NotImplementedError  # pragma: no cover - puerto


class LocalStorage(StorageBackend):
    """Disco local. El espacio efímero es una carpeta hermana, no otro backend."""

    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.temporal_root = self.root / "_temporal"
        self.temporal_root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str, root: Path | None = None) -> Path:
        """Resuelve la clave dentro de su raíz, o falla.

        La comprobación es ``is_relative_to`` y no una comparación de prefijos de
        texto: ``str(target).startswith(str(base))`` acepta ``/datos-ajenos`` como si
        estuviera dentro de ``/datos``, que es un escape de directorio de manual.
        """
        base = root or self.root
        target = (base / key).resolve()
        if not target.is_relative_to(base):
            raise ApiError(400, "INVALID_STORAGE_KEY", "Ruta de archivo inválida.")
        return target

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        await to_thread.run_sync(path.write_bytes, data)

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise ApiError(404, "PHOTO_NOT_FOUND", "La foto no está disponible.")
        return await to_thread.run_sync(path.read_bytes)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_file():
            await to_thread.run_sync(path.unlink)

    async def put_temp(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key, self.temporal_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        await to_thread.run_sync(path.write_bytes, data)

    async def move_blob(self, source_key: str, dest_key: str) -> int:
        source = self._path(source_key, self.temporal_root)
        destination = self._path(dest_key)

        def move() -> int:
            if not source.is_file():
                # Reentrada: el traslado ya ocurrió en un intento anterior.
                if destination.is_file():
                    return destination.stat().st_size
                raise ApiError(404, "TEMP_PHOTO_NOT_FOUND", "La foto temporal ya no existe.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            size = source.stat().st_size
            # replace() es atómico dentro del mismo volumen y deja el origen limpio.
            source.replace(destination)
            return size

        return await to_thread.run_sync(move)


class AzureBlobStorage(StorageBackend):
    def __init__(self, connection_string: str, container: str, temporal_container: str | None):
        try:
            from azure.storage.blob import BlobServiceClient  # noqa: PLC0415
        except ImportError:  # pragma: no cover - depende del extra opcional
            raise RuntimeError(
                "STORAGE_BACKEND=azure requiere el extra opcional: pip install '.[azure]'"
            ) from None
        self.service = BlobServiceClient.from_connection_string(connection_string)
        self.container = container
        # Sin contenedor efímero declarado, el permanente hace de los dos: las claves
        # temporales ya viven bajo el prefijo `temporal/` y el traslado sigue siendo
        # copiar y borrar. Evita exigir una variable nueva en un entorno ya desplegado.
        self.temporal_container = temporal_container or container

    def _client(self, key: str, container: str | None = None):
        return self.service.get_blob_client(container=container or self.container, blob=key)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        from azure.storage.blob import ContentSettings  # noqa: PLC0415

        await to_thread.run_sync(
            lambda: self._client(key).upload_blob(
                data, overwrite=True, content_settings=ContentSettings(content_type=content_type)
            )
        )

    async def get(self, key: str) -> bytes:
        def download() -> bytes:
            from azure.core.exceptions import ResourceNotFoundError  # noqa: PLC0415

            try:
                return self._client(key).download_blob().readall()
            except ResourceNotFoundError:
                raise ApiError(404, "PHOTO_NOT_FOUND", "La foto no está disponible.") from None

        return await to_thread.run_sync(download)

    async def delete(self, key: str) -> None:
        def remove() -> None:
            from azure.core.exceptions import ResourceNotFoundError  # noqa: PLC0415

            try:
                self._client(key).delete_blob()
            except ResourceNotFoundError:
                return

        await to_thread.run_sync(remove)

    async def put_temp(self, key: str, data: bytes, content_type: str) -> None:
        from azure.storage.blob import ContentSettings  # noqa: PLC0415

        await to_thread.run_sync(
            lambda: self._client(key, self.temporal_container).upload_blob(
                data, overwrite=True, content_settings=ContentSettings(content_type=content_type)
            )
        )

    async def move_blob(self, source_key: str, dest_key: str) -> int:
        """Copia del contenedor efímero al permanente y borra el original.

        Se traslada leyendo y reescribiendo en vez de con ``start_copy_from_url``: la
        copia del servicio exige una SAS de lectura sobre un contenedor privado, y una
        foto pesa como mucho ``MAX_PHOTO_BYTES`` (3 MB por defecto). Todo ocurre en un
        único salto a hilo, así que el bucle de eventos del worker no se bloquea.
        """

        def move() -> int:
            from azure.core.exceptions import ResourceNotFoundError  # noqa: PLC0415
            from azure.storage.blob import ContentSettings  # noqa: PLC0415

            source = self._client(source_key, self.temporal_container)
            try:
                stream = source.download_blob()
                data = stream.readall()
            except ResourceNotFoundError:
                # Reentrada: si el destino ya tiene la foto, el traslado se completó
                # en una entrega anterior y este mensaje es una repetición.
                try:
                    return self._client(dest_key).get_blob_properties().size
                except ResourceNotFoundError:
                    raise ApiError(
                        404, "TEMP_PHOTO_NOT_FOUND", "La foto temporal ya no existe."
                    ) from None
            # El tipo se deduce de la extensión que puso el servidor tras oler los
            # bytes, no del que traiga el blob temporal: así el permanente nunca
            # sirve una foto con un Content-Type que no hemos decidido nosotros.
            self._client(dest_key).upload_blob(
                data,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type_of(dest_key)),
            )
            # Solo después de que el permanente tenga la foto: si el borrado falla, el
            # peor caso es una copia huérfana en el efímero, nunca una carta sin foto.
            try:
                source.delete_blob()
            except ResourceNotFoundError:  # pragma: no cover - carrera con otra entrega
                pass
            return len(data)

        return await to_thread.run_sync(move)


def build_storage(settings: Settings) -> StorageBackend:
    if settings.storage_backend == "azure":
        return AzureBlobStorage(
            settings.azure_storage_connection_string.get_secret_value(),
            settings.azure_container_name,
            settings.azure_temporal_container_name,
        )
    return LocalStorage(settings.local_storage_dir)


def sniff_content_type(data: bytes, declared: str | None) -> str:
    """Determina el tipo real por firma binaria; el header del cliente no basta."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ApiError(415, "UNSUPPORTED_MEDIA", "Solo se aceptan imágenes JPEG, PNG o WebP.")
