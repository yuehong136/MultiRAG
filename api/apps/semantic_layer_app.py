from enum import Enum, auto

from fastapi import APIRouter, Depends, Body
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from api.db.db_models import get_db
from api.apps import manager

from api.service.semantic_layer_service.text_embedding_service import TextEmbeddingService, get_text_embedding_service
from api.service.semantic_layer_service.models import SemanticTextData

router = APIRouter()


class Type(Enum):
    THEME_DOMAIN = auto()  # 主题域
    THEME_DOMAIN_EN = auto()  # 主题域英文

    DATASET = auto()  # 数据集
    DATASET_EN = auto()  # 数据集英文
    DATASET_DESC = auto()  # 数据集描述

    MODEL = auto()  # 模型
    MODEL_EN = auto()  # 模型英文
    MODEL_DESC = auto()  # 模型描述

    METRIC = auto()  # 指标
    METRIC_EN = auto()  # 指标英文
    METRIC_DESC = auto()  # 指标描述
    METRIC_SYNONYMS = auto()  # 指标同义词

    DIMENSION = auto()  # 维度
    DIMENSION_EN = auto()  # 维度英文
    DIMENSION_DESC = auto()  # 维度描述
    DIMENSION_SYNONYMS = auto()  # 维度同义词

    DIMENSION_VALUE = auto()  # 维度值
    DIMENSION_VALUE_SYNONYMS = auto()  # 维度值同义词

    TERM = auto()  # 术语
    TERM_DESC = auto()  # 术语描述
    TERM_SYNONYMS = auto()  # 术语同义词


class ReqBody(BaseModel):
    text: str = Field(
        ...,
        title="文本内容",
        description="需要转换为向量的文本内容",
    )
    type: str = Field(
        ...,
        title="文本类型",
        description="文本的类型，对应 Type 枚举中的类型名称",
    )
    id: str = Field(
        ...,
        title="ID",
        description="在中台表中的ID",
    )
    model_id: str | None = Field(
        None,
        title="模型id",
        description="所属模型id（当为指标、维度相关信息保存向量时需要填写）",
    )
    dataset_id: str | None = Field(
        None,
        title="数据集id",
        description="所属数据集id（当为指标、维度相关信息保存向量时需要填写）",
    )
    theme_domain_id: str | None = Field(
        None,
        title="主题域id",
        description="所属主题域id（当为指标、维度相关信息保存向量时需要填写）",
    )
    embedding_model: str = Field(
        ...,
        title="嵌入模型",
        description="嵌入模型的名称",
    )

    class Config:
        schema_extra = {
            "example": {
                "text": "这是一个数据集描述的示例文本，该数据集包含了2010-2020年的气象数据。",
                "type": "DATASET_DESC",
                "id": "12345",
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2"
            }
        }


@router.post("/save-text-to-embedding", summary="将文本转为向量并保存")
async def save_text_to_embedding(
        body: ReqBody = Body(
            ...,
            title="文本信息",
            description="需要转换为向量的文本信息",
            example={
                "text": "这是一个术语描述的示例文本，该术语代表某个特定的概念。",
                "type": "TERM_DESC",
                "id": "term_67890",
                "model_id": "12345",
                "dataset_id": "12345",
                "theme_domain_id": "12345",
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2"
            }
        ),
        db: Session = Depends(get_db),
        user=Depends(manager),
        service: TextEmbeddingService = Depends(get_text_embedding_service)
):
    semantic_data = SemanticTextData(
        text=body.text,
        element_type=body.type,
        element_id=body.id,
        embedding_model=body.embedding_model,
        model_id=body.model_id,
        dataset_id=body.dataset_id,
        theme_domain_id=body.theme_domain_id
    )
    await service.save_semantic_text_to_embedding(semantic_data=semantic_data)
    return {"status": "success", "data": "text-to-embedding"}
