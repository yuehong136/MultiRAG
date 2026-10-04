"""Bounded skill object I/O with independent byte integrity checks."""

import asyncio
import hashlib
import io
import zipfile
from typing import Any

from api.skills.package import PackageFile
from api.skills.schemas import SkillError


class SkillStorage:
    def __init__(self, storage: Any) -> None:
        self.storage = storage

    def supported(self) -> bool:
        if not all(callable(getattr(self.storage, method, None)) for method in ("get", "put", "rm")):
            return False
        adapter = self.storage
        seen: set[int] = set()
        while id(adapter) not in seen:
            seen.add(id(adapter))
            if not callable(getattr(adapter, "get_bytes", None)):
                return False
            if not hasattr(adapter, "storage_impl"):
                return True
            adapter = adapter.storage_impl
        return False

    def require_supported(self) -> None:
        if not self.supported():
            raise SkillError(503, "STORAGE_VERIFICATION_UNSUPPORTED", "Storage lacks strict deletion readback")

    async def put(self, bucket: str, key: str, file: PackageFile) -> None:
        self.require_supported()
        await asyncio.to_thread(self.storage.put, bucket, key, file.data)
        await self.get(bucket, key, file.sha256, len(file.data))

    async def get(self, bucket: str, key: str, digest: str, size: int) -> bytes:
        self.require_supported()
        try:
            data = await asyncio.to_thread(self.storage.get, bucket, key)
        except Exception as exc:
            raise SkillError(503, "STORAGE_UNAVAILABLE", "Skill content is unavailable") from exc
        if not isinstance(data, bytes) or len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise SkillError(503, "CONTENT_INTEGRITY", "Skill content failed integrity verification")
        return data

    async def remove(self, bucket: str, key: str) -> None:
        self.require_supported()
        try:
            if await asyncio.to_thread(self.storage.rm, bucket, key) is False:
                raise SkillError(503, "STORAGE_DELETE_FAILED", "Storage rejected deletion")
            strict_read = getattr(self.storage, "get_bytes", None)
            if not callable(strict_read):
                raise SkillError(503, "STORAGE_VERIFICATION_UNSUPPORTED", "Storage lacks strict deletion readback")
            if await asyncio.to_thread(strict_read, bucket, key) is not None:
                raise SkillError(503, "STORAGE_DELETE_FAILED", "Skill object still exists")
        except SkillError:
            raise
        except Exception as exc:
            raise SkillError(503, "STORAGE_DELETE_FAILED", "Skill object could not be removed") from exc


def zip_files(files: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in files:
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return output.getvalue()
