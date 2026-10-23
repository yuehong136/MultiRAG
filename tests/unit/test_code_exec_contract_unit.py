#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import types
from typing import Any

import pytest

from agent.tools import code_exec


def _load_module() -> types.ModuleType:
    return code_exec


def _build_code_exec(output_type: str) -> code_exec.CodeExec:
    return _build_code_exec_with_outputs({"result": {"value": None, "type": output_type}})


def _build_code_exec_with_outputs(outputs: dict[str, dict[str, Any]]) -> code_exec.CodeExec:
    tool = code_exec.CodeExec.__new__(code_exec.CodeExec)
    tool._param = types.SimpleNamespace(outputs=outputs)
    tool._canvas = types.SimpleNamespace(get_tenant_id=lambda: "tenant-1")
    return tool


def test_select_business_output_ignores_system_outputs():
    module = _load_module()
    outputs = {
        "content": {"value": "", "type": "string"},
        "actual_type": {"value": "", "type": "string"},
        "_ERROR": {"value": "", "type": "string"},
        "_ARTIFACTS": {"value": [], "type": "Array<Object>"},
        "_ATTACHMENT_CONTENT": {"value": "", "type": "string"},
        "attachments": {"value": [], "type": "Array<String>"},
        "raw_result": {"value": None, "type": "Any"},
        "_created_time": {"value": 1.0, "type": "Number"},
        "_elapsed_time": {"value": 2.0, "type": "Number"},
        "result": {"value": None, "type": "Array<Number>"},
    }

    name, meta = module.select_business_output(outputs)

    assert name == "result"
    assert meta["type"] == "Array<Number>"


def test_array_result_is_preserved_as_single_business_value():
    module = _load_module()
    contract = module.build_code_exec_contract(
        {"result": {"value": None, "type": "Array<Number>"}},
        (1, 2, 3),
    )

    assert contract["business_output"] == "result"
    assert contract["value"] == [1, 2, 3]
    assert contract["actual_type"] == "Array<Number>"
    assert contract["content"] == "[\n  1,\n  2,\n  3\n]"


def test_object_result_is_not_wrapped_by_business_name():
    module = _load_module()
    contract = module.build_code_exec_contract(
        {"result": {"value": None, "type": "Object"}},
        {"foo": "bar", "n": 1},
    )

    assert contract["business_output"] == "result"
    assert contract["value"] == {"foo": "bar", "n": 1}
    assert contract["content"] == '{\n  "foo": "bar",\n  "n": 1\n}'


def test_canonical_object_rendering_is_key_order_stable():
    module = _load_module()
    assert module.render_canonical_content({"b": 1, "a": 2}) == '{\n  "a": 2,\n  "b": 1\n}'


def test_lowercase_object_expected_type_validates():
    module = _load_module()
    contract = module.build_code_exec_contract(
        {"result": {"value": None, "type": "object"}},
        {"foo": "bar"},
    )

    assert contract["actual_type"] == "Object"
    assert contract["value"] == {"foo": "bar"}


def test_tuple_is_normalized_to_array_semantics():
    module = _load_module()
    assert module.normalize_output_value((1, 2, 3)) == [1, 2, 3]
    assert module.infer_actual_type((1, 2, 3)) == "Array<Number>"


def test_list_is_preserved_as_list_without_normalization_changes():
    module = _load_module()
    values = [1, 2, 3]
    normalized = module.normalize_output_value(values)
    assert normalized == [1, 2, 3]
    assert isinstance(normalized, list)


def test_canonical_content_rendering_handles_common_shapes():
    module = _load_module()
    assert module.render_canonical_content("hello") == "hello"
    assert module.render_canonical_content(None) == ""
    assert module.render_canonical_content(1.5) == "1.5"
    assert module.render_canonical_content({"x": [1, 2]}) == '{\n  "x": [\n    1,\n    2\n  ]\n}'


def test_any_does_not_allow_unsupported_top_level_python_types():
    module = _load_module()
    with pytest.raises(module.ContractError, match="unsupported top-level result type"):
        module.build_code_exec_contract(
            {"result": {"value": None, "type": "Any"}},
            {1, 2},
        )


def test_mismatch_raises_contract_error():
    module = _load_module()
    with pytest.raises(module.ContractError, match="expected type Number"):
        module.build_code_exec_contract({"result": {"value": None, "type": "Number"}}, "not-a-number")


def test_array_number_rejects_string_elements_without_coercion():
    module = _load_module()
    with pytest.raises(module.ContractError, match=r"expected type Number, got String"):
        module.build_code_exec_contract({"result": {"value": None, "type": "Array<Number>"}}, ["1", 2])


