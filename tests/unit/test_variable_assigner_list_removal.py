import pytest

from agent.component.variable_assigner import VariableAssigner


@pytest.mark.parametrize("value", ["", {}, None, 0])
def test_list_removal_rejects_non_lists_even_when_empty(value) -> None:
    assert VariableAssigner._remove_first(None, value) == "ERROR:VARIABLE_NOT_LIST"
    assert VariableAssigner._remove_last(None, value) == "ERROR:VARIABLE_NOT_LIST"


def test_list_removal_handles_empty_and_populated_lists() -> None:
    assert VariableAssigner._remove_first(None, []) == []
    assert VariableAssigner._remove_last(None, []) == []
    assert VariableAssigner._remove_first(None, [1, 2, 3]) == [2, 3]
    assert VariableAssigner._remove_last(None, [1, 2, 3]) == [1, 2]
