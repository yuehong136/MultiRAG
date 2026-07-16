import pytest
from jinja2.exceptions import SecurityError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment

from core.prompts.generator import PROMPT_JINJA_ENV


def test_prompt_env_is_sandboxed() -> None:
    # Prompt templates can embed user-defined content (e.g. citation_guidelines),
    # so the shared environment must stay sandboxed to block SSTI.
    assert isinstance(PROMPT_JINJA_ENV, SandboxedEnvironment)


@pytest.mark.parametrize(
    "payload",
    [
        "{{ self.__class__.__mro__[1].__subclasses__() }}",
        "{{ ''.__class__.__mro__[1].__subclasses__() }}",
        "{{ request.__class__.__mro__[1].__subclasses__() }}",
        "{{ config.__class__.__init__.__globals__['os'] }}",
    ],
)
def test_ssti_payload_blocked(payload: str) -> None:
    template = PROMPT_JINJA_ENV.from_string(payload)
    with pytest.raises((SecurityError, AttributeError, UndefinedError)):
        template.render()


def test_safe_template_rendering() -> None:
    template = PROMPT_JINJA_ENV.from_string("Hello, {{ name }}!")
    assert template.render(name="World") == "Hello, World!"


def test_loop_rendering_still_works() -> None:
    template = PROMPT_JINJA_ENV.from_string("{% for item in items %}{{ item }}{% endfor %}")
    assert template.render(items=["a", "b", "c"]) == "abc"
