"""Strict document patches; stored historical snapshots are deliberately not DTOs.

Defaults only make omitted fields constructible. Always dump exclude_unset=True;
never use these defaults as a document's parser defaults. No resource access,
parser reset or scheduling belongs here.
"""

from collections.abc import Mapping
from copy import deepcopy
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, field_validator, model_validator

from common.metadata_config import MetadataField, MetadataType

NonNegativeInt = Annotated[int, Field(ge=0)]
NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
UnitFloat = Annotated[float, Field(ge=0, le=1)]


class _Patch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class DocumentRaptorPatch(_Patch):
    use_raptor: bool = False
    prompt: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] = "{cluster_content}"
    max_token: Annotated[int, Field(ge=1, le=2048)] = 256
    threshold: UnitFloat = 0.1
    max_cluster: Annotated[int, Field(ge=1, le=1024)] = 64
    random_seed: NonNegativeInt = 0
    auto_disable_for_structured_data: bool = True
    scope: Literal["file", "dataset"] = "file"


class DocumentGraphragPatch(_Patch):
    use_graphrag: bool = False
    entity_types: list[NonEmptyString] = Field(default_factory=list)
    method: Literal["light", "general"] = "light"
    community: bool = False
    resolution: bool = False


class DocumentParentChildPatch(_Patch):
    use_parent_child: bool = False
    children_delimiter: NonEmptyString = r"\n"


class DocumentMetadataField(_Patch):
    """Legacy extraction fields consumed by metadata_schema()."""

    key: NonEmptyString
    description: str = ""
    descriptions: str = ""
    enum: list[str | int | float] = Field(default_factory=list)
    type: MetadataType | None = None
    examples: list[str | int | float] | None = None
    restrict_values: bool = False

    @model_validator(mode="after")
    def validate_metadata_field(self) -> "DocumentMetadataField":
        MetadataField.model_validate(self.model_dump(exclude_unset=True))
        return self


MinerULanguage = Literal[
    "English",
    "Chinese",
    "Traditional Chinese",
    "Russian",
    "Ukrainian",
    "Indonesian",
    "Spanish",
    "Vietnamese",
    "Japanese",
    "Korean",
    "Portuguese BR",
    "German",
    "French",
    "Italian",
    "Tamil",
    "Telugu",
    "Kannada",
    "Thai",
    "Greek",
    "Hindi",
    "Bulgarian",
    "Turkish",
]


class DocumentParserConfigPatch(_Patch):
    """Incoming fields for documents, never a stored snapshot validator.

    The document token limit includes the existing 8192 graph default and UI.
    The dataset ParserConfig is unchanged. Overlap retains consumer dual units:
    (0,1) is a fraction, >=1 is percent.
    """

    auto_keywords: Annotated[int, Field(ge=0, le=32)] = 0
    auto_questions: Annotated[int, Field(ge=0, le=10)] = 0
    chunk_token_num: Annotated[int, Field(ge=1, le=8192)] = 512
    delimiter: NonEmptyString = r"\n"
    graphrag: DocumentGraphragPatch = Field(default_factory=DocumentGraphragPatch)
    html4excel: bool = False
    layout_recognize: NonEmptyString = "DeepDOC"
    parent_child: DocumentParentChildPatch = Field(default_factory=DocumentParentChildPatch)
    enable_children: bool = False
    children_delimiter: str = ""
    raptor: DocumentRaptorPatch = Field(default_factory=DocumentRaptorPatch)
    tag_kb_ids: list[NonEmptyString] = Field(default_factory=list)
    topn_tags: Annotated[int, Field(ge=1, le=10)] = 1
    filename_embd_weight: UnitFloat = 0.1
    task_page_size: Annotated[int, Field(ge=1)] = 12
    pages: list[list[Annotated[int, Field(ge=1)]]] = Field(default_factory=list)
    image_context_size: NonNegativeInt = 0
    table_context_size: NonNegativeInt = 0
    toc_extraction: bool = False
    overlapped_percent: Annotated[float, Field(ge=0, le=90)] = 0
    mineru_parse_method: Literal["auto", "txt", "ocr"] = "auto"
    mineru_formula_enable: bool = True
    mineru_table_enable: bool = True
    mineru_lang: MinerULanguage = "English"
    enable_metadata: bool = False
    metadata: dict[str, JsonValue] | list[DocumentMetadataField] = Field(default_factory=list)
    built_in_metadata: list[DocumentMetadataField] = Field(default_factory=list)
    llm_id: str = ""
    analyze_hyperlink: bool = False
    hyperlink_urls: bool = False
    video_prompt: str = ""

    @field_validator("pages")
    @classmethod
    def validate_pages(cls, value: list[list[int]]) -> list[list[int]]:
        if any(len(pair) != 2 or pair[0] >= pair[1] for pair in value):
            raise ValueError("pages must contain one-based [start, exclusive_stop] ranges")
        return value

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: object) -> object:
        if isinstance(value, dict) and (("type" in value and value["type"] != "object") or ("properties" in value and not isinstance(value["properties"], dict))):
            raise ValueError("metadata schema must have object properties")
        return value

    @model_validator(mode="after")
    def consistent_parent_child(self) -> "DocumentParserConfigPatch":
        pc_fields = self.parent_child.model_fields_set
        if "parent_child" in self.model_fields_set:
            if "use_parent_child" in pc_fields and "enable_children" in self.model_fields_set and self.parent_child.use_parent_child != self.enable_children:
                raise ValueError("parent_child and enable_children conflict")
            if "children_delimiter" in pc_fields and "children_delimiter" in self.model_fields_set and self.parent_child.children_delimiter != self.children_delimiter:
                raise ValueError("nested and flat children_delimiter conflict")
        return self


