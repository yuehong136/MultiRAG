"""Strict document update input and stable, feature-local error material."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator

from api.utils.document_parser_config import DocumentParserConfigPatch


def _reject_null(value: Any) -> Any:
    if isinstance(value, dict) and any(item is None for item in value.values()):
        raise ValueError("Document update fields cannot be null.")
    return value


class DocumentUpdatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    name: Annotated[StrictStr | None, Field(min_length=1)] = None
    chunk_method: StrictStr | None = None
    pipeline_id: StrictStr | None = None
    parser_config: DocumentParserConfigPatch | None = None
    enabled: StrictBool | Annotated[int, Field(strict=True, ge=0, le=1)] | None = None
    meta_fields: dict[str, Any] | None = None
    chunk_count: Annotated[int | None, Field(strict=True, ge=0)] = None
    token_count: Annotated[int | None, Field(strict=True, ge=0)] = None
    progress: Annotated[float | None, Field(ge=0, le=1)] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null(cls, value: Any) -> Any:
        return _reject_null(value)

    @field_validator("meta_fields")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        import math

        if value is None:
            return value
        for item in value.values():
            items = item if isinstance(item, list) else [item]
            if any(type(element) not in {str, int, float, bool} or (isinstance(element, float) and not math.isfinite(element)) for element in items):
                raise ValueError("Document metadata must contain JSON scalars or scalar lists.")
        return value


class DocumentUpdateError(ValueError):
    """Safe explicit HTTP category and confirmed mutation outcome."""

    def __init__(
        self,
        message: str = "Document update failed.",
        *,
        status: int = 400,
        numeric_code: int = 102,
        code: str = "DOCUMENT_UPDATE_INVALID",
        outcome: Literal["unchanged", "unknown"] = "unchanged",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.numeric_code = numeric_code
        self.code = code
        self.outcome = outcome


class LegacyDocumentParserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    doc_id: StrictStr
    parser_id: StrictStr | None = None
    pipeline_id: StrictStr | None = None
    parser_config: DocumentParserConfigPatch | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null(cls, value: Any) -> Any:
        return _reject_null(value)

    def document_patch(self) -> DocumentUpdatePatch:
        fields = self.model_dump(exclude_unset=True, exclude={"doc_id"})
        if "parser_id" in fields:
            fields["chunk_method"] = fields.pop("parser_id")
        return DocumentUpdatePatch.model_validate(fields)
