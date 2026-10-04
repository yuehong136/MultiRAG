import hashlib
import io
import stat
import zipfile

import pytest
from pydantic import ValidationError

from api.skills.package import normalize_path, read_archive, validate_package
from api.skills.schemas import SkillError, UpdateConfig, UploadManifest


def package_files() -> list[tuple[str, bytes]]:
    return [("SKILL.md", b"---\nname: demo\ndescription: Example\ntags: [test]\n---\nInstructions"), ("scripts/run.txt", b"example")]


def test_directory_and_zip_share_manifest_digest() -> None:
    files = package_files()
    manifest = UploadManifest(name="demo", version="1.2.3-alpha.1+build.7")
    direct = validate_package(manifest, files)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, data in reversed(files):
            archive.writestr(path, data)
    packed = validate_package(manifest, read_archive(buffer.getvalue()))
    assert direct.manifest() == packed.manifest()
    assert direct.content_digest == packed.content_digest
    assert direct.content_digest == hashlib.sha256("".join(f"{file.path}\0{file.sha256}\0{len(file.data)}\n" for file in direct.files).encode()).hexdigest()


@pytest.mark.parametrize("path", ["../secret", "/absolute", "a//b", "a/./b", "a/../b", "a\\b", "C:/windows", "a\x00b", "a\nb"])
def test_paths_cannot_escape_or_alias(path: str) -> None:
    with pytest.raises(SkillError, match="safe POSIX"):
        normalize_path(path)


def test_normalized_path_collision_rejected() -> None:
    with pytest.raises(SkillError, match="Duplicate"):
        validate_package(UploadManifest(name="demo", version="1.0.0"), package_files() + [("e\u0301.txt", b"one"), ("é.txt", b"two")])


def test_zip_symlink_rejected() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "../outside")
    with pytest.raises(SkillError, match="special"):
        read_archive(buffer.getvalue())


def test_manifest_byte_mismatch_rejected() -> None:
    manifest = UploadManifest(name="demo", version="1.0.0", files=[{"path": "SKILL.md", "sha256": "0" * 64, "size": 1}])
    with pytest.raises(SkillError, match="differ"):
        validate_package(manifest, package_files())


@pytest.mark.parametrize("version", ["1.2", "01.2.3", "1.2.3trailing", "1.2.3-01", "1.2.3+", "1.2.3-foo..bar"])
def test_semver_is_complete(version: str) -> None:
    with pytest.raises(ValidationError):
        UploadManifest(name="demo", version=version)


def test_model_ids_do_not_accept_float_or_unsafe_json_number() -> None:
    with pytest.raises(ValidationError):
        UpdateConfig(revision=1, embedding_model_id=9007199254740993)
    assert UpdateConfig(revision=1, embedding_model_id="9007199254740993").embedding_model_id == "9007199254740993"


def test_binary_assets_preserved_and_counted() -> None:
    result = validate_package(UploadManifest(name="demo", version="1.0.0"), package_files() + [("assets/image.bin", b"\xff\x00")])
    assert result.skipped_binary_count == 1
    assert any(file.data == b"\xff\x00" for file in result.files)


def test_unicode_path_segments_match_file_name_column_limit() -> None:
    accepted = "中" * 255
    assert normalize_path(accepted) == accepted
    assert normalize_path(accepted + "/" + accepted) == accepted + "/" + accepted
    with pytest.raises(SkillError) as oversized:
        normalize_path("中" * 256)
    assert oversized.value.code == "INVALID_PATH" and oversized.value.status == 422
