"""Public REST pagination bounds and unambiguous integer query values."""

import re
from typing import Annotated, Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from pydantic import BeforeValidator

REST_API_MAX_PAGE_SIZE = 100


def validate_rest_api_page_size(value: Any) -> int:
    """Preserve explicit zero where allowed; reject coercion and oversize values."""
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= REST_API_MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be an integer between 0 and {REST_API_MAX_PAGE_SIZE}")
    return value


RestPageSize = Annotated[int, BeforeValidator(validate_rest_api_page_size)]


def validate_rest_api_pagination(request: Request) -> None:
    """Do not let Starlette silently choose the last repeated pagination value."""
    for name in ("page", "page_size"):
        values = request.query_params.getlist(name)
        if len(values) > 1:
            raise RequestValidationError([{"type": "value_error", "loc": ("query", name), "msg": f"{name} must be supplied only once", "input": values}])
        if name == "page" and values and not re.fullmatch(r"[0-9]+", values[0]):
            raise RequestValidationError([{"type": "value_error", "loc": ("query", name), "msg": "page must be a non-negative integer", "input": values[0]}])
        if name == "page_size" and values:
            try:
                validate_rest_api_page_size(values[0])
            except ValueError as error:
                raise RequestValidationError([{"type": "value_error", "loc": ("query", name), "msg": str(error), "input": values[0]}]) from error
