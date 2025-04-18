"""
Shared data models for semantic layer services
"""
from typing import Optional


class SemanticTextData:
    def __init__(
            self, text: str, element_type: str, element_id: str, embedding_model: str,
            model_id: str | None = None, dataset_id: str | None = None, theme_domain_id: str | None = None
    ):
        self.text = text
        self.element_type = element_type
        self.element_id = element_id
        self.embedding_model = embedding_model
        self.model_id = model_id
        self.dataset_id = dataset_id
        self.theme_domain_id = theme_domain_id
