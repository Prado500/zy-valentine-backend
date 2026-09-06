"""Puerto de almacenamiento de fotos.

Local usa disco (solo APP_ENV=local); develop/staging/production exigen Azure Blob
para que las fotos sobrevivan al reinicio del App Service. El backend de Azure se
importa de forma perezosa: el paquete es un extra opcional (``pip install .[azure]``).
"""

from pathlib import Path

from anyio import to_thread

from app.core.config import Settings
from app.core.errors import ApiError

ALLOWED_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class StorageBackend:
    async def put(self, key: str, data: bytes, content_type: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def get(self, key: str) -> bytes:  # pragma: no cover - puerto
        raise NotImplementedError

    async def delete(self, key: str) -> None:  # pragma: no cover - puerto
        raise NotImplementedError


class LocalStorage(StorageBackend):
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        target = (self.root / key).resolve()
        if not str(target).startswith(str(self.root)):
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


class AzureBlobStorage(StorageBackend):
    def __init__(self, connection_string: str, container: str):
        try:
            from azure.storage.blob import BlobServiceClient  # noqa: PLC0415
        except ImportError:  # pragma: no cover - depende del extra opcional
            raise RuntimeError(
                "STORAGE_BACKEND=azure requiere el extra opcional: pip install '.[azure]'"
            ) from None
        self.service = BlobServiceClient.from_connection_string(connection_string)
        self.container = container

    def _client(self, key: str):
        return self.service.get_blob_client(container=self.container, blob=key)

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


def build_storage(settings: Settings) -> StorageBackend:
    if settings.storage_backend == "azure":
        return AzureBlobStorage(
            settings.azure_storage_connection_string.get_secret_value(),
            settings.azure_container_name,
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
