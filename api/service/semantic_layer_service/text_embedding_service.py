import logging
from typing import Any

from fastapi.params import Depends
from pymilvus import CollectionSchema, DataType
from sqlalchemy.orm import Session

from api.apps import manager
from api.db import LLMType
from api.db.db_models import get_db
from api.db.services.llm_service import LLMBundle
from api.settings import docStoreConn
from api.service.semantic_layer_service.models import SemanticTextData
from core.utils.milvus_conn import MilvusConnection


class TextEmbeddingService:
    COLLECTION_NAME = "semantic_layer_elements"

    def __init__(self, db: Session, user: Any):
        self.db = db
        self.user = user
        self.vector_database = docStoreConn

    async def save_semantic_text_to_embedding(self, semantic_data: SemanticTextData):
        """将文本转为向量并保存到数据库"""
        if not self.vector_database.has_collection(collection_name=self.COLLECTION_NAME):
            self._create_collection(embedding_model=semantic_data.embedding_model)

        insert_data = self._assemble_insert_data(semantic_data)
        self.vector_database.insert(collection_name=self.COLLECTION_NAME, data=insert_data)

    def _assemble_insert_data(self, semantic_data: SemanticTextData):
        embedding = self._generate_embedding(semantic_data.embedding_model, semantic_data.text)
        insert_data = {
            "element_type": semantic_data.element_type,
            "element_id": semantic_data.element_type + "_" + semantic_data.element_id,
            "original_text": semantic_data.text,
            "model_id": semantic_data.model_id,
            "dataset_id": semantic_data.dataset_id,
            "theme_domain_id": semantic_data.theme_domain_id,
            "vector": embedding
        }
        return insert_data

    def _generate_embedding(self, embedding_model: str, text: str):
        embedding_model = LLMBundle(self.db, self.user.id, LLMType.EMBEDDING, llm_name=embedding_model)
        vectors, _ = embedding_model.encode([text])
        return vectors[0]

    def _get_embedding_model_dim(self, embedding_model: str) -> int:
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
    return TextEmbeddingService(db, user)
