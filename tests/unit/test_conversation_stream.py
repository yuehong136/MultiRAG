import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# TODO(multirag): Transitional compatibility coverage. Remove these tests when
# the legacy cumulative SSE adapter is deleted.
def _load_transformer_class() -> type:
    source = (Path(__file__).resolve().parents[2] / "api" / "apps" / "conversation_app.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    class_node = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "FullAnswerStreamTransformer")
    isolated_module = ast.Module(body=[class_node], type_ignores=[])
    namespace = {"dataclass": dataclass, "Any": Any}
    exec(compile(isolated_module, filename="conversation_app.py", mode="exec"), namespace)
    return namespace["FullAnswerStreamTransformer"]


def test_full_answer_stream_transformer_rebuilds_legacy_sse_chunks() -> None:
    FullAnswerStreamTransformer = _load_transformer_class()
    transformer = FullAnswerStreamTransformer()

    chunks = [
        transformer.transform({"answer": "", "reference": {}, "id": "m1", "session_id": "c1", "start_to_think": True, "final": False}),
        transformer.transform({"answer": "分析中", "reference": {}, "id": "m1", "session_id": "c1", "final": False}),
        transformer.transform({"answer": "", "reference": {}, "id": "m1", "session_id": "c1", "end_to_think": True, "final": False}),
        transformer.transform({"answer": "最终回答", "reference": {}, "id": "m1", "session_id": "c1", "final": False}),
        transformer.transform({"answer": "", "reference": {"chunks": [{"document_name": "doc"}]}, "id": "m1", "session_id": "c1", "final": True}),
    ]

    assert [chunk["answer"] for chunk in chunks if chunk] == [
        "<think>",
        "<think>分析中",
        "<think>分析中</think>",
        "<think>分析中</think>最终回答",
        "<think>分析中</think>最终回答",
    ]
    assert all("final" not in chunk for chunk in chunks if chunk)
    assert all("start_to_think" not in chunk for chunk in chunks if chunk)
    assert all("end_to_think" not in chunk for chunk in chunks if chunk)
    assert chunks[-1]["reference"] == {"chunks": [{"document_name": "doc"}]}


def test_full_answer_stream_transformer_keeps_single_final_answer_reference() -> None:
    FullAnswerStreamTransformer = _load_transformer_class()
    transformer = FullAnswerStreamTransformer()

    chunk = transformer.transform(
        {
            "answer": "完整答案",
            "reference": {"chunks": [{"document_name": "doc"}]},
            "id": "m1",
            "session_id": "c1",
        }
    )

    assert chunk == {
        "answer": "完整答案",
        "reference": {"chunks": [{"document_name": "doc"}]},
        "id": "m1",
        "session_id": "c1",
    }
