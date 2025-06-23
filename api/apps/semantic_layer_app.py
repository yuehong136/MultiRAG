from enum import Enum
from typing import List, Optional, Any, Dict

from fastapi import APIRouter, Depends, Body, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from api.db.db_models import get_db
from api.apps import manager

from api.service.semantic_layer_service.text_embedding_service import TextEmbeddingService, get_text_embedding_service
from api.service.semantic_layer_service.models import SemanticTextData, SemanticElementType, OwnerType

router = APIRouter()


class StatusEnum(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class ResponseSchema(BaseModel):
    status: StatusEnum = StatusEnum.SUCCESS
    message: str | None = None
    data: Any | None = None


class DeleteByOwnerTypeRequest(BaseModel):
    """删除特定类型实体相关向量的请求模型"""
    owner_type: OwnerType = Field(
        ...,
        title="实体类型",
        description="要删除的实体类型，必须是 MODEL, DATASET 或 THEME_DOMAIN"
    )
    original_id: str = Field(
        ...,
        title="实体ID",
        description="MODEL, DATASET 或 THEME_DOMAIN 的ID"
    )

    class Config:
        schema_extra = {
            "example": {
                "type": "MODEL",
                "id": "model_12345"
            }
        }


class DeleteByElementTypeRequest(BaseModel):
    """删除特定元素类型和ID的向量数据请求模型"""
    element_type: SemanticElementType = Field(
        ...,
        title="元素类型",
        description="要删除的元素类型，对应SemanticElementType枚举中的类型"
    )
    original_id: str = Field(
        ...,
        title="元素在中台表中的ID",
        description="要删除的元素ID"
    )

    class Config:
        schema_extra = {
            "example": {
                "element_type": "METRIC",
                "id": "12345"
            }
        }


class TextItemBase(BaseModel):
    """文本转向量的基础模型"""
    text: str = Field(
        ...,
        title="文本内容",
        description="需要转换为向量的文本内容",
    )
    type: SemanticElementType = Field(
        ...,
        title="文本类型",
        description="文本的类型，对应 SemanticElementType 枚举中的类型名称",
    )
    original_id: str = Field(
        ...,
        title="original_id",
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
                        "original_id": "12345",
                        "embedding_model": "sentence-transformers/all-MiniLM-L6-v2"
                    },
                    {
                        "text": "这是第二个示例文本，描述了某个术语的含义",
                        "type": "TERM_DESC",
                        "original_id": "67890",
                        "embedding_model": "sentence-transformers/all-MiniLM-L6-v2"
                    }
                ]
            }
        }


class SearchVectorsRequest(BaseModel):
    """向量搜索请求模型"""
    query_text: str = Field(
        ...,
        title="查询文本",
        description="用于查询相似向量的文本内容"
    )
    embedding_model: str = Field(
        ...,
        title="嵌入模型",
        description="用于生成查询向量的嵌入模型名称"
    )
    element_types: Optional[List[SemanticElementType]] = Field(
        None,
        title="元素类型列表",
        description="用于过滤的元素类型列表，可选"
    )
    theme_domain_ids: Optional[List[str]] = Field(
        None,
        title="主题域ID列表",
        description="用于过滤的主题域ID列表，可选"
    )
    dataset_ids: Optional[List[str]] = Field(
        None,
        title="数据集ID列表",
        description="用于过滤的数据集ID列表，可选"
    )
    model_ids: Optional[List[str]] = Field(
        None,
        title="模型ID列表",
        description="用于过滤的模型ID列表，可选"
    )
    top_k: int = Field(
        10,
        title="返回结果数量",
        description="返回的最大结果数量",
        ge=1,
        le=100
    )
    score_threshold: float = Field(
        0.82,
        title="相似度阈值",
        description="相似度分数阈值，低于该值的结果将被过滤",
        ge=0,
        le=1
    )

    class Config:
        schema_extra = {
            "example": {
                "query_text": "气象数据分析",
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
                "element_types": ["DATASET_DESC", "MODEL_DESC"],
                "theme_domain_ids": ["theme_domain_123"],
                "top_k": 5,
                "score_threshold": 0.7
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
                "original_id": "term_67890",
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
    try:
        semantic_data = SemanticTextData(
            text=body.text,
            element_type=body.type,
            original_id=body.original_id,
            embedding_model=body.embedding_model,
            model_id=body.model_id,
            dataset_id=body.dataset_id,
            theme_domain_id=body.theme_domain_id
        )
        await service.save_semantic_text_to_embedding(semantic_data=semantic_data)
        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message="文本已成功转换为向量并保存"
        )
    except Exception as e:
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"文本转向量保存失败: {str(e)}"
        )