def test_boolean_rejects_string_form_without_coercion():
    module = _load_module()
    with pytest.raises(module.ContractError, match=r"expected type Boolean, got String"):
        module.build_code_exec_contract({"result": {"value": None, "type": "Boolean"}}, "true")


def test_lowercase_array_number_expected_type_validates():
    module = _load_module()
    contract = module.build_code_exec_contract(
        {"result": {"value": None, "type": "array<number>"}},
        (1, 2, 3),
    )

    assert contract["actual_type"] == "Array<Number>"
    assert contract["value"] == [1, 2, 3]


def test_lowercase_array_string_expected_type_validates():
    module = _load_module()
    contract = module.build_code_exec_contract(
        {"result": {"value": None, "type": "array<string>"}},
        ("a", "b"),
    )

    assert contract["actual_type"] == "Array<String>"
    assert contract["value"] == ["a", "b"]


@pytest.mark.parametrize("schema", ["Array<>", "Array< >", "array<>", "array< >"])
def test_malformed_array_schema_is_rejected(schema):
    module = _load_module()
    with pytest.raises(module.ContractError, match="Unsupported expected type"):
        module.build_code_exec_contract({"result": {"value": None, "type": schema}}, [1, 2])


def test_any_and_empty_expected_type_skip_validation():
    module = _load_module()
    assert module.build_code_exec_contract({"result": {"value": None, "type": "Any"}}, {"foo": "bar"})["value"] == {"foo": "bar"}
    assert module.build_code_exec_contract({"result": {"value": None, "type": ""}}, {"foo": "bar"})["value"] == {"foo": "bar"}
    assert module.build_code_exec_contract({"result": {"value": None, "type": None}}, {"foo": "bar"})["value"] == {"foo": "bar"}


def test_legacy_multi_output_schema_is_rejected():
    module = _load_module()
    with pytest.raises(module.ContractError, match="exactly one business output"):
        module.select_business_output(
            {
                "result": {"value": None, "type": "Number"},
                "answer": {"value": None, "type": "String"},
                "_ERROR": {"value": "", "type": "string"},
            }
        )


@pytest.mark.parametrize("name", ["content", "actual_type", "attachments", "_ERROR", "_ARTIFACTS", "_ATTACHMENT_CONTENT", "raw_result"])
def test_reserved_business_output_names_are_rejected(name):
    module = _load_module()
    with pytest.raises(module.ContractError, match="reserved output name"):
        module.build_code_exec_contract(
            {name: {"value": None, "type": "String"}},
            "ok",
        )


def test_dotted_business_output_name_is_rejected():
    module = _load_module()
    with pytest.raises(module.ContractError, match=r"must not contain '.'"):
        module.build_code_exec_contract(
            {"payload.items": {"value": None, "type": "Array<String>"}},
            ["a"],
        )


def test_process_execution_result_preserves_whole_array_for_single_business_output():
    tool = _build_code_exec("Array<String>")

    result = tool._process_execution_result('["a", "b"]', None, "unit-test")

    assert result["result"] == ["a", "b"]
    assert result["content"] == '[\n  "a",\n  "b"\n]'
    assert result["raw_result"] == ["a", "b"]


def test_process_execution_result_sets_actual_type_from_contract_value():
    tool = _build_code_exec("Object")

    result = tool._process_execution_result('{"foo": "bar"}', None, "unit-test")

    assert result["result"] == {"foo": "bar"}
    assert result["actual_type"] == "Object"


def test_process_execution_result_contract_mismatch_sets_error_and_clears_business_output():
    tool = _build_code_exec("Number")

    result = tool._process_execution_result('["a", "b"]', None, "unit-test")

    assert "expected type Number" in result["_ERROR"]
    assert result["result"] is None
    assert result["actual_type"] == "Array<String>"
    assert result["raw_result"] == ["a", "b"]


def test_process_execution_result_invalid_schema_clears_stale_business_outputs():
    tool = _build_code_exec_with_outputs(
        {
            "result": {"value": "stale-result", "type": "String"},
            "answer": {"value": {"stale": True}, "type": "Object"},
            "_ERROR": {"value": "", "type": "string"},
        }
    )

    result = tool._process_execution_result('["a", "b"]', None, "unit-test")

    assert "exactly one business output" in result["_ERROR"]
    assert result["result"] is None
    assert result["answer"] is None
    assert result["actual_type"] == "Array<String>"
    assert result["raw_result"] == ["a", "b"]


