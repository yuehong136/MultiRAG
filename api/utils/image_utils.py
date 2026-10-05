from io import BytesIO

from PIL import Image

from api.db.db_models import db_connection
from api.db.services.document_image_lock import image_write_locks
from common import settings
from core.utils.encrypted_storage import EncryptedStorageWrapper


def read_chunk_image(bucket: str, name: str) -> bytes | None:
    """Return absence only when the underlying adapter supports strict reads."""
    storage = settings.STORAGE_IMPL
    read = getattr(storage, "get_bytes", None) or storage.get
    data = read(bucket, name)
    adapter = storage.storage_impl if isinstance(storage, EncryptedStorageWrapper) else storage
    # The encryption wrapper exposes get_bytes even for legacy adapters whose
    # get() swallows failures. Its None cannot establish that an object is absent.
    if data is None and not callable(getattr(adapter, "get_bytes", None)):
        raise RuntimeError("Chunk image absence could not be confirmed.")
    if data is not None and not isinstance(data, bytes):
        raise RuntimeError("Chunk image bytes could not be read.")
    return data


def _confirm_chunk_image(bucket: str, name: str, expected: bytes) -> None:
    # Legacy put adapters may swallow errors; confirmation preserves decryption.
    if read_chunk_image(bucket, name) != expected:
        raise RuntimeError("Chunk image write could not be confirmed.")


def replace_chunk_image(bucket: str, name: str, image_binary: bytes) -> None:
    """Replace exact bytes under the shared key lock and confirm storage readback.

    This confirms the object write, not an atomic commit with the chunk index.
    Creation callers retain the append behavior of ``store_chunk_image``.
    """
    with db_connection() as db, image_write_locks(db.get_bind(), [(bucket, name)]):
        settings.STORAGE_IMPL.put(bucket, name, image_binary)
        _confirm_chunk_image(bucket, name, image_binary)


def store_chunk_image(bucket: str, name: str, image_binary: bytes, *, verify: bool = False) -> None:
    """Append by default; REST PATCH also requires exact stored-byte confirmation."""
    with db_connection() as db, image_write_locks(db.get_bind(), [(bucket, name)]):
        expected = _store_locked_chunk_image(bucket, name, image_binary, verify=verify)
        if verify:
            _confirm_chunk_image(bucket, name, expected)


def _store_locked_chunk_image(bucket: str, name: str, image_binary: bytes, *, verify: bool = False) -> bytes:
    # Confirmed REST writes must not interpret a swallowed stat/get error as an
    # absent old image. Other callers retain their existing storage semantics.
    if verify:
        old_binary = read_chunk_image(bucket, name)
        exists = old_binary is not None
    else:
        exists = settings.STORAGE_IMPL.obj_exist(bucket, name)
        old_binary = settings.STORAGE_IMPL.get(bucket, name) if exists else None
    if exists:
        if old_binary is None:
            raise RuntimeError("Existing chunk image bytes could not be read.")
        old_img = Image.open(BytesIO(old_binary)).convert("RGB")
        new_img = Image.open(BytesIO(image_binary)).convert("RGB")
        width = max(old_img.width, new_img.width)
        height = old_img.height + new_img.height
        combined = Image.new("RGB", (width, height), (255, 255, 255))
        combined.paste(old_img, (0, 0))
        combined.paste(new_img, (0, old_img.height))
        buf = BytesIO()
        combined.save(buf, format="JPEG")
        combined_binary = buf.getvalue()
        settings.STORAGE_IMPL.put(bucket, name, combined_binary)
        return combined_binary

    settings.STORAGE_IMPL.put(bucket, name, image_binary)
    return image_binary