@router.post("/delete-embeddings-by-owner-type-and-id", summary="删除特定类型实体相关的向量数据")
async def delete_embeddings_by_owner_type_and_id(
        body: DeleteByOwnerTypeRequest = Body(
            ...,
            title="删除请求信息",
            description="指定要删除的实体类型和ID",
            example={
                "type": "MODEL",
                "original_id": "12345"
            }
        ),
        db: Session = Depends(get_db),
        user=Depends(manager),
        service: TextEmbeddingService = Depends(get_text_embedding_service)
):
    """删除与特定实体相关的所有向量数据

    Args:
        body: 包含实体类型和ID的请求体
        db: 数据库会话
        user: 当前用户
        service: 文本嵌入服务

    Returns:
        包含删除操作状态和结果信息的响应

    Raises:
        HTTPException: 当删除操作失败时
    """
    try:
        result = await service.delete_by_owner_type_and_id(owner_type=body.owner_type, original_id=body.original_id)
        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message=f"成功删除 {body.owner_type}_ID={body.original_id} 的向量数据，删除数量: {result.get('delete_count', 0)}"
        )
    except (ValueError, RuntimeError) as e:
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=str(e)
        )
    except Exception as e:
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"删除向量数据时发生未知错误: {str(e)}"
        )


@router.post("/delete-embeddings-by-element-type-and-id", summary="删除特定元素类型和ID的向量数据")
async def delete_embeddings_by_element_type_and_id(
        body: DeleteByElementTypeRequest = Body(
            ...,
            title="删除请求信息",
            description="指定要删除的元素类型和ID",
            example={
                "element_type": "METRIC",
                "original_id": "12345"
            }
        ),
        db: Session = Depends(get_db),
        user=Depends(manager),
        service: TextEmbeddingService = Depends(get_text_embedding_service)
):
    """删除特定元素类型和ID的向量数据

    Args:
        body: 包含元素类型和ID的请求体
        db: 数据库会话
        user: 当前用户
        service: 文本嵌入服务

    Returns:
        包含删除操作状态和结果信息的响应

    Raises:
        HTTPException: 当删除操作失败时
    """
    try:
        result = await service.delete_by_element_type_and_id(element_type=body.element_type,
                                                             original_id=body.original_id)
        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message=f"成功删除元素类型 {body.element_type} ID={body.original_id} 的向量数据，删除数量: {result.get('delete_count', 0)}"
        )
    except (ValueError, RuntimeError) as e:
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=str(e)
        )
    except Exception as e:
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"删除向量数据时发生未知错误: {str(e)}"
        )


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
                    "original_id": "12345",
                    "embedding_model": "sentence-transformers/all-MiniLM-L6-v2"
                },
                {
                    "text": "这是第二个示例文本",
                    "type": "TERM_DESC",
                    "original_id": "67890",
                    "embedding_model": "sentence-transformers/all-MiniLM-L6-v2"
                }
            ]
        ),
        db: Session = Depends(get_db),
        user=Depends(manager),
        service: TextEmbeddingService = Depends(get_text_embedding_service)
):
    """批量保存多条文本到向量数据库"""
    try:
        semantic_data_list = [
            SemanticTextData(
                text=item.text,
                element_type=item.type,
                original_id=item.original_id,
                embedding_model=item.embedding_model,
                model_id=item.model_id,
                dataset_id=item.dataset_id,
                theme_domain_id=item.theme_domain_id
            ) for item in body
        ]

        await service.save_semantic_texts_to_embedding_batch(semantic_data_list=semantic_data_list)
        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message=f"批量文本转向量处理成功，共处理 {len(semantic_data_list)} 条数据"
        )
    except Exception as e:
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"批量文本转向量处理失败: {str(e)}"
        )


@router.post("/search-similar-vectors", summary="根据文本查询相似的向量数据")
async def search_similar_vectors(
        body: SearchVectorsRequest = Body(
            ...,
            title="查询请求信息",
            description="用于查询相似向量的文本和过滤条件"
        ),
        db: Session = Depends(get_db),
        user=Depends(manager),
        service: TextEmbeddingService = Depends(get_text_embedding_service)
):
    """
    根据文本查询相似的向量数据，支持组合条件过滤

    Args:
        body: 包含查询文本和过滤条件的请求体
        db: 数据库会话
        user: 当前用户
        service: 文本嵌入服务

    Returns:
        包含查询结果的响应

    Raises:
        HTTPException: 当查询操作失败时
    """
    try:
        results = await service.search_similar_vectors(
            query_text=body.query_text,
            embedding_model=body.embedding_model,
            element_types=body.element_types,
            theme_domain_ids=body.theme_domain_ids,
            dataset_ids=body.dataset_ids,
            model_ids=body.model_ids,
            top_k=body.top_k,
            score_threshold=body.score_threshold
        )

        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message=f"查询成功，找到 {len(results)} 条相似结果",
            data=results
        )
    except RuntimeError as e:
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=str(e)
        )
    except Exception as e:
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"查询相似向量时发生未知错误: {str(e)}"
        )
