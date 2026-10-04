"""Dataset metadata definitions shared by validation and extraction.

Keep the stored representation lossless. Only the extraction projection turns
legacy names, examples and UI types into JSON Schema.
"""

from copy import deepcopy
from math import isfinite
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, StringConstraints, model_serializer, model_validator

MetadataType = Literal["string", "list", "time", "number"]
MetadataKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


def numeric_values(values: list[Any]) -> list[int | float]:
    """Accept numeric UI strings without creating a number/string enum mismatch."""
    result: list[int | float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)) or (isinstance(value, str) and not value.strip()):
            raise ValueError("number metadata values must be finite numbers")
        if isinstance(value, int):
            result.append(value)
            continue
        number = float(value)
        if not isfinite(number):
            raise ValueError("number metadata values must be finite numbers")
        result.append(int(number) if number.is_integer() else number)
    return result


def validate_parser_metadata(config: dict[str, Any] | None) -> None:
    """Validate submitted field lists without validating historical snapshots."""
    if not config:
        return
    for key in ("metadata", "built_in_metadata"):
        if isinstance(config.get(key), list):
            for field in config[key]:
                MetadataField.model_validate(field)


class MetadataField(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, allow_inf_nan=False)

    key: MetadataKey | None = None
    name: MetadataKey | None = None
    type: MetadataType | None = None
    description: str | None = None
    enum: list[str | int | float] | None = None
    examples: list[str | int | float] | None = None
    restrict_values: bool = False

    @model_serializer(mode="wrap")
    def serialize_explicit(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return {key: value for key, value in handler(self).items() if key in self.model_fields_set or key not in type(self).model_fields}

    @model_validator(mode="after")
    def validate_definition(self) -> "MetadataField":
        if not (self.key or self.name):
            raise ValueError("metadata field requires key or name")
        if self.key and self.name and self.key != self.name:
            raise ValueError("metadata key and name conflict")
        for values in (self.enum, self.examples):
            if values is None:
                continue
            if self.type == "number":
                numeric_values(values)
            elif any(not isinstance(value, str) for value in values):
                raise ValueError("non-number metadata values must be strings")
        if self.enum is not None and self.restrict_values and self.examples is not None and self.enum != self.examples:
            raise ValueError("metadata enum and restricted examples conflict")
        return self


class MetadataConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    metadata: list[MetadataField] | dict[str, Any] | None = Field(default=None, exclude_if=lambda value: value is None)
    built_in_metadata: list[MetadataField] | None = Field(default=None, exclude_if=lambda value: value is None)
    enabled: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    fields: list[MetadataField] | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_config(self) -> "MetadataConfig":
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("metadata configuration fields cannot be null; use [] to clear")
        if self.metadata is not None and self.fields is not None:
            canonical = [canonical_field(field.model_dump(exclude_unset=True)) for field in self.fields]
            other = [canonical_field(field.model_dump(exclude_unset=True)) for field in self.metadata] if isinstance(self.metadata, list) else self.metadata
            if canonical != other:
                raise ValueError("metadata and fields conflict")
        if isinstance(self.metadata, dict) and self.metadata and (self.metadata.get("type", "object") != "object" or not isinstance(self.metadata.get("properties"), dict)):
            raise ValueError("metadata schema must have object properties")
        return self


def canonical_field(field: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(field)
    if "key" not in result and "name" in result:
        result["key"] = result.pop("name")
    if result.get("restrict_values") and result.get("examples") is not None and "enum" not in result:
        result["enum"] = deepcopy(result["examples"])
    return result


def apply_metadata_config(stored: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """New envelopes patch explicit fields; old envelopes retain their defaults."""
    MetadataConfig.model_validate(config)
    result = deepcopy(stored)
    canonical = "metadata" in config or "built_in_metadata" in config
    if canonical:
        for key in ("metadata", "built_in_metadata"):
            if key in config:
                result[key] = deepcopy(config[key])
        if "fields" in config and "metadata" not in config:
            result["metadata"] = deepcopy(config["fields"])
        if "enabled" in config:
            result["enable_metadata"] = config["enabled"]
    else:
        result["metadata"] = deepcopy(config.get("fields", []))
        result["enable_metadata"] = config.get("enabled", True)
    return result


def metadata_config_view(stored: dict[str, Any]) -> dict[str, Any]:
    metadata = deepcopy(stored.get("metadata") or [])
    if isinstance(metadata, list):
        metadata = [canonical_field(field) for field in metadata if isinstance(field, dict)]
    return {"metadata": metadata, "built_in_metadata": deepcopy(stored.get("built_in_metadata") or [])}


def field_schema(field: dict[str, Any]) -> dict[str, Any]:
    field = canonical_field(field)
    result: dict[str, Any] = {"description": field.get("description") or field.get("descriptions") or ""}
    kind = field.get("type")
    values = field.get("enum")
    if kind == "list":
        result.update(type="array", items={"type": "string"})
        if values:
            result["items"]["enum"] = deepcopy(values)
    else:
        if kind:
            result["type"] = "string" if kind == "time" else kind
        if values:
            result["enum"] = numeric_values(values) if kind == "number" else deepcopy(values)
            result.setdefault("type", "string")
    if field.get("examples") is not None and not field.get("restrict_values"):
        examples = field["examples"]
        result["examples"] = numeric_values(examples) if kind == "number" else [[value] for value in examples] if kind == "list" else deepcopy(examples)
    return result
