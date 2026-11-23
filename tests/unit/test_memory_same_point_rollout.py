"""Exercise both artifact and restored-state prerequisites in the recovery drill."""

from pathlib import Path

import pytest

from tests.manual.memory_same_point_rollout import STORES, RecoveryPointMismatch, admit_restored, archive_hash, check_archives, fingerprint


def test_archive_check_rejects_missing_and_individually_valid_mixed_backup(tmp_path: Path) -> None:
    paths = {store: tmp_path / store for store in STORES}
    for store, path in paths.items():
        path.write_text("first " + store)
    expected = {store: archive_hash(path) for store, path in paths.items()}
    check_archives(paths, expected)

    with pytest.raises(RecoveryPointMismatch, match="backup set incomplete"):
        check_archives({store: path for store, path in paths.items() if store != "redis"}, expected)
    later = tmp_path / "later-redis"
    later.write_text("later redis")
    with pytest.raises(RecoveryPointMismatch, match="archive digest mismatch: redis"):
        check_archives({**paths, "redis": later}, expected)
    check_archives({**paths, "redis": later}, {**expected, "redis": archive_hash(later)})

    later.unlink()
    with pytest.raises(RecoveryPointMismatch, match="backup archive missing"):
        check_archives({**paths, "redis": later}, expected)


def test_admission_requires_semantic_equality_even_with_three_valid_files() -> None:
    expected = {store: fingerprint([store, 1]) for store in STORES}
    admit_restored(expected, expected)
    with pytest.raises(RecoveryPointMismatch, match="restored state incomplete"):
        admit_restored({"postgresql": expected["postgresql"], "redis": expected["redis"]}, expected)
    with pytest.raises(RecoveryPointMismatch, match="restored state mismatch: redis"):
        admit_restored({**expected, "redis": fingerprint(["redis", 2])}, expected)
