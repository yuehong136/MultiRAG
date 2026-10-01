"""List semantics through real Canvas, Begin, parameter checks and invoke."""

import json
from collections.abc import Callable, Iterator
from copy import deepcopy
from typing import Any

import pytest

from agent.canvas import Canvas
from agent.component.list_operations import ListOperations, ListOperationsParam
from core.utils.redis_conn import REDIS_CONN


@pytest.fixture
def component(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[dict[str, Any], Any], tuple[Canvas, ListOperations]]]:
    canvases: list[Canvas] = []
    monkeypatch.setattr(REDIS_CONN, "delete", lambda *args: None)

    def build(params: dict[str, Any], value: Any = None) -> tuple[Canvas, ListOperations]:
        dsl = {
            "components": {
                "begin": {"obj": {"component_name": "Begin", "params": {}}, "upstream": [], "downstream": ["ListOperations:test"]},
                "ListOperations:test": {"obj": {"component_name": "ListOperations", "params": {"query": "{begin@items}", **params}}, "upstream": ["begin"], "downstream": []},
            },
            "path": [],
            "retrieval": [],
        }
        canvas = Canvas(json.dumps(dsl))
        canvases.append(canvas)
        canvas.get_component_obj("begin").invoke(inputs={"items": {"type": "array", "value": value}})
        operation = canvas.get_component_obj("ListOperations:test")
        assert isinstance(operation, ListOperations)
        return canvas, operation

    yield build
    for canvas in canvases:
        canvas._thread_pool.shutdown(wait=True)


_NEW_CASES = [
    ("nth", -6, [], False),
    ("nth", -5, ["a"], True),
    ("nth", -1, ["e"], True),
    ("nth", 0, [], False),
    ("nth", 1, ["a"], True),
    ("nth", 2, ["b"], True),
    ("nth", 5, ["e"], True),
    ("nth", 6, [], False),
    ("head", -6, [], False),
    ("head", -1, [], False),
    ("head", 0, [], False),
    ("head", 1, ["a"], True),
    ("head", 2, ["a", "b"], True),
    ("head", 5, ["a", "b", "c", "d", "e"], True),
    ("head", 6, ["a", "b", "c", "d", "e"], False),
    ("tail", -6, [], False),
    ("tail", -1, [], False),
    ("tail", 0, [], False),
    ("tail", 1, ["e"], True),
    ("tail", 2, ["d", "e"], True),
    ("tail", 5, ["a", "b", "c", "d", "e"], True),
    ("tail", 6, ["a", "b", "c", "d", "e"], False),
]


def assert_list_output(output: dict[str, Any], expected: list[Any]) -> None:
    assert output["result"] == expected
    assert output["first"] == (expected[0] if expected else None)
    assert output["last"] == (expected[-1] if expected else None)


@pytest.mark.parametrize(("operation", "n", "expected", "valid"), _NEW_CASES)
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_new_operation_matrix(component: Callable[..., Any], operation: str, n: int, expected: list[Any], valid: bool, strict: bool, empty: bool) -> None:
    _, node = component({"operations_version": 2, "operations": operation, "n": n, "strict": strict}, [] if empty else list("abcde"))
    output = node.invoke()
    failed = strict and (empty or not valid)
    assert_list_output(output, [] if failed or empty else expected)
    if failed:
        assert output["_ERROR"] == f"{operation} requires n to be within the valid range in strict mode, got {n}."
    else:
        assert not output.get("_ERROR")


_LEGACY_CASES = [
    ("topN", -6, []),
    ("topN", -1, []),
    ("topN", 0, []),
    ("topN", 1, ["a"]),
    ("topN", 2, ["a", "b"]),
    ("topN", 5, list("abcde")),
    ("topN", 6, list("abcde")),
    ("head", -6, []),
    ("head", -1, []),
    ("head", 0, []),
    ("head", 1, ["a"]),
    ("head", 2, ["b"]),
    ("head", 5, ["e"]),
    ("head", 6, []),
    ("tail", -6, []),
    ("tail", -1, []),
    ("tail", 0, []),
    ("tail", 1, ["e"]),
    ("tail", 2, ["d"]),
    ("tail", 5, ["a"]),
    ("tail", 6, []),
    (None, -6, []),
    (None, -1, []),
    (None, 0, []),
    (None, 1, ["a"]),
    (None, 2, ["a", "b"]),
    (None, 5, list("abcde")),
    (None, 6, list("abcde")),
]


