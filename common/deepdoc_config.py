"""Typed DeepDOC options with the application's normal configuration precedence."""

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, PositiveInt, ValidationError, field_validator

from common.app_config import AppConfigError, get_app_config


class DeepdocConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    page_batch_size: PositiveInt = 50
    dla_url: str = ""

    @field_validator("dla_url", mode="before")
    @classmethod
    def blank_remote_url(cls, value: Any) -> Any:
        return "" if value is None else value


def get_deepdoc_config() -> DeepdocConfig:
    raw_section = get_app_config().get_section("deepdoc")
    if raw_section is not None and not isinstance(raw_section, dict):
        raise AppConfigError("deepdoc configuration must be an object")
    section = dict(raw_section or {})
    # Canonical section values (including explicit blanks) win over old names.
    section.setdefault("page_batch_size", os.getenv("PDF_PARSER_PAGE_BATCH_SIZE", "50"))
    section.setdefault("dla_url", os.getenv("DEEPDOC_URL") or os.getenv("TENSORRT_DLA_SVR") or "")
    try:
        return DeepdocConfig.model_validate(section)
    except ValidationError as exc:
        raise AppConfigError("Invalid deepdoc configuration: page_batch_size must be positive and dla_url must be a string") from exc
