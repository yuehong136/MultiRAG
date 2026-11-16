"""Check the installed checkpoint loader with small locally created weights."""

import json
from pathlib import Path
from typing import Any

import pytest
import torch
from accelerate.utils import modeling


@pytest.mark.parametrize("symlink", [False, True])
def test_sharded_checkpoint_loads_local_weights(tmp_path: Path, symlink: bool) -> None:
    source = torch.nn.Linear(2, 1)
    checkpoint = tmp_path / "snapshot"
    shards = checkpoint / "shards"
    shards.mkdir(parents=True)
    weights = source.state_dict()
    for name, value in weights.items():
        shard = shards / f"{name}.bin"
        if symlink:
            blob = tmp_path / f"blob-{name}"
            torch.save({name: value}, blob)
            shard.symlink_to(blob)
        else:
            torch.save({name: value}, shard)
    (checkpoint / "weights.index.json").write_text(json.dumps({"weight_map": {name: f"shards/{name}.bin" for name in weights}}))
    target = torch.nn.Linear(2, 1)
    modeling.load_checkpoint_in_model(target, str(checkpoint))
    for name, value in target.state_dict().items():
        torch.testing.assert_close(value, weights[name])


@pytest.mark.parametrize("kind", ["absolute", "parent", "nested_parent", "directory", "fifo"])
def test_invalid_shard_is_rejected_before_weight_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    checkpoint = tmp_path / "snapshot"
    checkpoint.mkdir()
    candidate = checkpoint / "invalid.bin"
    if kind == "directory":
        candidate.mkdir()
    elif kind == "fifo":
        import os

        os.mkfifo(candidate)
    name = {
        "absolute": str(tmp_path / "outside.bin"),
        "parent": "../outside.bin",
        "nested_parent": "shards/../../outside.bin",
        "directory": candidate.name,
        "fifo": candidate.name,
    }[kind]
    (checkpoint / "weights.index.json").write_text(json.dumps({"weight_map": {"weight": name}}))
    reads: list[str] = []

    def unexpected_weight_io(path: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        reads.append(path)
        raise AssertionError("Invalid shard reached weight I/O")

    # Bound I/O even if the dependency regresses, so a FIFO can never hang CI.
    monkeypatch.setattr(modeling, "load_state_dict", unexpected_weight_io)
    with pytest.raises(ValueError, match="shard path"):
        modeling.load_checkpoint_in_model(torch.nn.Linear(2, 1), str(checkpoint))
    assert reads == []
