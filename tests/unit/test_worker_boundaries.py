"""Worker CLI, dependency boundaries, and first pipeline progress records."""

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.prompts import generator
from core.svr import executor_metrics, task_executor, task_executor_limiter
from core.svr.worker_cli import parse_worker_args


@pytest.mark.parametrize("argv,index", [([], "0"), (["-i", "1"], "1"), (["--index", "host_3", "-t", "common"], "host_3")])
async def test_cli_configures_consumer_before_main(monkeypatch: pytest.MonkeyPatch, argv: list[str], index: str) -> None:
    seen = []

    async def main() -> None:
        seen.append((task_executor.CONSUMER_NO, task_executor.CONSUMER_NAME))

    monkeypatch.setattr(task_executor, "CONSUMER_NO", "unconfigured")
    monkeypatch.setattr(task_executor, "CONSUMER_NAME", "unconfigured")
    monkeypatch.setattr(task_executor, "main", main)
    monkeypatch.setattr(task_executor, "init_root_logger", lambda name: seen.append(name))
    monkeypatch.setattr(task_executor.faulthandler, "enable", lambda: None)
    # Exercise the real entrypoint in a synchronous callback, outside this loop.
    import asyncio

    await asyncio.to_thread(task_executor.run_task_executor, argv)
    assert seen == ["task_executor_" + index, (index, "task_executor_" + index)]
    assert executor_metrics.resolve_port(9464, index) == (9465 if index == "1" else 9464)


@pytest.mark.parametrize("argv", [["1"], ["-t", "raptor"], ["-t", "graphrag"], ["--unknown"]])
def test_cli_rejects_unsupported_contracts(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as failure:
        parse_worker_args(argv)
    assert failure.value.code == 2


def test_shared_limits_do_not_depend_on_worker_entrypoint() -> None:
    root = Path(__file__).resolve().parents[2]
    for relative in ("core/flow/tokenizer/tokenizer.py", "core/utils/base64_image.py", "core/svr/task_executor_limiter.py"):
        tree = ast.parse((root / relative).read_text())
        assert not any(isinstance(node, ast.ImportFrom) and node.module == "core.svr.task_executor" for node in ast.walk(tree))
    assert task_executor.embed_limiter is task_executor_limiter.embed_limiter
    assert task_executor.minio_limiter is task_executor_limiter.minio_limiter


@pytest.mark.parametrize("initial", [None, "", b"", "[]", b"[]"])
def test_first_pipeline_callback_persists_trace(monkeypatch: pytest.MonkeyPatch, initial: Any) -> None:
    from core.flow import pipeline as module

    writes = []
    monkeypatch.setattr(module, "has_canceled", lambda task_id: False)
    monkeypatch.setattr(module, "REDIS_CONN", SimpleNamespace(get=lambda key: initial, set_obj=lambda *args: writes.append(args)))
    pipeline = module.Pipeline.__new__(module.Pipeline)
    pipeline._flow_id, pipeline.task_id, pipeline._doc_id = "flow", "task", None
    pipeline.callback("parser", 0.2, "first")
    assert len(writes) == 1
    key, records, ttl = writes[0]
    assert key == "flow-task-logs" and ttl == 1800
    assert records[0]["component_id"] == "parser"
    assert records[0]["trace"][0]["message"] == "first"


@pytest.mark.parametrize("shape", ["dict", "pair", "mixed"])
async def test_toc_shapes_keep_source_positions(monkeypatch: pytest.MonkeyPatch, shape: str) -> None:
    sources = [{"title": "Alpha", "chunk_id": "chunk-a"}, {"title": "Beta", "chunk_id": "chunk-b"}, {"title": "Gamma", "chunk_id": "chunk-c"}]

    async def extract(info: dict[str, Any], *args: Any) -> None:
        info["toc"] = sources

    levels: list[Any] = [{"level": "1", "title": item["title"]} for item in sources]
    if shape == "pair":
        levels = [[1, item["title"]] for item in sources]
    elif shape == "mixed":
        levels[1] = "invalid"
    monkeypatch.setattr(generator, "gen_toc_from_text", extract)
    monkeypatch.setattr(generator, "assign_toc_levels", AsyncMock(return_value=levels))
    monkeypatch.setattr(generator.rag_tokenizer, "tokenize", lambda text: text)
    result = await generator.run_toc_from_text(["body"], SimpleNamespace(max_length=8192))
    assert result == [{"level": "1", **item} for i, item in enumerate(sources) if shape != "mixed" or i != 1]


@pytest.mark.parametrize("level", [None, [], {}, "bad", 0, True, "1.2"])
def test_invalid_toc_level_has_no_index_or_type_error(level: Any) -> None:
    assert generator._normalize_toc_levels([{"title": "Alpha", "level": level}], [{"title": "Alpha", "chunk_id": 0}]) == []


def test_toc_reordered_title_cannot_acquire_another_chunk_id() -> None:
    assert generator._normalize_toc_levels([["1", "Beta"]], [{"title": "Alpha", "chunk_id": 0}]) == []
