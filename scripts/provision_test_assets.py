"""Provision the three OCR import assets at a reviewed, immutable revision."""

import hashlib
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVISION = "9c7aa2c730d7a242d7f04cf6109a6a239aefc717"
ASSETS = {
    "det.onnx": "30a86f5731181461d08021402766601e4302a9b9b9666be8aff402696339cdff",
    "rec.onnx": "1c7cf60de2afd728d512f4190cf37455092b45f06175365c6fc58d8cd7e2a68b",
    "ocr.res": "28b2362ad4ab2dc38769aa72feb535e3a9ddb3fd2a7585a05920e6393b1dc7f7",
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def provision(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, expected in ASSETS.items():
        target = directory / name
        if target.is_file() and digest(target) == expected:
            continue
        for base in ("https://huggingface.co", "https://hf-mirror.com"):
            temporary: Path | None = None
            try:
                with urllib.request.urlopen(f"{base}/InfiniFlow/deepdoc/resolve/{REVISION}/{name}", timeout=60) as response:
                    with tempfile.NamedTemporaryFile(dir=directory, prefix=f".{name}-", delete=False) as stream:
                        temporary = Path(stream.name)
                        while block := response.read(1024 * 1024):
                            stream.write(block)
                if digest(temporary) != expected:
                    raise ValueError(f"SHA256 mismatch for {name}")
                os.replace(temporary, target)
                break
            except (OSError, ValueError, urllib.error.URLError):
                continue
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        else:
            raise RuntimeError(f"Could not provision verified OCR asset: {name}")


if __name__ == "__main__":
    provision(ROOT / "core/res/deepdoc")
