"""Composite image IDs preserve the producer's opaque object key."""

from typing import Any

import pytest
from PIL import Image

from core.utils import base64_image


@pytest.mark.parametrize("key", ["file.jpg", "page-1.png", "folder/a-b 空间%/image", "page-", "-"])
def test_id2image_preserves_object_key(key: str) -> None:
    calls: list[tuple[str, str]] = []

    def read(*, bucket: str, filename: str) -> bytes:
        calls.append((bucket, filename))
        return base64_image.test_image

    image = base64_image.id2image(f"imagetemps-{key}", read)
    assert isinstance(image, Image.Image)
    try:
        image.load()
        assert image.size == (100, 100)
    finally:
        image.close()
    assert calls == [("imagetemps", key)]


@pytest.mark.parametrize("value", [None, "", "nohyphen", "-key", "bucket-"])
def test_id2image_invalid_ids_never_read_storage(value: str | None) -> None:
    def forbidden(**kwargs: Any) -> bytes:
        pytest.fail("invalid image ID reached storage")

    assert base64_image.id2image(value, forbidden) is None


@pytest.mark.parametrize("mode", ["missing", "empty", "malformed", "transport"])
def test_id2image_unavailable_content(mode: str) -> None:
    def read(**kwargs: Any) -> bytes | None:
        if mode == "transport":
            raise OSError("storage unavailable")
        return {"missing": None, "empty": b"", "malformed": b"invalid"}[mode]

    assert base64_image.id2image("bucket-page-1", read) is None
