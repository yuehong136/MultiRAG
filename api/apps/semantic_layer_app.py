from enum import Enum, auto
from typing import List, Optional

from fastapi import APIRouter, Depends, Body
from pydantic import BaseModel, Field, validator
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


class TextItemBase(BaseModel):
    """文本转向量的基础模型"""
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
    model_id: Optional[str] = Field(
        None,
        title="模型id",
        description="所属模型id（当为指标、维度相关信息保存向量时需要填写）",
    )
    dataset_id: Optional[str] = Field(
        None,
        title="数据集id",
        description="所属数据集id（当为指标、维度相关信息保存向量时需要填写）",
    )
    theme_domain_id: Optional[str] = Field(
        None,
        title="主题域id",
        description="所属主题域id（当为指标、维度相关信息保存向量时需要填写）",
    )
    embedding_model: str = Field(
        ...,
        title="嵌入模型",
        description="嵌入模型的名称",
    )

    @validator('type')
    def validate_type(cls, v):
        """验证type是否为Type枚举中的有效值"""
        try:
            Type[v]
            return v
        except KeyError:
            valid_types = [t.name for t in Type]
            raise ValueError(f"无效的类型: {v}. 有效类型: {', '.join(valid_types)}")

    class Config:
        schema_extra = {
            "example": {
                "text": "这是一个数据集描述的示例文本，该数据集包含了2010-2020年的气象数据。",
                "type": "DATASET_DESC",
                "id": "12345",
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2"
            }
        }


class BatchTextItem(BaseModel):
    """批量处理的请求模型"""
    items: List[TextItemBase] = Field(
        ...,
        title="批量文本信息",
        description="需要批量转换为向量的多条文本信息",
        min_items=1,
    )

    class Config:
        schema_extra = {
            "example": {
                "items": [
                    {
                        "text": "这是第一个示例文本，描述了某个数据集的内容",
                        "type": "DATASET_DESC",
                        "id": "12345",
                        "embedding_model": "sentence-transformers/all-MiniLM-L6-v2"
                    },
                    {
                        "text": "这是第二个示例文本，描述了某个术语的含义",
                        "type": "TERM_DESC",
                        "id": "67890",
                        "embedding_model": "sentence-transformers/all-MiniLM-L6-v2"
                    }
                ]
            }
        }


@router.post("/save-text-to-embedding", summary="将单条文本转为向量并保存")
async def save_text_to_embedding(
        body: TextItemBase = Body(
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
    """保存单条文本到向量数据库"""
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
    return {"status": "success", "message": "文本已成功转换为向量并保存"}


@router.post("/save-texts-to-embedding-batch", summary="批量将多个文本转为向量并保存")
async def save_texts_to_embedding_batch(
        body: List[TextItemBase] = Body(
            ...,
            title="批量文本信息",
            description="需要批量转换为向量的多条文本信息",
            example=[
                {
                    "text": "这是第一个示例文本",
                    "type": "DATASET_DESC",
                    "id": "12345",
                    "embedding_model": "sentence-transformers/all-MiniLM-L6-v2"
                },
                {
                    "text": "这是第二个示例文本",
                    "type": "TERM_DESC",
                    "id": "67890",
                    "embedding_model": "sentence-transformers/all-MiniLM-L6-v2"
                }
            ]
        ),
        db: Session = Depends(get_db),
        user=Depends(manager),
        service: TextEmbeddingService = Depends(get_text_embedding_service)
):
    """批量保存多条文本到向量数据库"""
    semantic_data_list = [
        SemanticTextData(
            text=item.text,
            element_type=item.type,
            element_id=item.id,
            embedding_model=item.embedding_model,
            model_id=item.model_id,
            dataset_id=item.dataset_id,
            theme_domain_id=item.theme_domain_id
        ) for item in body
    ]

    await service.save_semantic_texts_to_embedding_batch(semantic_data_list=semantic_data_list)
    return {
        "status": "success",
        "message": f"批量文本转向量处理成功，共处理 {len(semantic_data_list)} 条数据"
    }
