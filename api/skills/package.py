"""Bounded ZIP/directory validation; archive contents are never executed."""

import hashlib
import io
import mimetypes
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from typing import Any

import yaml

from api.skills.schemas import SkillError, UploadManifest

MAX_FILE_SIZE = 5 * 1024 * 1024
MAX_TOTAL_SIZE = 50 * 1024 * 1024
MAX_FILES = 1000


@dataclass(frozen=True)
class PackageFile:
    path: str
    data: bytes
    sha256: str
    media_type: str

    def manifest(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size": len(self.data)}


@dataclass(frozen=True)
class SkillPackage:
    name: str
    version: str
    description: str
    tags: list[str]
    files: list[PackageFile]
    content_digest: str
    skipped_binary_count: int

    def manifest(self) -> dict[str, Any]:
        return {"files": [file.manifest() for file in self.files]}


def normalize_path(value: str) -> str:
    path = unicodedata.normalize("NFC", value)
    if (
        not path
        or len(path) > 512
        or "\\" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} or len(part) > 255 for part in path.split("/"))
        or any(unicodedata.category(c).startswith("C") for c in path)
        or (len(path) >= 2 and path[1] == ":")
    ):
        raise SkillError(422, "INVALID_PATH", "Package paths must be safe POSIX relative paths")
    return path


def read_archive(data: bytes) -> list[tuple[str, bytes]]:
    if len(data) > MAX_TOTAL_SIZE:
        raise SkillError(413, "PACKAGE_TOO_LARGE", "Compressed package exceeds limit")
    files: list[tuple[str, bytes]] = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > 2 * MAX_FILES:
                raise SkillError(413, "TOO_MANY_FILES", "Archive contains too many entries")
            for item in archive.infolist():
                mode = item.external_attr >> 16
                if item.flag_bits & 1 or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                    raise SkillError(422, "INVALID_ARCHIVE", "Encrypted or special archive entries are not allowed")
                path = normalize_path(item.filename[:-1] if item.is_dir() else item.filename)
                if item.is_dir():
                    continue
                if len(files) >= MAX_FILES or item.file_size > MAX_FILE_SIZE:
                    raise SkillError(413, "PACKAGE_TOO_LARGE", "Package exceeds file limits")
                with archive.open(item) as stream:
                    content = stream.read(MAX_FILE_SIZE + 1)
                total += len(content)
                if len(content) > MAX_FILE_SIZE or total > MAX_TOTAL_SIZE:
                    raise SkillError(413, "PACKAGE_TOO_LARGE", "Expanded package exceeds limit")
                files.append((path, content))
    except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError) as exc:
        raise SkillError(422, "INVALID_ARCHIVE", "Invalid or unsupported ZIP archive") from exc
    return files


def validate_package(manifest: UploadManifest, inputs: list[tuple[str, bytes]]) -> SkillPackage:
    if not inputs or len(inputs) > MAX_FILES:
        raise SkillError(413, "TOO_MANY_FILES", "Package requires 1–1000 files")
    files: list[PackageFile] = []
    seen: set[str] = set()
    total = 0
    binary = 0
    for raw_path, data in inputs:
        path = normalize_path(raw_path)
        if path in seen:
            raise SkillError(422, "DUPLICATE_PATH", "Duplicate normalized package path")
        seen.add(path)
        total += len(data)
        if len(data) > MAX_FILE_SIZE or total > MAX_TOTAL_SIZE:
            raise SkillError(413, "PACKAGE_TOO_LARGE", "Package exceeds byte limits")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            binary += 1
        files.append(PackageFile(path, data, hashlib.sha256(data).hexdigest(), mimetypes.guess_type(path)[0] or "application/octet-stream"))
    # A file may not also be a directory prefix.
    if any("/".join(path.split("/")[:i]) in seen for path in seen for i in range(1, len(path.split("/")))):
        raise SkillError(422, "DUPLICATE_PATH", "A file path conflicts with a directory")
    files.sort(key=lambda file: file.path.encode("utf-8"))
    actual = {file.path: file.manifest() for file in files}
    if manifest.files is not None:
        expected: dict[str, dict[str, Any]] = {}
        for entry in manifest.files:
            path = normalize_path(entry.path)
            if path in expected:
                raise SkillError(422, "DUPLICATE_PATH", "Duplicate manifest path")
            expected[path] = {"path": path, "sha256": entry.sha256, "size": entry.size}
        if actual != expected:
            raise SkillError(422, "MANIFEST_MISMATCH", "Package bytes differ from the supplied manifest")
    skill_md = next((file.data for file in files if file.path == "SKILL.md"), None)
    try:
        content = skill_md.decode("utf-8") if skill_md is not None else ""
        lines = content.splitlines()
        if not lines or lines[0] != "---":
            raise ValueError("Missing frontmatter")
        end = lines.index("---", 1)
        frontmatter = yaml.safe_load("\n".join(lines[1:end]))
        if not isinstance(frontmatter, dict) or frontmatter.get("name") != manifest.name:
            raise ValueError("Name mismatch")
        description = frontmatter.get("description")
        tags = frontmatter.get("tags", [])
        if not isinstance(description, str) or not description.strip() or len(description) > 4096:
            raise ValueError("Invalid description")
        if not isinstance(tags, list) or len(tags) > 32 or not all(isinstance(tag, str) and len(tag) <= 256 for tag in tags):
            raise ValueError("Invalid tags")
    except (ValueError, UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
        raise SkillError(422, "INVALID_SKILL", "SKILL.md requires valid UTF-8 YAML name, description and optional tags") from exc
    digest_input = "".join(f"{file.path}\0{file.sha256}\0{len(file.data)}\n" for file in files)
    return SkillPackage(manifest.name, manifest.version, description, tags, files, hashlib.sha256(digest_input.encode()).hexdigest(), binary)
