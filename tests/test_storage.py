"""Almacenamiento: contenedor efímero y traslado al permanente.

Rule of 10 sobre ``move_blob``, que es la operación que decide si una foto pagada
llega o se pierde. Ninguna prueba necesita PostgreSQL ni credenciales de Azure: el
backend local usa disco y el de Azure se ejerce con un cliente falso, así que todas
se ejecutan también en el pipeline.
"""

import uuid

import pytest

from app.core.errors import ApiError
from app.services.storage import (
    AzureBlobStorage,
    LocalStorage,
    content_type_of,
    sniff_content_type,
    temp_key,
    temp_key_owner,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
USER = uuid.uuid4()


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(str(tmp_path / "fotos"))


async def upload(storage: LocalStorage, data: bytes = PNG) -> str:
    key = temp_key(USER, "image/png")
    await storage.put_temp(key, data, "image/png")
    return key


# --- move_blob: los cinco casos ---------------------------------------------------------


async def test_happy_path_moves_bytes_and_clears_the_temporary(storage):
    source = await upload(storage)
    size = await storage.move_blob(source, "letters/abc/foto.png")

    assert size == len(PNG)
    assert await storage.get("letters/abc/foto.png") == PNG
    # El efímero queda limpio: no debe acumular fotos de cartas ya escritas.
    with pytest.raises(ApiError) as gone:
        await storage.get(f"_temporal/{source}")
    assert gone.value.status_code == 404


async def test_sad_path_missing_source_is_reported_as_not_found(storage):
    with pytest.raises(ApiError) as error:
        await storage.move_blob(f"temporal/{USER}/{uuid.uuid4().hex}.png", "letters/abc/foto.png")

    assert error.value.status_code == 404
    assert error.value.detail["code"] == "TEMP_PHOTO_NOT_FOUND"


async def test_edge_reentry_after_a_completed_move_is_not_an_error(storage):
    """El mensaje se reentrega tras mover la foto pero antes de guardar su fila.

    El origen ya no existe y el destino sí: el traslado se da por hecho y se
    devuelve el tamaño. Sin esta regla, el reintento perdería la foto para siempre.
    """
    source = await upload(storage)
    await storage.move_blob(source, "letters/abc/foto.png")

    assert await storage.move_blob(source, "letters/abc/foto.png") == len(PNG)
    assert await storage.get("letters/abc/foto.png") == PNG


async def test_edge_concurrent_moves_of_different_photos_do_not_collide(storage):
    """Doce fotos del mismo lote van a doce destinos distintos, sin pisarse."""
    import asyncio

    sources = [await upload(storage, PNG + bytes([index])) for index in range(12)]
    sizes = await asyncio.gather(
        *(
            storage.move_blob(source, f"letters/abc/{index}.png")
            for index, source in enumerate(sources)
        )
    )

    assert sizes == [len(PNG) + 1] * 12
    stored = [await storage.get(f"letters/abc/{index}.png") for index in range(12)]
    assert len({bytes(item) for item in stored}) == 12


@pytest.mark.parametrize(
    "key",
    ["../fuera.png", "sub/../../fuera.png", "/absoluta.png"],
)
async def test_exception_path_traversal_is_rejected(storage, key):
    """Una clave que se sale de su raíz nunca llega al disco.

    La comprobación es `is_relative_to`, no un prefijo de texto: `<raiz>-ajena`
    empieza por `<raiz>` y sin embargo está fuera.
    """
    with pytest.raises(ApiError) as error:
        await storage.move_blob(key, "letters/abc/foto.png")
    assert error.value.detail["code"] == "INVALID_STORAGE_KEY"

    with pytest.raises(ApiError):
        await storage.get(key)


async def test_sibling_directory_is_not_inside_the_root(tmp_path):
    """Regresión: `startswith` aceptaba `/datos-ajenos` como si fuera `/datos`."""
    storage = LocalStorage(str(tmp_path / "datos"))
    (tmp_path / "datos-ajenos").mkdir()

    with pytest.raises(ApiError):
        await storage.get("../datos-ajenos/secreto.png")


# --- Backend de Azure con un cliente falso -----------------------------------------------


class FakeBlob:
    def __init__(self, container: dict, name: str):
        self.container = container
        self.name = name

    def download_blob(self):
        from azure.core.exceptions import ResourceNotFoundError

        if self.name not in self.container:
            raise ResourceNotFoundError(self.name)
        return FakeStream(self.container[self.name])

    def get_blob_properties(self):
        from azure.core.exceptions import ResourceNotFoundError

        if self.name not in self.container:
            raise ResourceNotFoundError(self.name)
        return type("Props", (), {"size": len(self.container[self.name])})()

    def upload_blob(self, data, overwrite=False, content_settings=None):
        self.container[self.name] = bytes(data)
        self.content_type = getattr(content_settings, "content_type", None)

    def delete_blob(self):
        from azure.core.exceptions import ResourceNotFoundError

        if self.name not in self.container:
            raise ResourceNotFoundError(self.name)
        del self.container[self.name]


class FakeStream:
    def __init__(self, data: bytes):
        self.data = data
        self.properties = type(
            "Props", (), {"content_settings": type("CS", (), {"content_type": "image/png"})()}
        )()

    def readall(self) -> bytes:
        return self.data


class FakeService:
    """Doble del `BlobServiceClient`: dos contenedores en memoria."""

    def __init__(self):
        self.containers: dict[str, dict[str, bytes]] = {"permanente": {}, "temporal": {}}
        self.uploads: list[tuple[str, str | None]] = []

    def get_blob_client(self, container: str, blob: str):
        client = FakeBlob(self.containers[container], blob)
        original = client.upload_blob

        def record(data, overwrite=False, content_settings=None):
            original(data, overwrite=overwrite, content_settings=content_settings)
            self.uploads.append((blob, getattr(content_settings, "content_type", None)))

        client.upload_blob = record
        return client


@pytest.fixture
def azure(mocker):
    pytest.importorskip("azure.storage.blob")
    service = FakeService()
    mocker.patch(
        "azure.storage.blob.BlobServiceClient.from_connection_string", return_value=service
    )
    storage = AzureBlobStorage("UseDevelopmentStorage=true", "permanente", "temporal")
    return storage, service


async def test_azure_move_copies_then_deletes_the_original(azure):
    storage, service = azure
    service.containers["temporal"]["temporal/u/1.png"] = PNG

    size = await storage.move_blob("temporal/u/1.png", "letters/abc/1.png")

    assert size == len(PNG)
    assert service.containers["permanente"]["letters/abc/1.png"] == PNG
    assert "temporal/u/1.png" not in service.containers["temporal"]
    # El tipo lo decide el servidor por la extensión, no el blob de origen.
    assert service.uploads == [("letters/abc/1.png", "image/png")]


async def test_azure_move_is_reentrant_when_the_destination_exists(azure):
    storage, service = azure
    service.containers["permanente"]["letters/abc/1.png"] = PNG

    assert await storage.move_blob("temporal/u/1.png", "letters/abc/1.png") == len(PNG)


async def test_azure_move_without_source_or_destination_fails_cleanly(azure):
    storage, _ = azure

    with pytest.raises(ApiError) as error:
        await storage.move_blob("temporal/u/desaparecida.png", "letters/abc/1.png")

    assert error.value.detail["code"] == "TEMP_PHOTO_NOT_FOUND"
    # El mensaje que ve la persona no menciona contenedores, cuentas ni rutas.
    assert "temporal/u" not in error.value.detail["message"]


# --- Claves efímeras ----------------------------------------------------------------------


def test_temp_key_is_attributable_and_typed():
    key = temp_key(USER, "image/webp")

    assert temp_key_owner(key) == str(USER)
    assert content_type_of(key) == "image/webp"


@pytest.mark.parametrize("key", ["temporal/x", "letters/u/1.png", "", "temporal//1.png"])
def test_keys_outside_the_temporary_namespace_have_no_owner(key):
    assert temp_key_owner(key) == ""


def test_unknown_extension_is_not_given_a_media_type():
    with pytest.raises(ApiError) as error:
        content_type_of("letters/abc/archivo.exe")
    assert error.value.status_code == 422


@pytest.mark.parametrize(
    ("data", "declared"),
    [
        (b"<script>alert(1)</script>", "image/png"),
        (b"GIF89a", "image/png"),
        (b"%PDF-1.7", "image/jpeg"),
        (b"", "image/png"),
    ],
)
def test_content_type_comes_from_the_bytes_not_from_the_client(data, declared):
    """Declarar `image/png` no convierte un script en una imagen."""
    with pytest.raises(ApiError) as error:
        sniff_content_type(data, declared)
    assert error.value.status_code == 415