@pytest.mark.parametrize(("operation", "n", "expected"), _LEGACY_CASES)
@pytest.mark.parametrize("empty", [False, True])
def test_legacy_dsl_result_and_serialized_version(component: Callable[..., Any], operation: str | None, n: int, expected: list[Any], empty: bool) -> None:
    params = {"n": n, "strict": True}  # strict was an unused redundant field.
    if operation is not None:
        params["operations"] = operation
    before = deepcopy(params)
    canvas, node = component(params, [] if empty else list("abcde"))
    assert_list_output(node.invoke(), [] if empty else expected)
    assert params == before
    persisted = json.loads(str(canvas))["components"]["ListOperations:test"]["obj"]["params"]
    assert persisted["operations_version"] == 1 and persisted["operations"] == (operation or "topN") and persisted["n"] == n
    _, reloaded = component(persisted, [] if empty else list("abcde"))
    assert_list_output(reloaded.invoke(), [] if empty else expected)


@pytest.mark.parametrize(
    ("strict", "is_strict"),
    [
        (False, False),
        (True, True),
        ("false", False),
        (" FALSE ", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("", False),
        ("garbage", False),
        (None, False),
        (0, False),
        (1, True),
        ("true", True),
        (" TRUE ", True),
        ("1", True),
        ("yes", True),
        ("on", True),
    ],
)
def test_strict_compatibility_values(component: Callable[..., Any], strict: Any, is_strict: bool) -> None:
    _, node = component({"operations_version": 2, "operations": "nth", "n": 0, "strict": strict}, [1])
    output = node.invoke()
    assert bool(output.get("_ERROR")) is is_strict
    assert_list_output(output, [])


@pytest.mark.parametrize(("n", "coerced"), [("2", 2), (" 2 ", 2), (2.9, 2), (-2.9, -2), (True, 1), (False, 0), (None, 0), ("invalid", 0), ({}, 0), ([], 0), (float("inf"), 0), (float("nan"), 0)])
@pytest.mark.parametrize("strict", [False, True])
def test_n_conversion_preserves_existing_int_boundary(component: Callable[..., Any], n: Any, coerced: int, strict: bool) -> None:
    _, node = component({"operations_version": 2, "operations": "nth", "n": n, "strict": strict}, list("abcde"))
    expected = {2: ["b"], -2: ["d"], 1: ["a"], 0: []}[coerced]
    output = node.invoke()
    assert_list_output(output, expected)
    assert bool(output.get("_ERROR")) is (strict and coerced == 0)


@pytest.mark.parametrize("operation", ["nth", "head", "tail", "filter", "sort", "drop_duplicates"])
def test_missing_input_is_empty_array(component: Callable[..., Any], operation: str) -> None:
    canvas, node = component({"operations_version": 2, "operations": operation, "n": 1}, None)
    assert canvas.get_variable_value("{begin@items}") is None
    output = node.invoke()
    assert_list_output(output, [])
    assert not output.get("_ERROR") and node.get_input_value("{begin@items}") == []


@pytest.mark.parametrize("value", ["", "text", 0, False, {}, {"items": []}])
def test_non_list_input_keeps_type_error(component: Callable[..., Any], value: Any) -> None:
    _, node = component({"operations_version": 2, "operations": "head", "n": 2}, value)
    output = node.invoke()
    assert output["_ERROR"] == "The input of List Operations should be an array."
    assert_list_output(output, [])


@pytest.mark.parametrize(
    ("operation", "extra", "value", "expected"),
    [
        ("filter", {"filter": {"operator": "=", "value": "2"}}, [1, 2, "2", None], [2, "2"]),
        ("filter", {"filter": {"operator": "≠", "value": "2"}}, [1, 2, "2", None], [1, None]),
        ("filter", {"filter": {"operator": "contains", "value": "a"}}, ["cat", "dog", "apple"], ["cat", "apple"]),
        ("filter", {"filter": {"operator": "start with", "value": "a"}}, ["cat", "dog", "apple"], ["apple"]),
        ("filter", {"filter": {"operator": "end with", "value": "t"}}, ["cat", "dog", "apple"], ["cat"]),
        ("filter", {"filter": {"operator": "unknown", "value": "a"}}, ["a"], []),
        ("sort", {"sort_method": "asc"}, [3, 1, 2], [1, 2, 3]),
        ("sort", {"sort_method": "desc"}, [3, 1, 2], [3, 2, 1]),
        ("sort", {"sort_method": "asc"}, [{"a": 2, "z": 0}, {"a": 1, "z": 9}], [{"a": 1, "z": 9}, {"a": 2, "z": 0}]),
        ("sort", {"sort_method": "desc"}, [{"a": 2, "z": 0}, {"a": 1, "z": 9}], [{"a": 2, "z": 0}, {"a": 1, "z": 9}]),
        ("drop_duplicates", {}, [2, 1, 2, 3, 1], [2, 1, 3]),
        ("drop_duplicates", {}, [{"a": [1]}, {"a": [1]}, {"a": [2]}], [{"a": [1]}, {"a": [2]}]),
    ],
)
@pytest.mark.parametrize("version", [1, 2])
def test_other_operations_keep_effective_behavior(component: Callable[..., Any], operation: str, extra: dict[str, Any], value: list[Any], expected: list[Any], version: int) -> None:
    _, node = component({"operations_version": version, "operations": operation, **extra}, value)
    output = node.invoke()
    assert_list_output(output, expected)
    assert not output.get("_ERROR")


@pytest.mark.parametrize("operation", [" topN ", "TOPN", "topn"])
@pytest.mark.parametrize("version", [1, 2])
def test_topn_alias_keeps_slice_semantics(component: Callable[..., Any], operation: str, version: int) -> None:
    _, node = component({"operations_version": version, "operations": operation, "n": 2}, list("abcde"))
    assert_list_output(node.invoke(), ["a", "b"])
    assert node._param.operations == ("topN" if version == 1 else "head")


@pytest.mark.parametrize("version", [0, 3, None, True, "2", 2.0])
def test_unknown_version_is_rejected(component: Callable[..., Any], version: Any) -> None:
    with pytest.raises(ValueError, match="operations_version must be"):
        component({"operations_version": version, "operations": "head"}, [])


def test_defaults_and_parameter_copy() -> None:
    param = ListOperationsParam()
    assert param.operations == "nth" and param.operations_version == 2 and param.strict is False
    conf = {"query": "{begin@items}", "n": 2}
    param.update(conf)
    param.check()
    assert conf == {"query": "{begin@items}", "n": 2}
    assert param.operations == "topN" and param.operations_version == 1
    param.update({"query": "{begin@items}", "operations_version": 2})
    param.check()
    assert param.operations == "nth"


def test_repeat_failure_clears_success_and_success_clears_error(component: Callable[..., Any]) -> None:
    canvas, node = component({"operations_version": 2, "operations": "head", "n": 2, "strict": True}, list("abcde"))
    assert_list_output(node.invoke(), ["a", "b"])
    node._param.n = 6
    output = node.invoke()
    assert output["_ERROR"] and "got 6" in output["_ERROR"]
    assert_list_output(output, [])
    node._param.n = 1
    canvas.get_component_obj("begin").invoke(inputs={"items": {"value": "bad", "type": "array"}})
    output = node.invoke()
    assert output["_ERROR"] == "The input of List Operations should be an array."
    assert_list_output(output, [])
    canvas.get_component_obj("begin").invoke(inputs={"items": {"value": ["ok"], "type": "array"}})
    output = node.invoke()
    assert_list_output(output, ["ok"])
    assert "_ERROR" not in output


def test_real_sort_error_and_variable_error_are_not_hidden(component: Callable[..., Any]) -> None:
    _, node = component({"operations_version": 2, "operations": "sort"}, [1, "two"])
    output = node.invoke()
    assert output["_ERROR"] and "not supported" in output["_ERROR"]
    assert_list_output(output, [])
    node._param.query = "{not-a-variable}"
    output = node.invoke()
    assert "not-a-variable" in output["_ERROR"]
    assert_list_output(output, [])
