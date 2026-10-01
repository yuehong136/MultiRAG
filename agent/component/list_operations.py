import os
from abc import ABC
from copy import deepcopy
from typing import Any

from agent.component.base import ComponentBase, ComponentParamBase
from api.utils.api_utils import timeout


class ListOperationsParam(ComponentParamBase):
    """
    Define the List Operations component parameters.
    """

    def __init__(self) -> None:
        super().__init__()
        self.query = ""
        self.operations = "nth"
        self.operations_version = 2
        self.n = 0
        self.strict = False
        self.sort_method = "asc"
        self.filter = {"operator": "=", "value": ""}
        self.outputs = {"result": {"value": [], "type": "Array of ?"}, "first": {"value": "", "type": "?"}, "last": {"value": "", "type": "?"}}

    def update(self, conf: dict[str, Any], allow_redundant: bool = False) -> "ListOperationsParam":
        # Historical DSL has no version marker, including when operations was
        # omitted. Keep its defaults/semantics when constructing a runtime copy.
        config = deepcopy(conf)
        config.setdefault("operations_version", 1)
        config.setdefault("operations", "topN" if config["operations_version"] == 1 else "nth")
        super().update(config, allow_redundant=allow_redundant)
        return self

    def check(self) -> None:
        self.check_empty(self.query, "query")
        if type(self.operations_version) is not int or self.operations_version not in (1, 2):
            raise ValueError("operations_version must be 1 (legacy) or 2.")
        operation = "" if self.operations is None else str(self.operations).strip()
        if operation.lower() == "topn":
            operation = "topN" if self.operations_version == 1 else "head"
        if self.operations_version == 2:
            operation = operation or "nth"
        self.operations = operation
        self.check_valid_value(self.operations, "Support operations", ["topN" if self.operations_version == 1 else "nth", "head", "tail", "filter", "sort", "drop_duplicates"])

    def get_input_form(self) -> dict[str, dict]:
        return {}


class ListOperations(ComponentBase, ABC):
    component_name = "ListOperations"
    _param: ListOperationsParam

    @timeout(int(os.environ.get("COMPONENT_EXEC_TIMEOUT", 10 * 60)))
    def _invoke(self, **kwargs: Any) -> None:
        # invoke() records failures as _ERROR. Never retain a prior successful
        # list or a prior error across repeated invocations of this component.
        self._set_outputs([])
        self._param.outputs.pop("_ERROR", None)
        self.input_objects = []
        inputs = getattr(self._param, "query", None)
        self.inputs = []
        self.inputs = self._canvas.get_variable_value(inputs)
        if self.inputs is None:
            self.inputs = []
        elif not isinstance(self.inputs, list):
            raise TypeError("The input of List Operations should be an array.")
        self.set_input_value(inputs, self.inputs)
        if self._param.operations == "topN":
            self._head()
        elif self._param.operations == "nth":
            self._nth()
        elif self._param.operations == "head":
            if self._param.operations_version == 1:
                self._legacy_nth(from_end=False)
            else:
                self._head()
        elif self._param.operations == "tail":
            if self._param.operations_version == 1:
                self._legacy_nth(from_end=True)
            else:
                self._tail()
        elif self._param.operations == "filter":
            self._filter()
        elif self._param.operations == "sort":
            self._sort()
        elif self._param.operations == "drop_duplicates":
            self._drop_duplicates()

    def _coerce_n(self) -> int:
        try:
            return int(getattr(self._param, "n", 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def _is_strict(self) -> bool:
        if self._param.operations_version == 1:
            return False
        strict = self._param.strict
        if isinstance(strict, str):
            return strict.strip().lower() in {"1", "true", "yes", "on"}
        return bool(strict)

    def _set_outputs(self, outputs: list[Any]) -> None:
        self.set_output("result", outputs)
        self.set_output("first", outputs[0] if outputs else None)
        self.set_output("last", outputs[-1] if outputs else None)

    def _raise_strict_range_error(self, operation: str, n: int) -> None:
        raise ValueError(f"{operation} requires n to be within the valid range in strict mode, got {n}.")

    def _nth(self) -> None:
        n = self._coerce_n()
        if n != 0 and abs(n) <= len(self.inputs):
            self._set_outputs([self.inputs[n - 1 if n > 0 else n]])
        elif self._is_strict():
            self._raise_strict_range_error("nth", n)

    def _head(self) -> None:
        n = self._coerce_n()
        if self._is_strict() and not 1 <= n <= len(self.inputs):
            self._raise_strict_range_error("head", n)
        self._set_outputs(self.inputs[:n] if n >= 1 else [])

    def _tail(self) -> None:
        n = self._coerce_n()
        if self._is_strict() and not 1 <= n <= len(self.inputs):
            self._raise_strict_range_error("tail", n)
        self._set_outputs(self.inputs[-n:] if n >= 1 else [])

    def _legacy_nth(self, *, from_end: bool) -> None:
        n = self._coerce_n()
        if 1 <= n <= len(self.inputs):
            outputs = [self.inputs[-n if from_end else n - 1]]
        else:
            outputs = []
        self._set_outputs(outputs)

    def _filter(self) -> None:
        self._set_outputs([i for i in self.inputs if self._eval(self._norm(i), self._param.filter["operator"], self._param.filter["value"])])

    def _norm(self, v: Any) -> str:
        s = "" if v is None else str(v)
        return s

    def _eval(self, v: str, operator: str, value: Any) -> bool:
        if operator == "=":
            return v == value
        elif operator == "≠":
            return v != value
        elif operator == "contains":
            return value in v
        elif operator == "start with":
            return v.startswith(value)
        elif operator == "end with":
            return v.endswith(value)
        else:
            return False

    def _sort(self) -> None:
        items = self.inputs or []
        method = getattr(self._param, "sort_method", "asc") or "asc"
        reverse = method == "desc"

        if not items:
            self._set_outputs([])
            return

        first = items[0]

        if isinstance(first, dict):
            outputs = sorted(
                items,
                key=lambda x: self._hashable(x),
                reverse=reverse,
            )
        else:
            outputs = sorted(items, reverse=reverse)

        self._set_outputs(outputs)

    def _drop_duplicates(self) -> None:
        seen = set()
        outs = []
        for item in self.inputs:
            k = self._hashable(item)
            if k in seen:
                continue
            seen.add(k)
            outs.append(item)
        self._set_outputs(outs)

    def _hashable(self, x: Any) -> Any:
        if isinstance(x, dict):
            return tuple(sorted((k, self._hashable(v)) for k, v in x.items()))
        if isinstance(x, (list, tuple)):
            return tuple(self._hashable(v) for v in x)
        if isinstance(x, set):
            return tuple(sorted(self._hashable(v) for v in x))
        return x

    def thoughts(self) -> str:
        return "ListOperation in progress"