def test_process_execution_result_keeps_business_output_when_stderr_is_non_fatal():
    tool = _build_code_exec("Object")

    result = tool._process_execution_result('{"foo": "bar"}', "warning on stderr", "unit-test")

    assert result["_ERROR"] == ""
    assert result["result"] == {"foo": "bar"}
    assert result["content"] == '{\n  "foo": "bar"\n}'


def test_process_execution_result_clears_outputs_for_stderr_only_without_artifacts() -> None:
    tool = _build_code_exec("String")

    result = tool._process_execution_result("", "hard failure", "unit-test")

    assert result["_ERROR"] == "hard failure"
    assert result.get("result") is None
    assert result["content"] == ""
    assert result["attachments"] == []
    assert result["_ARTIFACTS"] is None


def test_process_execution_result_appends_artifact_content_to_canonical_content():
    tool = _build_code_exec("Object")
    tool._upload_artifacts = lambda _artifacts: [{"name": "chart.png", "url": "/artifact/chart.png", "mime_type": "image/png", "size": 12}]
    tool._build_attachment_content = lambda _artifacts, _artifact_urls: "attachment_count: 1\n\nattachment1 (image): chart.png\nparsed artifact"

    result = tool._process_execution_result(
        '{"foo": "bar"}',
        None,
        "unit-test",
        artifacts=[{"name": "chart.png", "content_b64": "ZmFrZQ==", "mime_type": "image/png", "size": 12}],
    )

    assert result["result"] == {"foo": "bar"}
    assert result["content"] == '{\n  "foo": "bar"\n}\n\nattachment_count: 1\n\nattachment1 (image): chart.png\nparsed artifact'
    assert result["_ARTIFACTS"] == [{"name": "chart.png", "url": "/artifact/chart.png", "mime_type": "image/png", "size": 12}]
    assert result["_ARTIFACTS"][0]["mime_type"] == "image/png"
    assert result["_ATTACHMENT_CONTENT"] == "attachment_count: 1\n\nattachment1 (image): chart.png\nparsed artifact"
    assert "attachment1 (image): chart.png" in result["_ATTACHMENT_CONTENT"]
    assert result["attachments"] == ["![chart.png](/artifact/chart.png)"]


def test_build_attachment_markdown_list_formats_images_downloads_and_names():
    tool = _build_code_exec("Object")

    result = tool._build_attachment_markdown_list(
        [
            {"name": "chart.png", "url": "/artifact/chart.png", "mime_type": "image/png"},
            {"name": "report.pdf", "url": "/artifact/report.pdf", "mime_type": "application/pdf"},
            {"name": "notes.txt", "url": "", "mime_type": "text/plain"},
            {"name": "", "url": "/artifact/missing-name.csv", "mime_type": "text/csv"},
        ]
    )

    assert result == [
        "![chart.png](/artifact/chart.png)",
        "[Download report.pdf](/artifact/report.pdf)",
        "notes.txt",
    ]


def test_process_execution_result_without_artifacts_clears_stale_artifacts_output():
    tool = _build_code_exec_with_outputs(
        {
            "result": {"value": None, "type": "String"},
            "_ARTIFACTS": {"value": [{"name": "stale"}], "type": "Array<Object>"},
            "attachments": {"value": ["stale"], "type": "Array<String>"},
        }
    )

    result = tool._process_execution_result('"ok"', None, "unit-test")

    assert result["result"] == "ok"
    assert result["_ARTIFACTS"] is None
    assert result["attachments"] == []


def test_code_exec_param_defaults_include_attachments_system_output():
    module = _load_module()
    param = module.CodeExecParam()

    assert param.outputs["attachments"] == {"value": [], "type": "Array<String>"}


def test_process_execution_result_prefers_structured_result_metadata_over_stdout_guessing():
    tool = _build_code_exec("Object")

    result = tool._process_execution_result(
        '{"fake":"stdout-log"}',
        None,
        "unit-test",
        execution_metadata={
            "result_present": True,
            "result_value": {"real": "value"},
            "result_type": "json",
        },
    )

    assert result["result"] == {"real": "value"}
    assert result["actual_type"] == "Object"
    assert result["content"] == '{\n  "real": "value"\n}'


def test_process_execution_result_preserves_json_looking_string_when_metadata_marks_string():
    tool = _build_code_exec("String")

    result = tool._process_execution_result(
        '{"a":1}',
        None,
        "unit-test",
        execution_metadata={
            "result_present": True,
            "result_value": '{"a":1}',
            "result_type": "json",
        },
    )

    assert result["result"] == '{"a":1}'
    assert result["actual_type"] == "String"
    assert result["content"] == '{"a":1}'
