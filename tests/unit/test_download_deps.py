import hashlib
from pathlib import Path

import pytest

import download_deps


def test_get_urls_pin_expected_uv_release() -> None:
    urls = download_deps.get_urls(use_china_mirrors=True)

    uv_urls = [url for url in urls if isinstance(url, str) and "/uv-" in url]
    assert uv_urls == [
        f"{download_deps.UV_RELEASE_BASE_URL}/uv-x86_64-unknown-linux-gnu.tar.gz",
        f"{download_deps.UV_RELEASE_BASE_URL}/uv-aarch64-unknown-linux-gnu.tar.gz",
    ]


def test_download_file_replaces_invalid_cached_uv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    filename = "uv-x86_64-unknown-linux-gnu.tar.gz"
    expected_content = b"uv 0.11.27 test archive"
    expected_sha256 = hashlib.sha256(expected_content).hexdigest()
    monkeypatch.setitem(download_deps.UV_ARCHIVE_SHA256, filename, expected_sha256)
    monkeypatch.chdir(tmp_path)
    Path(filename).write_bytes(b"stale uv archive")

    def fake_urlretrieve(url: str, output: str | Path) -> tuple[str, None]:
        assert url == "https://example.test/uv.tar.gz"
        Path(output).write_bytes(expected_content)
        return str(output), None

    monkeypatch.setattr(download_deps.urllib.request, "urlretrieve", fake_urlretrieve)

    download_deps.download_file("https://example.test/uv.tar.gz", filename)

    assert Path(filename).read_bytes() == expected_content


def test_runtime_models_are_not_build_resources() -> None:
    assert set(download_deps.BUILD_RESOURCE_REPOSITORIES).isdisjoint(
        download_deps.RUNTIME_MODEL_REPOSITORIES,
    )


def test_download_runtime_models_uses_mount_ready_flat_layout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    downloads: list[tuple[str, Path]] = []

    def fake_download_model(repository_id: str, local_directory: Path) -> None:
        downloads.append((repository_id, local_directory))

    monkeypatch.setattr(download_deps, "download_model", fake_download_model)

    download_deps.download_runtime_models(tmp_path)

    assert downloads == [(repository_id, tmp_path / repository_id.rsplit("/", maxsplit=1)[-1]) for repository_id in download_deps.RUNTIME_MODEL_REPOSITORIES]
