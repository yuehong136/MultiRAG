import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock


def _load_invoke_module(monkeypatch):
    repo_root = Path(__file__).resolve().parents[2]

    pandas = ModuleType("pandas")
    pandas.DataFrame = type("DataFrame", (), {})
    monkeypatch.setitem(sys.modules, "pandas", pandas)

    deepdoc = ModuleType("deepdoc")
    deepdoc.__path__ = []
    monkeypatch.setitem(sys.modules, "deepdoc", deepdoc)
    deepdoc_parser = ModuleType("deepdoc.parser")
    deepdoc_parser.HtmlParser = MagicMock
    monkeypatch.setitem(sys.modules, "deepdoc.parser", deepdoc_parser)

    common_pkg = ModuleType("common")
    common_pkg.__path__ = [str(repo_root / "common")]
    monkeypatch.setitem(sys.modules, "common", common_pkg)

    connection_utils = ModuleType("common.connection_utils")
    connection_utils.timeout = lambda _seconds: lambda fn: fn
    monkeypatch.setitem(sys.modules, "common.connection_utils", connection_utils)

    misc_utils = ModuleType("common.misc_utils")
    misc_utils.thread_pool_exec = lambda fn, **kwargs: fn(**kwargs)
    monkeypatch.setitem(sys.modules, "common.misc_utils", misc_utils)

    agent_pkg = ModuleType("agent")
    agent_pkg.__path__ = [str(repo_root / "agent")]
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)

    agent_settings = ModuleType("agent.settings")
    agent_settings.FLOAT_ZERO = 1e-8
    agent_settings.PARAM_MAXDEPTH = 5
    monkeypatch.setitem(sys.modules, "agent.settings", agent_settings)

    component_pkg = ModuleType("agent.component")
    component_pkg.__path__ = [str(repo_root / "agent" / "component")]
    monkeypatch.setitem(sys.modules, "agent.component", component_pkg)

    base_spec = importlib.util.spec_from_file_location(
        "agent.component.base",
        repo_root / "agent" / "component" / "base.py",
    )
    base_module = importlib.util.module_from_spec(base_spec)
    monkeypatch.setitem(sys.modules, "agent.component.base", base_module)
    base_spec.loader.exec_module(base_module)

    invoke_spec = importlib.util.spec_from_file_location(
        "agent.component.invoke",
        repo_root / "agent" / "component" / "invoke.py",
    )
    invoke_module = importlib.util.module_from_spec(invoke_spec)
    monkeypatch.setitem(sys.modules, "agent.component.invoke", invoke_module)
    invoke_spec.loader.exec_module(invoke_module)

    return invoke_module


def _make_invoke(module, *, variables=None, variable_values=None, datatype="formdata"):
    variable_values = variable_values or {}

    canvas = MagicMock()
    canvas.get_variable_value = MagicMock(side_effect=lambda key: variable_values.get(key, ""))
    canvas.is_canceled = MagicMock(return_value=False)

    param = module.InvokeParam.__new__(module.InvokeParam)
    param.url = "http://example.com/token"
    param.method = "post"
    param.headers = ""
    param.variables = variables or []
    param.proxy = ""
    param.timeout = 60
    param.clean_html = False
    param.datatype = datatype
    param.max_retries = 0
    param.delay_after_error = 0
    param.outputs = {}
    param.inputs = {}

    invoke = module.Invoke.__new__(module.Invoke)
    invoke._canvas = canvas
    invoke._param = param
    invoke._id = "invoke_test"

    return invoke


def test_invoke_input_form_exposes_variable_refs(monkeypatch):
    module = _load_invoke_module(monkeypatch)
    invoke = _make_invoke(
        module,
        variables=[
            {"key": "username", "ref": "begin@username", "value": ""},
            {"key": "login", "ref": "begin@username", "value": ""},
            {"key": "literal", "ref": "", "value": "fixed"},
        ],
    )
    invoke.get_input_elements_from_text = lambda _text: {"begin@username": {"name": "Username"}}

    assert invoke.get_input_form() == {"begin@username": {"type": "line", "name": "Username"}}


def test_invoke_uses_runtime_kwargs_for_ref_variables(monkeypatch):
    module = _load_invoke_module(monkeypatch)
    invoke = _make_invoke(
        module,
        variables=[
            {"key": "grant_type", "ref": "", "value": "password"},
            {"key": "username", "ref": "begin@username", "value": ""},
            {"key": "password", "ref": "begin@password", "value": ""},
        ],
    )
    mock_post = MagicMock(return_value=SimpleNamespace(text="ok"))
    monkeypatch.setattr(module.requests, "post", mock_post)

    invoke._invoke(**{"begin@username": "admin@datav.com", "begin@password": "admin"})

    assert mock_post.call_args[1]["data"] == {
        "grant_type": "password",
        "username": "admin@datav.com",
        "password": "admin",
    }
    assert invoke._param.inputs["begin@username"]["value"] == "admin@datav.com"
    assert invoke._param.inputs["begin@password"]["value"] == "admin"
    invoke._canvas.get_variable_value.assert_not_called()


def test_invoke_falls_back_to_canvas_for_ref_variables(monkeypatch):
    module = _load_invoke_module(monkeypatch)
    invoke = _make_invoke(
        module,
        variables=[{"key": "username", "ref": "begin@username", "value": ""}],
        variable_values={"begin@username": "from-canvas"},
    )
    mock_post = MagicMock(return_value=SimpleNamespace(text="ok"))
    monkeypatch.setattr(module.requests, "post", mock_post)

    invoke._invoke()

    assert mock_post.call_args[1]["data"] == {"username": "from-canvas"}
    assert invoke._param.inputs["begin@username"]["value"] == "from-canvas"


def test_invoke_coerces_stringified_json_arguments(monkeypatch):
    module = _load_invoke_module(monkeypatch)
    invoke = _make_invoke(
        module,
        variables=[
            {"key": "payload", "ref": "begin@payload", "value": ""},
            {"key": "enabled", "ref": "", "value": False},
        ],
        datatype="json",
    )
    mock_post = MagicMock(return_value=SimpleNamespace(text="ok"))
    monkeypatch.setattr(module.requests, "post", mock_post)

    invoke._invoke(**{"begin@payload": '{"items": [1, 2]}'})

    assert mock_post.call_args[1]["json"] == {
        "payload": {"items": [1, 2]},
        "enabled": False,
    }
    assert invoke._param.inputs["begin@payload"]["value"] == {"items": [1, 2]}


def test_invoke_keeps_non_json_strings_in_json_mode(monkeypatch):
    module = _load_invoke_module(monkeypatch)
    invoke = _make_invoke(
        module,
        variables=[{"key": "message", "ref": "", "value": "plain text"}],
        datatype="json",
    )
    mock_post = MagicMock(return_value=SimpleNamespace(text="ok"))
    monkeypatch.setattr(module.requests, "post", mock_post)

    invoke._invoke()

    assert mock_post.call_args[1]["json"] == {"message": "plain text"}
