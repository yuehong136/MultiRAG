from typing import Any

from fastapi.params import Depends
from pymilvus import CollectionSchema, DataType
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name
from api.db.services.llm_service import LLMBundle
from api.service.semantic_layer_service.models import OwnerType, SemanticElementType, SemanticTextData
from common import settings
from common.misc_utils import thread_pool_exec


class TextEmbeddingService:
    COLLECTION_NAME = "semantic_layer_elements"

    def __init__(self, db: Session, user: Any):
        self.db = db
        self.user = user
        self.vector_database = settings.docStoreConn

    async def save_semantic_text_to_embedding(self, semantic_data: SemanticTextData):
        """将单条文本转为向量并保存到数据库"""
        await self.save_semantic_texts_to_embedding_batch([semantic_data])

    async def save_semantic_texts_to_embedding_batch(self, semantic_data_list: list[SemanticTextData]):
        """批量将多个文本转为向量并保存到数据库"""
        if not semantic_data_list:
            return

        # 确保集合存在
        embedding_model_name = semantic_data_list[0].embedding_model
        if not self.vector_database.has_collection(collection_name=self.COLLECTION_NAME):
            await self._create_collection(embedding_model=embedding_model_name)

        # 批量生成嵌入向量
        texts = [item.text for item in semantic_data_list]
        embedding_model_config = get_model_config_by_type_and_name(self.db, self.user.id, "embedding", embedding_model_name)
        embedding_model = LLMBundle(self.db, self.user.id, embedding_model_config)

        # 使用asyncio.to_thread将同步encode方法转为异步
        vectors, _ = await thread_pool_exec(embedding_model.encode, texts)

        # 组装批量插入数据
        batch_data = []
        for i, item in enumerate(semantic_data_list):
            insert_data = {
                "element_type": item.element_type,
                "original_id": item.original_id,
                "element_id": item.element_type + "_" + item.original_id,
                "original_text": item.text,
                "model_id": item.model_id,
                "dataset_id": item.dataset_id,
                "theme_domain_id": item.theme_domain_id,
                "vector": vectors[i],
            }
            batch_data.append(insert_data)

        # 批量插入到向量数据库
        # 如果vector_database.insert是耗时操作，也可以考虑使用asyncio.to_thread包装
        await thread_pool_exec(self.vector_database.insert, collection_name=self.COLLECTION_NAME, data=batch_data)

    async def search_similar_vectors(
        self,
        query_text: str,
        embedding_model: str,
        element_types: list[SemanticElementType] | None = None,
        theme_domain_ids: list[str] | None = None,
        dataset_ids: list[str] | None = None,
        model_ids: list[str] | None = None,
        top_k: int = 10,
        score_threshold: float = 0.6,
    ) -> list[dict[str, Any]]:
        """
        根据文本查询相似的向量数据，支持组合条件过滤

        Args:
            query_text (str): 查询文本
            embedding_model (str): 使用的嵌入模型
            element_types (List[SemanticElementType], optional): 元素类型列表，用于过滤结果
            theme_domain_ids (List[str], optional): 主题域ID列表，用于过滤结果
            dataset_ids (List[str], optional): 数据集ID列表，用于过滤结果
            model_ids (List[str], optional): 模型ID列表，用于过滤结果
            top_k (int): 返回的最大结果数量，默认为10
            score_threshold (float): 相似度阈值，低于该值的结果将被过滤，默认为0.6

        Returns:
            List[Dict[str, Any]]: 查询结果列表，每个结果包含匹配项的详细信息和相似度分数

        Raises:
            RuntimeError: 当集合不存在时
        """
        # 检查集合是否存在
        has_collection = await thread_pool_exec(self.vector_database.has_collection, collection_name=self.COLLECTION_NAME)
        if not has_collection:
            raise RuntimeError(f"Collection {self.COLLECTION_NAME} does not exist")

        # 生成查询向量
        embedding_model_config = get_model_config_by_type_and_name(self.db, self.user.id, "embedding", embedding_model)
        embedding_model_instance = LLMBundle(self.db, self.user.id, embedding_model_config)
        # 使用asyncio.to_thread将同步encode方法转为异步
        query_vector, _ = await thread_pool_exec(embedding_model_instance.encode, [query_text])

        # 构建过滤条件
        filter_conditions = []

        # 添加元素类型过滤条件
        if element_types and len(element_types) > 0:
            element_type_values = [f"'{et.value}'" for et in element_types]
            element_type_condition = f"element_type in [{', '.join(element_type_values)}]"
            filter_conditions.append(element_type_condition)

        # 添加主题域ID过滤条件
        if theme_domain_ids and len(theme_domain_ids) > 0:
            theme_domain_values = [f"'{tid}'" for tid in theme_domain_ids]
            theme_domain_condition = f"theme_domain_id in [{', '.join(theme_domain_values)}]"
            filter_conditions.append(theme_domain_condition)

        # 添加数据集ID过滤条件
        if dataset_ids and len(dataset_ids) > 0:
            dataset_values = [f"'{did}'" for did in dataset_ids]
            dataset_condition = f"dataset_id in [{', '.join(dataset_values)}]"
            filter_conditions.append(dataset_condition)

        # 添加模型ID过滤条件
        if model_ids and len(model_ids) > 0:
            model_values = [f"'{mid}'" for mid in model_ids]
            model_condition = f"model_id in [{', '.join(model_values)}]"
            filter_conditions.append(model_condition)

        # 组合所有过滤条件
        filter_expr = " and ".join(filter_conditions) if filter_conditions else ""

        # 执行向量搜索
        search_params = {
            "metric_type": "COSINE",  # 使用余弦相似度
            "params": {"nprobe": 16},  # 搜索参数，可根据性能需求调整
        }

        # 使用asyncio.to_thread将search_by_milvus方法转为异步
        results = await thread_pool_exec(
            self.vector_database.search_by_milvus,
            collection_name=self.COLLECTION_NAME,
            data=[query_vector[0]],
            anns_field="vector",
            filter=filter_expr if filter_expr else None,
            output_fields=["element_type", "original_id", "element_id", "original_text", "model_id", "dataset_id", "theme_domain_id"],
            limit=top_k,
            search_params=search_params,
        )

        # 处理搜索结果
        processed_results = []
        for hit in results[0]:
            if hit.get("distance", 0) < score_threshold:
                continue

            result_item = {
                "score": hit.get("distance"),
                "original_text": hit.get("entity").get("original_text"),
                "element_type": hit.get("entity").get("element_type"),
                "original_id": hit.get("entity").get("original_id"),
                "element_id": hit.get("entity").get("element_id"),
                "model_id": hit.get("entity").get("model_id"),
                "dataset_id": hit.get("entity").get("dataset_id"),
                "theme_domain_id": hit.get("entity").get("theme_domain_id"),
            }
            processed_results.append(result_item)

        return processed_results

    async def delete_by_owner_type_and_id(self, owner_type: OwnerType, original_id: str):
        """删除指定类型和ID相关的数据

        Args:
            owner_type (str): 实体类型，必须是 'MODEL', 'DATASET' 或 'THEME_DOMAIN'
            original_id (str): 实体ID

        Raises:
            ValueError: 当实体类型不是预期的值时
            RuntimeError: 当集合不存在时
        """
        # 检查集合是否存在
        has_collection = await thread_pool_exec(self.vector_database.has_collection, collection_name=self.COLLECTION_NAME)
        if not has_collection:
            raise RuntimeError(f"Collection {self.COLLECTION_NAME} does not exist")

        # 映射实体类型到对应的字段名
        field_mapping = {OwnerType.MODEL: "model_id", OwnerType.DATASET: "dataset_id", OwnerType.THEME_DOMAIN: "theme_domain_id"}

        field_name = field_mapping[owner_type]
        # 使用asyncio.to_thread将delete方法转为异步
        result = await thread_pool_exec(self.vector_database.delete, collection_name=self.COLLECTION_NAME, filter=f"{field_name} == '{original_id}'")

        return result

    async def delete_by_element_type_and_id(self, element_type: SemanticElementType, original_id: str):
        """删除指定元素类型和ID的向量数据

        Args:
            element_type (SemanticElementType): 元素类型，必须是 SemanticElementType 枚举中的一个值
            original_id (str): 元素ID

        Returns:
            dict: 删除操作的结果

        Raises:
            RuntimeError: 当集合不存在时
        """
        # 检查集合是否存在
        has_collection = await thread_pool_exec(self.vector_database.has_collection, collection_name=self.COLLECTION_NAME)
        if not has_collection:
            raise RuntimeError(f"Collection {self.COLLECTION_NAME} does not exist")

        # 构建 element_id 格式为: element_type + "_" + id
        element_id = f"{element_type.value}_{original_id}"

        # 使用asyncio.to_thread将delete方法转为异步
        result = await thread_pool_exec(self.vector_database.delete, collection_name=self.COLLECTION_NAME, filter=f"element_id == '{element_id}'")

        return result

    async def _get_embedding_model_dim(self, embedding_model: str) -> int:
        """获取嵌入模型的向量维度"""
        embedding_model_config = get_model_config_by_type_and_name(self.db, self.user.id, "embedding", embedding_model)
        embedding_model_instance = LLMBundle(self.db, self.user.id, embedding_model_config)
        # 使用asyncio.to_thread将encode方法转为异步
        sample_vec, _ = await thread_pool_exec(embedding_model_instance.encode, ["测试"])
        vector_dim = len(sample_vec[0])
        return vector_dim

    async def _create_collection(self, embedding_model: str):
        """创建Milvus集合

        Args:
            embedding_model (str): 嵌入模型的名称，用于确定向量维度
        """
        schema = await self._create_schema(embedding_model)
        index_params = self._create_index()
        dimension = await self._get_embedding_model_dim(embedding_model)

        # 使用asyncio.to_thread将create_collection方法转为异步
        await thread_pool_exec(self.vector_database.create_collection, collection_name=self.COLLECTION_NAME, dimension=dimension, schema=schema, index_params=index_params)

    async def _create_schema(self, embedding_model: str) -> CollectionSchema:
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
            field_name="original_text",
            datatype=DataType.VARCHAR,
            max_length=1024,
        )
        schema.add_field(
            field_name="element_type",
            datatype=DataType.VARCHAR,
            max_length=256,
        )
        schema.add_field(
            field_name="original_id",
            datatype=DataType.VARCHAR,
            max_length=256,
        )
        schema.add_field(
            field_name="element_id",  # element_type + id 为唯一标识
            datatype=DataType.VARCHAR,
            max_length=256,
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
        schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=await self._get_embedding_model_dim(embedding_model))
        return schema

    def _create_index(self):
        """创建集合的索引参数"""
        index_params = self.vector_database.prepare_index_params()
        index_params.add_index(field_name="vector", metric_type="COSINE", index_type="IVF_FLAT", index_name="vector_index", param={"nlist": 1024})
        index_params.add_index(field_name="element_type", index_type="BITMAP", index_name="element_type_index")
        index_params.add_index(field_name="original_id", index_type="INVERTED", index_name="original_id_index")
        index_params.add_index(field_name="element_id", index_type="INVERTED", index_name="element_id_index")
        index_params.add_index(field_name="model_id", index_type="INVERTED", index_name="model_id_index")
        index_params.add_index(field_name="dataset_id", index_type="INVERTED", index_name="dataset_id_index")
        index_params.add_index(field_name="theme_domain_id", index_type="INVERTED", index_name="theme_domain_id_index")
        return index_params


async def get_text_embedding_service(db: Session = Depends(get_db), user=Depends(manager)) -> TextEmbeddingService:
    """依赖注入获取TextEmbeddingService实例"""
    return TextEmbeddingService(db, user)
