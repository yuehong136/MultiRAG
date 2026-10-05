"""Request contract shared by REST dataset search and its service."""

import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field, model_validator

from api.utils.reference_metadata import ReferenceMetadata


class SparseSearchMode(BaseModel):
    type: Literal["sparse"] = "sparse"


class DenseSearchMode(BaseModel):
    type: Literal["dense"] = "dense"


class HybridSearchMode(BaseModel):
    type: Literal["hybrid"] = "hybrid"
    weight_dense: float = Field(default=0.7, ge=0, le=1)
    weight_sparse: float = Field(default=0.3, ge=0, le=1)

    @model_validator(mode="after")
    def normalize_weights(self) -> "HybridSearchMode":
        total = self.weight_dense + self.weight_sparse
        if total == 0:
            raise ValueError("At least one search weight must be positive")
        if abs(total - 1) > 0.001:
            self.weight_dense /= total
            self.weight_sparse /= total
        return self


class FusionSearchMode(BaseModel):
    type: Literal["fusion"] = "fusion"
    weights: str = Field(default="0.05,0.95", description="Comma-separated sparse,dense weights; finite and nonnegative, with a positive sum. Normalized before retrieval.")

    @model_validator(mode="after")
    def validate_weights(self) -> "FusionSearchMode":
        parts = self.weights.split(",")
        if len(parts) != 2:
            raise ValueError("weights must contain exactly two comma-separated values")
        sparse, dense = (float(part.strip()) for part in parts)
        total = sparse + dense
        if not all(math.isfinite(value) and value >= 0 for value in (sparse, dense)) or not math.isfinite(total) or total <= 0:
            raise ValueError("weights must be finite, nonnegative and have a positive sum")
        self.weights = f"{sparse / total:g},{dense / total:g}"
        return self


SearchMode = Annotated[SparseSearchMode | DenseSearchMode | HybridSearchMode | FusionSearchMode, Discriminator("type")]


class SearchDatasetRequest(BaseModel):
    reference_metadata: ReferenceMetadata | None = None
    question: str = Field(min_length=1)
    # Local multi-dataset consumers retain one retrieval, ranking and pagination.
    # When supplied, the path dataset must be part of this complete selection.
    dataset_ids: list[str] | None = Field(default=None, min_length=1)
    doc_ids: list[str] | None = None
    page: int = Field(default=1, ge=1)
    size: int = Field(default=30, ge=1)
    top_k: int = Field(default=1024, ge=1)
    similarity_threshold: float = Field(default=0, ge=0, le=1)
    vector_similarity_weight: float = Field(default=0.3, ge=0, le=1)
    use_kg: bool = False
    rerank_id: str | None = None
    tenant_rerank_id: int | None = None
    highlight: bool = False
    keyword: bool = False
    search_mode: SearchMode | None = Field(
        default=None, description="Defaults to dense vector retrieval. Sparse uses full text; hybrid and fusion combine independent text and vector candidates with the selected weights."
    )
    cross_languages: list[str] | None = None
    search_id: str | None = None
    meta_data_filter: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_question(self) -> "SearchDatasetRequest":
        self.question = self.question.strip()
        if not self.question:
            raise ValueError("question cannot be blank")
        return self

    def get_search_mode_dict(self) -> dict[str, Any] | None:
        if self.search_mode is None:
            return None
        values = self.search_mode.model_dump()
        return {values.pop("type"): values}