def _deep_merge(stored: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(stored))
    for key, value in patch.items():
        if isinstance(value, dict) and not value:
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def merge_document_parser_config(
    stored_config: Mapping[str, Any],
    incoming_patch: DocumentParserConfigPatch | Mapping[str, Any],
) -> dict[str, Any]:
    """Validate, merge submitted fields only, then sync child aliases.

    Empty nested patches are noops. Explicit child disable follows the existing
    flatten convention (parent_child={}, children_delimiter=''). All other
    nested config, including disabled RAPTOR/GraphRAG, retains old fields.
    """
    # Revalidate model_construct()/mutated models at this service boundary.
    raw = incoming_patch.model_dump(exclude_unset=True) if isinstance(incoming_patch, DocumentParserConfigPatch) else dict(incoming_patch)
    patch = DocumentParserConfigPatch.model_validate(raw).model_dump(exclude_unset=True)
    merged = _deep_merge(stored_config, patch)
    if isinstance(patch.get("metadata"), dict) and patch["metadata"]:
        schema = merged["metadata"]
        if schema.get("type", "object") != "object" or not isinstance(schema.get("properties"), dict):
            raise ValueError("merged metadata schema must have object properties")
        schema["type"] = "object"
    nested = patch.get("parent_child", {})
    if not nested and not ({"enable_children", "children_delimiter"} & patch.keys()):
        return merged
    old_pc = stored_config.get("parent_child")
    old_pc = old_pc if isinstance(old_pc, dict) else {}
    delimiter = nested.get("children_delimiter", patch.get("children_delimiter", old_pc.get("children_delimiter", stored_config.get("children_delimiter") or r"\n")))
    enabled = nested.get("use_parent_child", patch.get("enable_children", old_pc.get("use_parent_child", stored_config.get("enable_children", bool(delimiter)))))
    if "children_delimiter" in patch and not patch["children_delimiter"] and nested.get("use_parent_child", patch.get("enable_children")) is not False:
        raise ValueError("empty children_delimiter requires explicit parent-child disable")
    if enabled and not delimiter:
        raise ValueError("enabled parent-child parsing requires a children_delimiter")
    merged["enable_children"] = enabled
    merged["children_delimiter"] = delimiter if enabled else ""
    if enabled:
        pc = merged.get("parent_child")
        merged["parent_child"] = {**(pc if isinstance(pc, dict) else {}), "use_parent_child": True, "children_delimiter": delimiter}
    else:
        merged["parent_child"] = {}
    return merged
