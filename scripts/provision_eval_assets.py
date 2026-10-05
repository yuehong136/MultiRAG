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

REVISION = "7999e1d3359715c523056ef9478215996d62a620"
MODEL = "BAAI/bge-small-zh-v1.5"
DIRECTORY = ROOT / ".cache/evals/bge-small-zh-v1.5"
ASSETS = {
    "config.json": ("config.json", "3853a7979202c348751b753e36f579c41d8da7d36af617d3d907e1fc9b441f2a"),
    "tokenizer.json": ("tokenizer.json", "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26"),
    "tokenizer_config.json": ("tokenizer_config.json", "e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a"),
    "special_tokens_map.json": ("special_tokens_map.json", "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3"),
    "vocab.txt": ("vocab.txt", "45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c"),
    "model.safetensors": ("model.safetensors", "354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026"),
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
            url = f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{remote}"
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
