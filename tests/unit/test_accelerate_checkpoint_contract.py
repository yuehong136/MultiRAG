"""Check the installed checkpoint loader with small locally created weights."""

import json
from pathlib import Path

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
