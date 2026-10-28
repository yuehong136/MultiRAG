"""Public provider-instance inputs, independent of application initialization."""

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

ModelType = Literal["chat", "embedding", "rerank", "image2text", "speech2text", "tts", "ocr"]
ModelStatus = Literal["active", "inactive"]
PositiveModelID = Annotated[StrictInt, Field(gt=0, le=9223372036854775807)]
DEFAULT_MODEL_FIELDS = {"chat": "llm_id", "embedding": "embd_id", "rerank": "rerank_id", "image2text": "img2txt_id", "speech2text": "asr_id", "tts": "tts_id", "ocr": "ocr_id"}


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ProviderInput(ModelInput):
    provider_name: StrictStr = Field(min_length=1, max_length=128)


class ModelRegistration(ModelInput):
    model_name: StrictStr = Field(min_length=1, max_length=128)
    model_types: list[ModelType] = Field(min_length=1, max_length=7)
    max_tokens: StrictInt = Field(ge=0, le=2147483647)
    is_tools: StrictBool = False

    @model_validator(mode="after")
    def valid_model(self) -> "ModelRegistration":
        if self.model_name != self.model_name.strip() or "@" in self.model_name:
            raise ValueError("Invalid model name")
        if len(self.model_types) != len(set(self.model_types)):
            raise ValueError("Duplicate model capability")
        return self


class ConnectionCandidate(ModelInput):
    api_key: StrictStr = Field(repr=False)
    base_url: StrictStr = Field(default="", max_length=2048)
    region: StrictStr = Field(default="", max_length=128)
    model_ids: list[StrictStr] | None = None
    custom_model: ModelRegistration | None = None

    @field_validator("model_ids")
    @classmethod
    def distinct_models(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (len(value) != len(set(value)) or any(not name or name != name.strip() for name in value)):
            raise ValueError("Model names must be distinct and nonempty")
        return value

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if value != value.strip() or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or any(ord(char) < 32 for char in value):
            raise ValueError("Invalid base URL")
        return value


class InstanceInput(ConnectionCandidate):
    instance_name: StrictStr = Field(min_length=1, max_length=128)

    @field_validator("instance_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if value != value.strip() or not value or "@" in value or "/" in value:
            raise ValueError("Invalid instance name")
        return value


class InstancePatch(ModelInput):
    api_key: StrictStr | None = Field(default=None, repr=False)
    clear_api_key: StrictBool = False
    base_url: StrictStr | None = Field(default=None, max_length=2048)
    region: StrictStr | None = Field(default=None, max_length=128)
    status: ModelStatus | None = None

    @model_validator(mode="after")
    def valid_patch(self) -> "InstancePatch":
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        if self.clear_api_key and self.api_key:
            raise ValueError("Cannot replace and clear the API key together")
        if any(getattr(self, key) is None for key in self.model_fields_set - {"clear_api_key"}):
            raise ValueError("Omit unchanged fields instead of sending null")
        if self.base_url is not None:
            ConnectionCandidate.valid_url(self.base_url)
        return self


class StatusPatch(ModelInput):
    status: ModelStatus


class DefaultModelInput(ModelInput):
    model_type: ModelType
    model_id: PositiveModelID | None


def parse_model_id(value: str) -> int | None:
    """Numeric strings are identities; malformed numeric-looking values fail."""
    if re.fullmatch(r"[1-9][0-9]*", value):
        identifier = int(value)
        if identifier > 9223372036854775807:
            raise ValueError("Invalid model ID")
        return identifier
    if re.fullmatch(r"[+\-0-9.]+", value):
        raise ValueError("Invalid model ID")
    return None
