"""Explicit download of the immutable, hash-checked local Chinese evaluation model."""

import os
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.provision_test_assets import ASSETS as OCR_ASSETS
from scripts.provision_test_assets import digest, provision

REVISION = "75c43b069aac4d136ba6bc1122f995fedcfd2781"
MODEL = "BAAI/bge-small-zh-v1.5"
DIRECTORY = ROOT / ".cache/evals/fast-bge-small-zh-v1.5"
ASSETS = {
    "config.json": ("config.json", "d4193ead3a810fd694fa8a31d7fc72fbaebc0668b603e398734bf2f6538ff42f"),
    "tokenizer.json": ("tokenizer.json", "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26"),
    "tokenizer_config.json": ("tokenizer_config.json", "e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a"),
    "special_tokens_map.json": ("special_tokens_map.json", "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3"),
    "model_optimized.onnx": ("onnx/model_quantized.onnx", "15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc"),
}


def verify(directory: Path = DIRECTORY) -> None:
    for name, expected in OCR_ASSETS.items():
        path = ROOT / "core/res/deepdoc" / name
        if not path.is_file() or digest(path) != expected:
            raise RuntimeError(f"Missing or invalid OCR asset {name}; run make eval-assets")
    for name, (_, expected) in ASSETS.items():
        if not (directory / name).is_file() or digest(directory / name) != expected:
            raise RuntimeError(f"Missing or invalid eval asset {name}; run make eval-assets")


def provision_embedding(directory: Path = DIRECTORY) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, (remote, expected) in ASSETS.items():
        target = directory / name
        if target.is_file() and digest(target) == expected:
            continue
        temporary: Path | None = None
        try:
            url = f"https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/{REVISION}/{remote}"
            with urllib.request.urlopen(url, timeout=60) as response, tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
                temporary = Path(stream.name)
                while block := response.read(1024 * 1024):
                    stream.write(block)
            if digest(temporary) != expected:
                raise ValueError(f"SHA256 mismatch for {name}")
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    verify(directory)


if __name__ == "__main__":
    provision(ROOT / "core/res/deepdoc")
    provision_embedding()
