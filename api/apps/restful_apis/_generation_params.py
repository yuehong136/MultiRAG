"""Validated, request-local generation overrides shared by completion routes."""

from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from copy import deepcopy
from typing import Annotated, Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import AliasChoices, BaseModel, Field, StrictBool, model_validator

GENERATION_CONFIG_KEYS = ("temperature", "top_p", "frequency_penalty", "presence_penalty", "max_tokens")
GENERATION_CONFIG_ENABLED_KEYS = {
    "temperature": ("temperatureEnabled", "temperature_enabled"),
    "top_p": ("topPEnabled", "top_p_enabled"),
    "frequency_penalty": ("frequencyPenaltyEnabled", "frequency_penalty_enabled"),
    "presence_penalty": ("presencePenaltyEnabled", "presence_penalty_enabled"),
    "max_tokens": ("maxTokensEnabled", "max_tokens_enabled"),
}


class GenerationValidationRoute(APIRoute):
    """Reject malformed completion input without echoing non-JSON float values."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()
        if not self.path.endswith("/chat/completions"):
            return handler

        async def validate(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # FastAPI's default handler echoes input; NaN/Infinity in that
                # input makes JSONResponse raise instead of returning HTTP 422.
                errors = [{"loc": list(item["loc"]), "type": item["type"], "msg": item["msg"]} for item in exc.errors()]
                return JSONResponse(status_code=422, content={"detail": errors})

        return validate


class GenerationParameters(BaseModel):
    """Null/omitted values inherit saved settings; explicit zero is valid."""

    temperature: Annotated[float, Field(strict=True, ge=0, le=2, allow_inf_nan=False)] | None = None
    top_p: Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)] | None = None
    frequency_penalty: Annotated[float, Field(strict=True, ge=-2, le=2, allow_inf_nan=False)] | None = None
    presence_penalty: Annotated[float, Field(strict=True, ge=-2, le=2, allow_inf_nan=False)] | None = None
    max_tokens: Annotated[int, Field(strict=True, ge=0)] | None = None
    temperature_enabled: StrictBool | None = Field(None, validation_alias=AliasChoices("temperatureEnabled", "temperature_enabled"))
    top_p_enabled: StrictBool | None = Field(None, validation_alias=AliasChoices("topPEnabled", "top_p_enabled"))
    frequency_penalty_enabled: StrictBool | None = Field(None, validation_alias=AliasChoices("frequencyPenaltyEnabled", "frequency_penalty_enabled"))
    presence_penalty_enabled: StrictBool | None = Field(None, validation_alias=AliasChoices("presencePenaltyEnabled", "presence_penalty_enabled"))
    max_tokens_enabled: StrictBool | None = Field(None, validation_alias=AliasChoices("maxTokensEnabled", "max_tokens_enabled"))

    @model_validator(mode="before")
    @classmethod
    def validate_enable_flags(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        value = dict(value)
        for aliases in GENERATION_CONFIG_ENABLED_KEYS.values():
            for alias in aliases:
                flag = value.get(alias)
                if flag is not None and not isinstance(flag, bool):
                    raise ValueError(f"{alias} must be a boolean or null.")
            # Consume both spellings before Pydantic collects allowed extras.
            # Otherwise an unconsumed snake key can shadow the validated field.
            flags = [value.pop(alias) for alias in aliases if alias in value]
            if flags:
                value[aliases[1]] = False if False in flags else (True if True in flags else None)
        return value


def extract_generation_config(req: Mapping[str, Any]) -> dict[str, Any]:
    return {key: req[key] for key in GENERATION_CONFIG_KEYS if req.get(key) is not None and not any(req.get(flag) is False for flag in GENERATION_CONFIG_ENABLED_KEYS[key])}


def pop_generation_config(req: MutableMapping[str, Any]) -> dict[str, Any]:
    generation_config = extract_generation_config(req)
    for key in GENERATION_CONFIG_KEYS:
        req.pop(key, None)
        for flag in GENERATION_CONFIG_ENABLED_KEYS[key]:
            req.pop(flag, None)
    return generation_config


def merge_generation_config(dialog: Any, generation_config: Mapping[str, Any]) -> None:
    llm_setting = deepcopy(getattr(dialog, "llm_setting", None) or {})
    llm_setting.update(generation_config)
    dialog.llm_setting = llm_setting
