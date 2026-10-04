from typing import Any

import pytest

from api.skills.schemas import SkillError
from api.skills.storage import SkillStorage


@pytest.mark.asyncio
async def test_delete_does_not_trust_best_effort_exists_false() -> None:
    class Storage:
        def get(self, bucket: str, key: str) -> bytes:
            return b""

        def put(self, bucket: str, key: str, data: bytes) -> None:
            pass

        def rm(self, bucket: str, key: str) -> None:
            pass

        def obj_exist(self, bucket: str, key: str) -> bool:
            return False

        def get_bytes(self, bucket: str, key: str) -> Any:
            raise OSError("authentication failed")

    with pytest.raises(SkillError) as result:
        await SkillStorage(Storage()).remove("bucket", "key")
    assert result.value.code == "STORAGE_DELETE_FAILED"


@pytest.mark.asyncio
async def test_delete_requires_strict_readback_adapter() -> None:
    class Storage:
        def rm(self, bucket: str, key: str) -> None:
            pass

    with pytest.raises(SkillError) as result:
        await SkillStorage(Storage()).remove("bucket", "key")
    assert result.value.code == "STORAGE_VERIFICATION_UNSUPPORTED"


@pytest.mark.asyncio
async def test_encryption_wrapper_cannot_hide_missing_strict_provider_readback() -> None:
    from api.skills.package import PackageFile
    from core.utils.encrypted_storage import EncryptedStorageWrapper

    class LegacyProvider:
        def get(self, *args: Any) -> bytes:
            raise AssertionError("Unsupported provider must never be read")

        def put(self, *args: Any) -> None:
            raise AssertionError("Unsupported provider must never be written")

        def rm(self, *args: Any) -> None:
            raise AssertionError("Unsupported provider must never be deleted")

    # The production wrapper defines get_bytes, but falls back to provider.get.
    inner = object.__new__(EncryptedStorageWrapper)
    inner.storage_impl = LegacyProvider()
    outer = object.__new__(EncryptedStorageWrapper)
    outer.storage_impl = inner
    storage = SkillStorage(outer)
    assert callable(outer.get_bytes) and not storage.supported()
    for operation in (storage.get("b", "k", "0" * 64, 0), storage.put("b", "k", PackageFile("f", b"", "0" * 64, "text/plain")), storage.remove("b", "k")):
        with pytest.raises(SkillError) as result:
            await operation
        assert result.value.status == 503 and result.value.code == "STORAGE_VERIFICATION_UNSUPPORTED"
