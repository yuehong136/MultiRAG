import logging
from typing import Any, List

from fastapi.params import Depends
from pymilvus import CollectionSchema, DataType
from sqlalchemy.orm import Session

from api.apps import manager
from api.db import LLMType
from api.db.db_models import get_db
from api.db.services.llm_service import LLMBundle
from api.settings import docStoreConn
from api.service.semantic_layer_service.models import SemanticTextData, OwnerType, SemanticElementType
from core.utils.milvus_conn import MilvusConnection


class TextEmbeddingService:
    COLLECTION_NAME = "semantic_layer_elements"

    def __init__(self, db: Session, user: Any):
        self.db = db
        self.user = user
        self.vector_database = docStoreConn

    async def save_semantic_text_to_embedding(self, semantic_data: SemanticTextData):
        """将单条文本转为向量并保存到数据库"""
        await self.save_semantic_texts_to_embedding_batch([semantic_data])

    async def save_semantic_texts_to_embedding_batch(self, semantic_data_list: List[SemanticTextData]):
        """批量将多个文本转为向量并保存到数据库"""
        if not semantic_data_list:
            return

        # 确保集合存在
        embedding_model_name = semantic_data_list[0].embedding_model
        if not self.vector_database.has_collection(collection_name=self.COLLECTION_NAME):
            self._create_collection(embedding_model=embedding_model_name)

        # 批量生成嵌入向量
        texts = [item.text for item in semantic_data_list]
        embedding_model = LLMBundle(self.db, self.user.id, LLMType.EMBEDDING, llm_name=embedding_model_name)
        vectors, _ = embedding_model.encode(texts)

        # 组装批量插入数据
        batch_data = []
        for i, item in enumerate(semantic_data_list):
            insert_data = {
                "element_type": item.element_type,
                "element_id": item.element_type + "_" + item.element_id,
                "original_text": item.text,
                "model_id": item.model_id,
                "dataset_id": item.dataset_id,
                "theme_domain_id": item.theme_domain_id,
                "vector": vectors[i]
            }
            batch_data.append(insert_data)

        # 批量插入到向量数据库
        self.vector_database.insert(collection_name=self.COLLECTION_NAME, data=batch_data)

    async def delete_by_owner_type_and_id(self, owner_type: OwnerType, id: str):
        """删除指定类型和ID相关的数据

        Args:
            owner_type (str): 实体类型，必须是 'MODEL', 'DATASET' 或 'THEME_DOMAIN'
            id (str): 实体ID

        Raises:
            ValueError: 当实体类型不是预期的值时
            RuntimeError: 当集合不存在时
        """
        # 检查集合是否存在
        if not self.vector_database.has_collection(collection_name=self.COLLECTION_NAME):
            raise RuntimeError(f"Collection {self.COLLECTION_NAME} does not exist")

        # 映射实体类型到对应的字段名
        field_mapping = {
            OwnerType.MODEL: "model_id",
            OwnerType.DATASET: "dataset_id",
            OwnerType.THEME_DOMAIN: "theme_domain_id"
        }

        field_name = field_mapping[owner_type]
        # 执行删除操作
        result = self.vector_database.delete(
            collection_name=self.COLLECTION_NAME,
            filter=f"{field_name} == '{id}'"
        )

        return result

    async def delete_by_element_type_and_id(self, element_type: SemanticElementType, id: str):
        """删除指定元素类型和ID的向量数据

        Args:
            element_type (SemanticElementType): 元素类型，必须是 SemanticElementType 枚举中的一个值
            id (str): 元素ID

        Returns:
            dict: 删除操作的结果

        Raises:
            RuntimeError: 当集合不存在时
        """
        # 检查集合是否存在
        if not self.vector_database.has_collection(collection_name=self.COLLECTION_NAME):
            raise RuntimeError(f"Collection {self.COLLECTION_NAME} does not exist")

        # 构建 element_id 格式为: element_type + "_" + id
        element_id = f"{element_type.value}_{id}"

        # 执行删除操作
        result = self.vector_database.delete(
            collection_name=self.COLLECTION_NAME,
            filter=f"element_id == '{element_id}'"
        )

        return result

    def _get_embedding_model_dim(self, embedding_model: str) -> int:
        """获取嵌入模型的向量维度"""
        embedding_model = LLMBundle(self.db, self.user.id, LLMType.EMBEDDING, llm_name=embedding_model)
        sample_vec, _ = embedding_model.encode(["测试"])
        vector_dim = len(sample_vec[0])
        return vector_dim

    def _create_collection(self, embedding_model: str):
        """创建Milvus集合

        Args:
            embedding_model (str): 嵌入模型的名称，用于确定向量维度
        """
        schema = self._create_schema(embedding_model)
        index_params = self._create_index()
        self.vector_database.create_collection(collection_name=self.COLLECTION_NAME,
                                               dimension=self._get_embedding_model_dim(embedding_model),
                                               schema=schema,
                                               index_params=index_params)

    def _create_schema(self, embedding_model: str) -> CollectionSchema:
        """创建集合的schema定义"""
        schema = self.vector_database.create_schema(enable_dynamic_field=True)
        schema.add_field(
            field_name="element_pk",
            datatype=DataType.VARCHAR,
            is_primary=True,
            auto_id=True,
            max_length=256,
        )
        schema.add_field(
            field_name="element_type",
            datatype=DataType.VARCHAR,
            max_length=256,
        )
        schema.add_field(
            field_name="element_id",  # element_type + id 为唯一标识
            datatype=DataType.VARCHAR,
            max_length=256,
        )
        schema.add_field(
            field_name="original_text",
            datatype=DataType.VARCHAR,
            max_length=1024,
        )
        schema.add_field(
            field_name="model_id",
            datatype=DataType.VARCHAR,
            nullable=True,
            max_length=256,
        )
        schema.add_field(
            field_name="dataset_id",
            datatype=DataType.VARCHAR,
            nullable=True,
            max_length=256,
        )
        schema.add_field(
            field_name="theme_domain_id",
            datatype=DataType.VARCHAR,
            nullable=True,
            max_length=256,
        )
        schema.add_field(
            field_name="vector",
            datatype=DataType.FLOAT_VECTOR,
            dim=self._get_embedding_model_dim(embedding_model)
        )
        return schema

    def _create_index(self):
        """创建集合的索引参数"""
        index_params = self.vector_database.prepare_index_params()
        index_params.add_index(field_name="vector",
                               metric_type="COSINE",
                               index_type="IVF_FLAT",
                               index_name="vector_index",
                               param={"nlist": 128})
        index_params.add_index(
            field_name="element_type",
            index_type="BITMAP",
            index_name="element_type_index"
        )
        index_params.add_index(
            field_name="element_id",
            index_type="INVERTED",
            index_name="element_id_index"
        )
        index_params.add_index(
            field_name="model_id",
            index_type="INVERTED",
            index_name="model_id_index"
        )
        index_params.add_index(
            field_name="dataset_id",
            index_type="INVERTED",
            index_name="dataset_id_index"
        )
        index_params.add_index(
            field_name="theme_domain_id",
            index_type="INVERTED",
            index_name="theme_domain_id_index"
        )
        return index_params


def get_text_embedding_service(db: Session = Depends(get_db), user=Depends(manager)) -> TextEmbeddingService:
    """依赖注入获取TextEmbeddingService实例"""
    return TextEmbeddingService(db, user)
