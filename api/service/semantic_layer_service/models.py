"""
Shared data models for semantic layer services
"""

from enum import Enum


class SemanticTextData:
    def __init__(self, text: str, element_type: str, original_id: str, embedding_model: str, model_id: str | None = None, dataset_id: str | None = None, theme_domain_id: str | None = None):
        self.text = text
        self.element_type = element_type
        self.original_id = original_id
        self.embedding_model = embedding_model
        self.model_id = model_id
        self.dataset_id = dataset_id
        self.theme_domain_id = theme_domain_id


class SemanticElementType(str, Enum):
    THEME_DOMAIN = "THEME_DOMAIN"  # 主题域
    THEME_DOMAIN_EN = "THEME_DOMAIN_EN"  # 主题域英文

    DATASET = "DATASET"  # 数据集
    DATASET_EN = "DATASET_EN"  # 数据集英文
    DATASET_DESC = "DATASET_DESC"  # 数据集描述

    MODEL = "MODEL"  # 模型
    MODEL_EN = "MODEL_EN"  # 模型英文
    MODEL_DESC = "MODEL_DESC"  # 模型描述

    METRIC = "METRIC"  # 指标
    METRIC_EN = "METRIC_EN"  # 指标英文
    METRIC_DESC = "METRIC_DESC"  # 指标描述
    METRIC_SYNONYMS = "METRIC_SYNONYMS"  # 指标同义词

    DIMENSION = "DIMENSION"  # 维度
    DIMENSION_EN = "DIMENSION_EN"  # 维度英文
    DIMENSION_DESC = "DIMENSION_DESC"  # 维度描述
    DIMENSION_SYNONYMS = "DIMENSION_SYNONYMS"  # 维度同义词

    DIMENSION_VALUE = "DIMENSION_VALUE"  # 维度值
    DIMENSION_VALUE_SYNONYMS = "DIMENSION_VALUE_SYNONYMS"  # 维度值同义词

    TERM = "TERM"  # 术语
    TERM_DESC = "TERM_DESC"  # 术语描述
    TERM_SYNONYMS = "TERM_SYNONYMS"  # 术语同义词


class OwnerType(str, Enum):
    MODEL = "MODEL"
    DATASET = "DATASET"
    THEME_DOMAIN = "THEME_DOMAIN"
