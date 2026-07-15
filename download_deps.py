#!/usr/bin/env python3

# PEP 723 metadata
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "nltk",
#   "huggingface-hub"
# ]
# ///

import argparse
import hashlib
import os
import urllib.request
from collections.abc import Sequence
from pathlib import Path

import nltk
from huggingface_hub import snapshot_download

UV_VERSION = "0.11.27"
UV_RELEASE_BASE_URL = f"https://releases.astral.sh/github/uv/releases/download/{UV_VERSION}"
UV_ARCHIVE_SHA256 = {
    "uv-x86_64-unknown-linux-gnu.tar.gz": "0f4088a04ac92e4c52b4b76759d227a1047355e0ce1dd57cd738a6dec5966bd9",
    "uv-aarch64-unknown-linux-gnu.tar.gz": "321580b9a7069d0cdbd8db9482a5fb62b4f1285110f847746e3b495408e3a08c",
}
NLTK_RESOURCES = {
    "wordnet": Path("corpora/wordnet.zip"),
    "punkt": Path("tokenizers/punkt.zip"),
    "punkt_tab": Path("tokenizers/punkt_tab.zip"),
}


def get_urls(use_china_mirrors: bool = False) -> list[str | list[str]]:
    if use_china_mirrors:
        return [
            "http://mirrors.tuna.tsinghua.edu.cn/ubuntu/pool/main/o/openssl/libssl1.1_1.1.1f-1ubuntu2_amd64.deb",
            "http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports/pool/main/o/openssl/libssl1.1_1.1.1f-1ubuntu2_arm64.deb",
            "https://repo.huaweicloud.com/repository/maven/org/apache/tika/tika-server-standard/3.2.3/tika-server-standard-3.2.3.jar",
            "https://repo.huaweicloud.com/repository/maven/org/apache/tika/tika-server-standard/3.2.3/tika-server-standard-3.2.3.jar.md5",
            "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken",
            ["https://registry.npmmirror.com/-/binary/chrome-for-testing/121.0.6167.85/linux64/chrome-linux64.zip", "chrome-linux64-121-0-6167-85"],
            ["https://registry.npmmirror.com/-/binary/chrome-for-testing/121.0.6167.85/linux64/chromedriver-linux64.zip", "chromedriver-linux64-121-0-6167-85"],
            f"{UV_RELEASE_BASE_URL}/uv-x86_64-unknown-linux-gnu.tar.gz",
            f"{UV_RELEASE_BASE_URL}/uv-aarch64-unknown-linux-gnu.tar.gz",
        ]
    else:
        return [
            "http://archive.ubuntu.com/ubuntu/pool/main/o/openssl/libssl1.1_1.1.1f-1ubuntu2_amd64.deb",
            "http://ports.ubuntu.com/pool/main/o/openssl/libssl1.1_1.1.1f-1ubuntu2_arm64.deb",
            "https://repo1.maven.org/maven2/org/apache/tika/tika-server-standard/3.2.3/tika-server-standard-3.2.3.jar",
            "https://repo1.maven.org/maven2/org/apache/tika/tika-server-standard/3.2.3/tika-server-standard-3.2.3.jar.md5",
            "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken",
            ["https://storage.googleapis.com/chrome-for-testing-public/121.0.6167.85/linux64/chrome-linux64.zip", "chrome-linux64-121-0-6167-85"],
            ["https://storage.googleapis.com/chrome-for-testing-public/121.0.6167.85/linux64/chromedriver-linux64.zip", "chromedriver-linux64-121-0-6167-85"],
            f"{UV_RELEASE_BASE_URL}/uv-x86_64-unknown-linux-gnu.tar.gz",
            f"{UV_RELEASE_BASE_URL}/uv-aarch64-unknown-linux-gnu.tar.gz",
        ]


BUILD_RESOURCE_REPOSITORIES = (
    "InfiniFlow/text_concat_xgb_v1.0",
    "InfiniFlow/deepdoc",
)
RUNTIME_MODEL_REPOSITORIES = (
    "BAAI/bge-large-zh-v1.5",
    "BAAI/bge-reranker-v2-m3",
    "maidalun1020/bce-embedding-base_v1",
    "maidalun1020/bce-reranker-base_v1",
)


def calculate_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(download_url: str, filename: str) -> None:
    path = Path(filename)
    expected_sha256 = UV_ARCHIVE_SHA256.get(filename)
    if path.exists():
        if expected_sha256 is None or calculate_sha256(path) == expected_sha256:
            return
        print(f"Removing stale or invalid dependency: {filename}")
        path.unlink()

    urllib.request.urlretrieve(download_url, path)
    if expected_sha256 is not None and calculate_sha256(path) != expected_sha256:
        path.unlink(missing_ok=True)
        msg = f"SHA256 mismatch for {filename} downloaded from {download_url}"
        raise RuntimeError(msg)


def download_model(repository_id: str, local_directory: Path) -> None:
    resolved_directory = local_directory.expanduser().resolve()
    resolved_directory.mkdir(parents=True, exist_ok=True)
    snapshot_download(repo_id=repository_id, local_dir=str(resolved_directory))


def download_nltk_resources(local_dir: Path) -> None:
    for resource, relative_path in NLTK_RESOURCES.items():
        print(f"Downloading nltk {resource}...")
        if (local_dir / relative_path).is_file():
            continue
        if not nltk.download(resource, download_dir=local_dir):
            msg = f"Failed to download NLTK resource: {resource}"
            raise RuntimeError(msg)


def install_download_opener() -> None:
    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", "Mozilla/5.0")]
    urllib.request.install_opener(opener)


def download_build_dependencies(use_china_mirrors: bool) -> None:
    install_download_opener()
    urls = get_urls(use_china_mirrors)

    for url in urls:
        download_url = url[0] if isinstance(url, list) else url
        filename = url[1] if isinstance(url, list) else url.split("/")[-1]
        print(f"Downloading {filename} from {download_url}...")
        download_file(download_url, filename)

    local_dir = Path("nltk_data").resolve()
    download_nltk_resources(local_dir)

    for repository_id in BUILD_RESOURCE_REPOSITORIES:
        print(f"Downloading build resource {repository_id}...")
        download_model(repository_id, Path("huggingface.co") / repository_id)


def download_runtime_models(runtime_model_dir: Path) -> None:
    for repository_id in RUNTIME_MODEL_REPOSITORIES:
        model_directory = runtime_model_dir / repository_id.rsplit("/", maxsplit=1)[-1]
        print(f"Downloading runtime model {repository_id} to {model_directory}...")
        download_model(repository_id, model_directory)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download dependencies with optional China mirror support")
    parser.add_argument("--china-mirrors", action="store_true", help="Use China-accessible mirrors for downloads")
    parser.add_argument(
        "--runtime-model-dir",
        type=Path,
        help="Also download runtime models into a mount-ready directory",
    )
    parser.add_argument(
        "--runtime-models-only",
        action="store_true",
        help="Skip build dependencies and only populate --runtime-model-dir",
    )
    args = parser.parse_args(argv)
    if args.runtime_models_only and args.runtime_model_dir is None:
        parser.error("--runtime-models-only requires --runtime-model-dir")
    return args


def main(argv: Sequence[str] | None = None) -> None:
    os.chdir(Path(__file__).resolve().parent)
    args = parse_args(argv)

    if not args.runtime_models_only:
        download_build_dependencies(args.china_mirrors)
    if args.runtime_model_dir is not None:
        download_runtime_models(args.runtime_model_dir)


if __name__ == "__main__":
    main()
