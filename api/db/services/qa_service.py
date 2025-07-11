"""
教师科研考核问答系统 - 核心服务实现
支持QA模板存储和混合检索（密集向量+BM25）
"""
import json
import logging
import re
import uuid
from typing import Any
from datetime import datetime
from pymilvus import FieldSchema, DataType, CollectionSchema, Function, FunctionType, AnnSearchRequest, WeightedRanker
from pymilvus.client.constants import DEFAULT_CONSISTENCY_LEVEL

from sqlalchemy.orm import Session
from api.db.services.llm_service import LLMBundle
from api.db import LLMType
from api import settings
from core.utils import get_float

logger = logging.getLogger(__name__)

# 固定的QA模板集合名称
QA_TEMPLATE_COLLECTION = "bl_qa_template"

# ================================
# 1. 模板存储服务
# ================================

class QATemplateStorageService:
    """QA模板存储服务"""

    def __init__(self):
        self.collection_name = QA_TEMPLATE_COLLECTION

    def _ensure_collection_exists(self, dim):
        """确保QA模板集合存在"""
        try:
            if settings.docStoreConn.has_collection(self.collection_name):
                return True, "集合已存在"

            analyzer_params = {
                "type": "chinese",
            }
            # 定义字段schema
            fields = [
                # 主键字段
                FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=100, is_primary=True),
                # QA模板字段
                FieldSchema(name="qa_id", dtype=DataType.VARCHAR, max_length=200),
                FieldSchema(name="question_canonical", dtype=DataType.VARCHAR, max_length=2000, enable_analyzer=True, analyzer_params=analyzer_params),
                FieldSchema(name="paraphrases", dtype=DataType.VARCHAR, max_length=8000),  # JSON字符串存储
                FieldSchema(name="needed_params", dtype=DataType.VARCHAR, max_length=2000),  # JSON字符串存储
                FieldSchema(name="sql_template", dtype=DataType.VARCHAR, max_length=8000),
                FieldSchema(name="rule_id", dtype=DataType.VARCHAR, max_length=200),
                FieldSchema(name="tenant_id", dtype=DataType.VARCHAR, max_length=100),
                # 向量字段
                FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=dim),  # 密集向量
                FieldSchema(name="sparse_vector", dtype=DataType.SPARSE_FLOAT_VECTOR),  # 稀疏向量(BM25)
                # 时间戳字段
                FieldSchema(name="create_timestamp", dtype=DataType.FLOAT),
                FieldSchema(name="update_timestamp", dtype=DataType.FLOAT),
            ]

            # 创建BM25函数
            bm25_function_name = f"bm25_function_{str(uuid.uuid4())[:8]}"
            bm25_function = Function(
                name=bm25_function_name,
                function_type=FunctionType.BM25,
                input_field_names=["question_canonical"],
                output_field_names="sparse_vector"
            )

            # 创建集合架构
            description = f"QA模板集合（支持混合检索）- 创建于 {datetime.now().isoformat()}"
            schema = CollectionSchema(
                fields=fields,
                description=description,
                enable_dynamic_field=True
            )
            schema.add_function(bm25_function)

            # 创建集合
            conn = settings.docStoreConn._get_connection()
            conn.create_collection(
                self.collection_name,
                schema,
                consistency_level=DEFAULT_CONSISTENCY_LEVEL
            )

            # 为密集向量创建索引
            dense_index_params = {
                "metric_type": "COSINE",
                "index_type": "IVF_FLAT",
                "params": {"nlist": 128}
            }
            conn.create_index(self.collection_name, "vector", dense_index_params)

            # 为稀疏向量创建索引
            sparse_index_params = {
                "metric_type": "BM25",
                "index_type": "SPARSE_INVERTED_INDEX",
                "params": {
                    "inverted_index_algo": "DAAT_WAND",
                    # DAAT_MAXSCORE (默认)：适合多、长topk, DAAT_WAND:适合少、短topk, TAAT_NAIVE：不推荐
                    # 配置BM25参数
                    "bm25_k1": 1.5,  # 控制术语频率饱和度，范围[1.2, 2.0]
                    "bm25_b": 0.75  # 控制文档长度归一化，范围[0, 1]
                }
            }
            conn.create_index(self.collection_name, "sparse_vector", sparse_index_params)

            # 加载集合
            conn.load_collection(self.collection_name)

            logger.info(f"QA模板集合 {self.collection_name} 创建成功")
            return True, "QA模板集合创建成功"

        except Exception as e:
            logger.error(f"创建QA模板集合失败: {e}")
            return False, f"创建集合失败: {str(e)}"

    def store_templates(
            self,
            db: Session,
            templates: list[dict[str, Any]],
            tenant_id: str
    ) -> dict[str, Any]:
        """存储QA模板到Milvus"""
        try:
            # 获取嵌入模型
            embedding_model = LLMBundle(db, tenant_id, LLMType.EMBEDDING.value)

            # 准备要向量化的文本
            texts_to_embed = []
            for template in templates:
                # 收集标准问法
                texts_to_embed.append(template['question_canonical'])
                # # 收集同义句
                # for paraphrase in template.get('paraphrases', []):
                #     texts_to_embed.append(paraphrase)

            # 批量向量化
            embeddings, _ = embedding_model.encode(texts_to_embed)

            # 确保集合存在
            success, message = self._ensure_collection_exists(len(embeddings[0]))
            if not success:
                return {"success": False, "message": message}

            # 准备插入数据
            insert_data = []
            embedding_idx = 0
            current_timestamp = datetime.now().timestamp()

            for template in templates:
                # 为标准问法创建记录
                canonical_embedding = embeddings[embedding_idx]
                embedding_idx += 1

                record = {
                    "id": f"{tenant_id}_{template['qa_id']}_canonical",
                    "qa_id": template['qa_id'],
                    "question_canonical": template['question_canonical'],
                    "paraphrases": json.dumps(template.get('paraphrases', []), ensure_ascii=False),
                    "needed_params": json.dumps(template['needed_params'], ensure_ascii=False),
                    "sql_template": template['sql_template'],
                    "rule_id": str(template['rule_id']) if template.get('rule_id') is not None else "",
                    "tenant_id": tenant_id,
                    "vector": canonical_embedding.tolist(),
                    "create_timestamp": current_timestamp,
                    "update_timestamp": current_timestamp
                }
                insert_data.append(record)

                # # 为同义句创建记录
                # for i, paraphrase in enumerate(template.get('paraphrases', [])):
                #     paraphrase_embedding = embeddings[embedding_idx]
                #     embedding_idx += 1
                #
                #     paraphrase_record = {
                #         "id": f"{tenant_id}_{template['qa_id']}_para_{i}",
                #         "qa_id": template['qa_id'],
                #         "question_canonical": paraphrase,  # 同义句也放在question_canonical字段用于BM25
                #         "paraphrases": json.dumps(template.get('paraphrases', []), ensure_ascii=False),
                #         "needed_params": json.dumps(template['needed_params'], ensure_ascii=False),
                #         "sql_template": template['sql_template'],
                #         "rule_id": template.get('rule_id', ''),
                #         "tenant_id": tenant_id,
                #         "vector": paraphrase_embedding.tolist(),
                #         "create_timestamp": current_timestamp,
                #         "update_timestamp": current_timestamp
                #     }
                #     insert_data.append(paraphrase_record)

            # 使用docStoreConn的insert方法
            doc_store_result = settings.docStoreConn.insert(self.collection_name, insert_data)

            # 检查 insert_count 是否与本批次长度一致
            if doc_store_result.get("insert_count", 0) != len(insert_data):
                error_message = (
                    f"Insert count mismatch: expected {len(insert_data)}, "
                    f"got {doc_store_result.get('insert_count', 0)}."
                )
                raise Exception(error_message)

            return {
                "success": True,
                "message": f"成功存储 {len(templates)} 个QA模板",
                "template_count": len(templates),
                "record_count": len(insert_data)
            }

        except Exception as e:
            logger.error(f"存储QA模板失败: {e}")
            return {
                "success": False,
                "message": f"存储模板失败: {str(e)}"
            }

    def store_templates_v2(
            self,
            db: Session,
            templates: list[dict[str, Any]],
            tenant_id: str
    ) -> dict[str, Any]:
        """存储QA模板V2到Milvus - 支持类型化参数"""
        try:
            # 获取嵌入模型
            embedding_model = LLMBundle(db, tenant_id, LLMType.EMBEDDING.value)

            # 准备要向量化的文本
            texts_to_embed = []
            for template in templates:
                # 收集标准问法
                texts_to_embed.append(template['question_canonical'])

            # 批量向量化
            embeddings, _ = embedding_model.encode(texts_to_embed)

            # 确保集合存在（需要支持额外的字段）
            success, message = self._ensure_collection_v2_exists(len(embeddings[0]))
            if not success:
                return {"success": False, "message": message}

            # 准备插入数据
            insert_data = []
            embedding_idx = 0
            current_timestamp = datetime.now().timestamp()

            for template in templates:
                # 为标准问法创建记录
                canonical_embedding = embeddings[embedding_idx]
                embedding_idx += 1

                # 处理 sql_template：如果是数组则序列化为JSON，如果是字符串则保持兼容性
                sql_template_value = template['sql_template']
                if isinstance(sql_template_value, list):
                    sql_template_json = json.dumps(sql_template_value, ensure_ascii=False)
                else:
                    # 向后兼容：如果传入的是字符串，转换为单元素数组
                    sql_template_json = json.dumps([sql_template_value], ensure_ascii=False)

                record = {
                    "id": f"{tenant_id}_{template['qa_id']}_canonical",
                    "qa_id": template['qa_id'],
                    "question_canonical": template['question_canonical'],
                    "paraphrases": json.dumps(template.get('paraphrases', []), ensure_ascii=False),
                    "needed_params": json.dumps(template['needed_params'], ensure_ascii=False),
                    "needed_params_typed": template.get('needed_params_typed', '[]'),  # V2新增字段
                    "sql_template": sql_template_json,  # 存储为JSON字符串
                    "rule_id": str(template['rule_id']) if template.get('rule_id') is not None else "",
                    "tenant_id": tenant_id,
                    "vector": canonical_embedding.tolist(),
                    "create_timestamp": current_timestamp,
                    "update_timestamp": current_timestamp,
                    "template_version": "v2"  # V2版本标记
                }
                insert_data.append(record)

            # 使用docStoreConn的insert方法
            doc_store_result = settings.docStoreConn.insert(self.collection_name, insert_data)

            # 检查 insert_count 是否与本批次长度一致
            if doc_store_result.get("insert_count", 0) != len(insert_data):
                error_message = (
                    f"Insert count mismatch: expected {len(insert_data)}, "
                    f"got {doc_store_result.get('insert_count', 0)}."
                )
                raise Exception(error_message)

            return {
                "success": True,
                "message": f"成功存储 {len(templates)} 个QA模板V2（支持类型化参数）",
                "template_count": len(templates),
                "record_count": len(insert_data),
                "version": "v2"
            }

        except Exception as e:
            logger.error(f"存储QA模板V2失败: {e}")
            return {
                "success": False,
                "message": f"存储模板V2失败: {str(e)}"
            }

    def _ensure_collection_v2_exists(self, dim):
        """创建V2集合"""

        # 确保使用正确的集合名称（避免重复添加后缀）
        base_collection_name = QA_TEMPLATE_COLLECTION
        v2_collection_name = f"{base_collection_name}_v2"

        self.collection_name = v2_collection_name
        try:
            # 首先检查集合是否已存在
            if settings.docStoreConn.has_collection(self.collection_name):
                return True, f"V2集合 {self.collection_name} 已存在"
            analyzer_params = {
                "type": "chinese",
            }
            # 定义字段schema（包含V2新增字段）
            fields = [
                # 主键字段
                FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=100, is_primary=True),
                # QA模板字段
                FieldSchema(name="qa_id", dtype=DataType.VARCHAR, max_length=200),
                FieldSchema(name="question_canonical", dtype=DataType.VARCHAR, max_length=2000, enable_analyzer=True, analyzer_params=analyzer_params),
                FieldSchema(name="paraphrases", dtype=DataType.VARCHAR, max_length=8000),  # JSON字符串存储
                FieldSchema(name="needed_params", dtype=DataType.VARCHAR, max_length=2000),  # JSON字符串存储（V1兼容）
                FieldSchema(name="needed_params_typed", dtype=DataType.VARCHAR, max_length=4000),  # V2类型化参数信息
                FieldSchema(name="sql_template", dtype=DataType.VARCHAR, max_length=8000),
                FieldSchema(name="rule_id", dtype=DataType.VARCHAR, max_length=200),
                FieldSchema(name="tenant_id", dtype=DataType.VARCHAR, max_length=100),
                FieldSchema(name="template_version", dtype=DataType.VARCHAR, max_length=10),  # V2版本标记
                # 向量字段
                FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=dim),  # 密集向量
                FieldSchema(name="sparse_vector", dtype=DataType.SPARSE_FLOAT_VECTOR),  # 稀疏向量(BM25)
                # 时间戳字段
                FieldSchema(name="create_timestamp", dtype=DataType.FLOAT),
                FieldSchema(name="update_timestamp", dtype=DataType.FLOAT),
            ]

            # 创建BM25函数
            bm25_function_name = f"bm25_function_{str(uuid.uuid4())[:8]}"
            bm25_function = Function(
                name=bm25_function_name,
                function_type=FunctionType.BM25,
                input_field_names=["question_canonical"],
                output_field_names="sparse_vector"
            )

            # 创建集合架构
            description = f"QA模板V2集合（支持类型化参数和混合检索）- 创建于 {datetime.now().isoformat()}"
            schema = CollectionSchema(
                fields=fields,
                description=description,
                enable_dynamic_field=True
            )
            schema.add_function(bm25_function)

            # 创建集合
            conn = settings.docStoreConn._get_connection()
            conn.create_collection(
                self.collection_name,
                schema,
                consistency_level=DEFAULT_CONSISTENCY_LEVEL
            )

            # 为密集向量创建索引
            dense_index_params = {
                "metric_type": "COSINE",
                "index_type": "IVF_FLAT",
                "params": {"nlist": 128}
            }
            conn.create_index(self.collection_name, "vector", dense_index_params)

            # 为稀疏向量创建索引
            sparse_index_params = {
                "metric_type": "BM25",
                "index_type": "SPARSE_INVERTED_INDEX",
                "params": {
                    "inverted_index_algo": "DAAT_WAND",
                    "bm25_k1": 1.5,
                    "bm25_b": 0.75
                }
            }
            conn.create_index(self.collection_name, "sparse_vector", sparse_index_params)

            # 加载集合
            conn.load_collection(self.collection_name)

            logger.info(f"QA模板V2集合 {self.collection_name} 创建成功")
            return True, f"QA模板V2集合 {self.collection_name} 创建成功"

        except Exception as e:
            logger.error(f"创建QA模板V2集合失败: {e}")
            return False, f"创建V2集合失败: {str(e)}"

    def clear_tenant_templates(self, tenant_id: str) -> dict[str, Any]:
        """清空指定租户的所有QA模板"""
        try:
            conn = settings.docStoreConn._get_connection()
            
            # 检查并清空V1和V2集合
            base_collection_name = QA_TEMPLATE_COLLECTION
            v2_collection_name = f"{base_collection_name}_v2"
            
            cleared_collections = []
            
            # 清空V2集合
            if conn.has_collection(v2_collection_name):
                conn.drop_collection(v2_collection_name)
                cleared_collections.append(f"{v2_collection_name} (V2)")
                
            # 清空V1集合
            if conn.has_collection(base_collection_name):
                conn.drop_collection(base_collection_name)
                cleared_collections.append(f"{base_collection_name} (V1)")

            if cleared_collections:
                return {
                    "success": True,
                    "message": f"成功清空租户 {tenant_id} 的QA模板",
                    "cleared_collections": cleared_collections
                }
            else:
                return {
                    "success": True,
                    "message": f"租户 {tenant_id} 没有找到QA模板集合",
                    "cleared_collections": []
                }

        except Exception as e:
            logger.error(f"清空QA模板失败: {e}")
            return {
                "success": False,
                "message": f"清空模板失败: {str(e)}"
            }

    def delete_template_by_qa_id(self, qa_id: str, tenant_id: str) -> dict[str, Any]:
        """根据qa_id删除指定模板（单个）"""
        return self.delete_templates_by_qa_ids([qa_id], tenant_id)

    def delete_templates_by_qa_ids(self, qa_ids: list[str], tenant_id: str) -> dict[str, Any]:
        """根据qa_id列表批量删除模板"""
        try:
            # 获取可用的集合名称
            matcher = QATemplateMatchingService()
            collection_name, collection_version = matcher._get_available_collection()
            
            if not collection_name:
                return {
                    "success": False,
                    "message": "未找到可用的QA模板集合"
                }

            if not qa_ids:
                return {
                    "success": False,
                    "message": "qa_ids列表不能为空"
                }

            total_deleted = 0
            failed_qa_ids = []
            success_qa_ids = []

            for qa_id in qa_ids:
                try:
                    # 构建删除条件：匹配指定租户和qa_id的所有记录
                    delete_expr = f'tenant_id == "{tenant_id}" && qa_id == "{qa_id}"'
                    
                    # 先查询要删除的记录数量
                    query_results = settings.docStoreConn.query(
                        collection_name=collection_name,
                        filter=delete_expr,
                        output_fields=["id"]
                    )
                    
                    if not query_results:
                        logger.warning(f"未找到租户 {tenant_id} 中 qa_id 为 {qa_id} 的模板")
                        failed_qa_ids.append(qa_id)
                        continue

                    # 执行删除操作
                    delete_result = settings.docStoreConn.delete(
                        collection_name=collection_name,
                        filter=delete_expr
                    )

                    logger.info(f"删除QA模板 {qa_id} 结果: {delete_result}")
                    total_deleted += len(query_results)
                    success_qa_ids.append(qa_id)

                except Exception as e:
                    logger.error(f"删除QA模板 {qa_id} 失败: {e}")
                    failed_qa_ids.append(qa_id)

            # 生成结果消息
            if failed_qa_ids:
                if success_qa_ids:
                    message = f"部分删除成功：成功删除 {len(success_qa_ids)} 个模板，失败 {len(failed_qa_ids)} 个"
                else:
                    message = f"所有模板删除失败，共 {len(failed_qa_ids)} 个"
            else:
                message = f"成功删除 {len(success_qa_ids)} 个模板"

            return {
                "success": len(success_qa_ids) > 0,
                "message": message,
                "deleted_count": total_deleted,
                "failed_qa_ids": failed_qa_ids if failed_qa_ids else None,
                "success_qa_ids": success_qa_ids,
                "collection_used": f"{collection_name} ({collection_version})"
            }

        except Exception as e:
            logger.error(f"批量删除QA模板失败: {e}")
            return {
                "success": False,
                "message": f"批量删除模板失败: {str(e)}",
                "failed_qa_ids": qa_ids
            }



# ================================
# 2. 模板匹配服务
# ================================

class QATemplateMatchingService:
    """QA模板匹配服务 - 从Milvus中检索匹配的模板"""

    def __init__(self):
        self.base_collection_name = QA_TEMPLATE_COLLECTION
        self.v1_collection_name = QA_TEMPLATE_COLLECTION
        self.v2_collection_name = f"{QA_TEMPLATE_COLLECTION}_v2"

    def _get_available_collection(self):
        """获取可用的集合名称，优先使用V2集合"""
        if settings.docStoreConn.has_collection(self.v2_collection_name):
            return self.v2_collection_name, "v2"
        elif settings.docStoreConn.has_collection(self.v1_collection_name):
            return self.v1_collection_name, "v1"
        else:
            return None, None

    def find_best_template(
            self,
            db: Session,
            user_query: str,
            tenant_id: str,
            top_k: int = 10,
            threshold: float = 0.3,
            hybrid_weight: float = 0.7  # 密集向量权重，稀疏向量权重为1-hybrid_weight
    ) -> dict[str, Any] | None:
        """
        从Milvus中检索最匹配的QA模板
        使用混合检索（密集向量 + BM25稀疏向量）
        自动检测和使用V1或V2集合
        """
        try:
            # 获取可用的集合
            collection_name, collection_version = self._get_available_collection()
            if not collection_name:
                logger.error("未找到可用的QA模板集合")
                return None

            logger.info(f"使用集合: {collection_name} (版本: {collection_version})")

            # 获取嵌入模型并向量化用户查询
            embedding_model = LLMBundle(db, tenant_id, LLMType.EMBEDDING.value)
            query_embeddings, _ = embedding_model.encode_queries(user_query)
            query_vector = [get_float(v) for v in query_embeddings]

            # 设置输出字段（根据集合版本）
            if collection_version == "v2":
                output_fields = [
                    "qa_id", "question_canonical", "paraphrases", "needed_params", 
                    "needed_params_typed", "sql_template", "rule_id", "template_version"
                ]
            else:
                output_fields = [
                    "qa_id", "question_canonical", "paraphrases", "needed_params", 
                    "sql_template", "rule_id"
                ]

            filters = f'tenant_id == "{tenant_id}"'  # 添加租户过滤

            dense_req = AnnSearchRequest(
                data=[query_vector],
                anns_field="vector",
                param={"metric_type": "COSINE", "params": {"nprobe": 10}},
                limit=top_k,
                expr=filters,
            )
            sparse_req = AnnSearchRequest(
                data=[user_query],
                anns_field="sparse_vector",
                param={"metric_type": "BM25", "params": {"drop_ratio_search": 0.1}},
                limit=top_k,
                expr=filters,
            )
            ranker = WeightedRanker(hybrid_weight, 1 - hybrid_weight)

            results = settings.docStoreConn.hybrid_search(
                collection_name=collection_name,
                reqs=[dense_req, sparse_req],
                ranker=ranker,
                limit=top_k,
                output_fields=output_fields
            )
            logger.info(f"混合检索结果: {results}")

            # 处理检索结果
            if not results:
                logger.warning("混合检索未返回任何结果")
                return None

            # 提取最佳匹配（results[0]是得分最高的）
            best_result = results[0]

            # 计算相似度（distance转换为similarity）
            similarity = best_result.get('distance', 1.0)

            # 检查是否超过阈值
            if similarity < threshold:
                logger.info(f"最佳匹配相似度 {similarity:.4f} 低于阈值 {threshold}")
                return None

            # 提取entity字段
            entity = best_result.get('entity', {})
            if not entity:
                logger.error("检索结果中缺少entity字段")
                return None

            # 解析JSON字段
            try:
                paraphrases_str = entity.get('paraphrases', '[]')
                paraphrases = json.loads(paraphrases_str) if paraphrases_str else []
            except json.JSONDecodeError as e:
                logger.warning(f"解析paraphrases JSON失败: {e}")
                paraphrases = []

            try:
                needed_params_str = entity.get('needed_params', '[]')
                needed_params = json.loads(needed_params_str) if needed_params_str else []
            except json.JSONDecodeError as e:
                logger.warning(f"解析needed_params JSON失败: {e}")
                needed_params = []

            # 解析V2类型化参数（如果存在）
            typed_params = None
            if collection_version == "v2":
                try:
                    typed_params_str = entity.get('needed_params_typed', '[]')
                    typed_params = json.loads(typed_params_str) if typed_params_str else []
                except json.JSONDecodeError as e:
                    logger.warning(f"解析needed_params_typed JSON失败: {e}")
                    typed_params = []

            # 解析 sql_template（V2版本存储为JSON数组）
            sql_template_value = entity.get('sql_template')
            if collection_version == "v2":
                try:
                    # V2版本：尝试解析为JSON数组
                    if isinstance(sql_template_value, str) and sql_template_value.startswith('['):
                        sql_template = json.loads(sql_template_value)
                    else:
                        # 向后兼容：如果不是JSON数组格式，转换为单元素数组
                        sql_template = [sql_template_value] if sql_template_value else []
                except json.JSONDecodeError as e:
                    logger.warning(f"解析sql_template JSON失败: {e}")
                    # 降级处理：转换为单元素数组
                    sql_template = [sql_template_value] if sql_template_value else []
            else:
                # V1版本：保持原有字符串格式
                sql_template = sql_template_value

            # 构建返回结果
            best_match = {
                'qa_id': entity.get('qa_id'),
                'question_canonical': entity.get('question_canonical'),
                'paraphrases': paraphrases,
                'needed_params': needed_params,
                'sql_template': sql_template,
                'rule_id': entity.get('rule_id') if entity.get('rule_id') else None,
                'similarity': similarity,
                'matched_text': entity.get('question_canonical'),
                'match_score': best_result.get('distance', 1.0),
                'collection_version': collection_version,  # 标记使用的集合版本
            }

            # 如果是V2模板，添加类型化参数信息
            if collection_version == "v2" and typed_params:
                best_match['needed_params_typed'] = typed_params

            logger.info(f"找到最佳匹配模板: qa_id={best_match['qa_id']}, similarity={similarity:.4f}, version={collection_version}")
            return best_match

        except Exception as e:
            logger.error(f"检索QA模板失败: {e}")
            return None


# ================================
# 3. 重构槽位抽取服务（无状态版本）
# ================================

class StatelessSlotExtractionService:
    """无状态槽位抽取服务"""

    def __init__(self):
        # 单轮对话提示模板
        self.single_round_prompt = """你是智能参数抽取器，请从用户查询中抽取参数值。

需要抽取的字段：{needed_params}
当前系统日期：{system_date}
数据库表结构信息：
{table_schemas}

用户查询：{user_query}

请从查询中抽取对应的参数值：
- 如果某个字段在用户问题中没有明确提及，请返回 null
- 对于时间相关字段，可以根据系统日期进行推断
- 教师姓名可能需要转换为教师ID，请根据表结构信息判断
- 请尽可能准确地抽取信息

请返回JSON格式：{{"field1": "value1", "field2": "value2", ...}}
只返回JSON，不要包含其他文字。"""

        # 支持类型化参数的单轮对话提示模板
        self.typed_single_round_prompt = """你是智能参数抽取器，请从用户查询中抽取参数值，并按照指定的数据类型输出。

需要抽取的参数：
{typed_params_info}

当前系统日期：{system_date}
数据库表结构信息：
{table_schemas}

用户查询：{user_query}

请从查询中抽取对应的参数值，注意数据类型要求：
- string: 输出字符串值，如 "张三"
- integer: 输出整数值，如 25
- float: 输出浮点数值，如 85.5
- boolean: 输出布尔值，如 true 或 false
- date: 输出日期字符串，格式为 "YYYY-MM-DD"，如 "2024-01-15"
- 如果某个字段在用户问题中没有明确提及，请返回 null
- 对于时间相关字段，可以根据系统日期进行推断
- 教师姓名可能需要转换为教师ID，请根据表结构信息判断

请返回JSON格式，确保数据类型正确：{{"param1": value1, "param2": "value2", "param3": 123, ...}}
只返回JSON，不要包含其他文字。"""

        # 多轮对话提示模板
        self.multi_round_prompt = """你是智能参数抽取器，请从多轮对话中抽取和合并参数。

需要抽取的字段：{needed_params}
当前系统日期：{system_date}
数据库表结构信息：
{table_schemas}

完整对话历史：
{conversation_history}

已有参数：
{existing_params}

还缺失的参数：
{missing_params}

请重点关注最新的用户输入，将其与已有参数合并。对于用户刚提供的信息，请尝试匹配到对应的缺失参数字段。

请返回完整的参数对象JSON：{{"field1": "value1", "field2": "value2", ...}}
只返回JSON，不要包含其他文字。"""

        # 支持类型化参数的多轮对话提示模板
        self.typed_multi_round_prompt = """你是智能参数抽取器，请从多轮对话中抽取和合并参数，并按照指定的数据类型输出。

需要抽取的参数：
{typed_params_info}

当前系统日期：{system_date}
数据库表结构信息：
{table_schemas}

完整对话历史：
{conversation_history}

已有参数：
{existing_params}

还缺失的参数：
{missing_params}

请重点关注最新的用户输入，将其与已有参数合并，注意数据类型要求：
- string: 输出字符串值，如 "张三"
- integer: 输出整数值，如 25
- float: 输出浮点数值，如 85.5
- boolean: 输出布尔值，如 true 或 false
- date: 输出日期字符串，格式为 "YYYY-MM-DD"，如 "2024-01-15"

请返回完整的参数对象JSON，确保数据类型正确：{{"param1": value1, "param2": "value2", ...}}
只返回JSON，不要包含其他文字。"""

    def extract_slots(
        self,
        db: Session,
        user_query: str,
        needed_params: list[str],
        table_schemas: list[dict[str, Any]],
        system_date: str,
        tenant_id: str,
        llm_name: str | None = None
    ) -> dict[str, Any]:
        """单轮槽位抽取 - 与原有实现保持一致"""
        try:
            # 准备表结构信息
            schema_info = ""
            for schema in table_schemas:
                schema_info += f"表 {schema['table_name']}:\n"
                for col in schema['columns']:
                    schema_info += f"  - {col['name']} ({col['type']}): {col.get('description', '')}\n"

            # 构建提示词
            prompt = self.single_round_prompt.format(
                needed_params=needed_params,
                system_date=system_date,
                table_schemas=schema_info,
                user_query=user_query
            )

            # 调用LLM
            chat_model = LLMBundle(db, tenant_id, LLMType.CHAT.value, llm_name)

            response = chat_model.chat(
                system=prompt,
                history=[{"role": "user", "content": "请按照要求输出"}],
                gen_conf={"temperature": 0.1, "max_tokens": 500}
            )

            # 解析JSON结果
            try:
                json_match = re.search(r'\{.*\}', response, re.DOTALL)
                if json_match:
                    extracted_params = json.loads(json_match.group())
                else:
                    extracted_params = json.loads(response)
            except json.JSONDecodeError:
                logger.error(f"Failed to parse LLM response as JSON: {response}")
                extracted_params = {}

            # 分析缺失参数
            missing_params = []
            valid_params = {}

            for param in needed_params:
                if param not in extracted_params or extracted_params[param] is None:
                    missing_params.append(param)
                else:
                    valid_params[param] = extracted_params[param]

            return {
                "extracted_params": valid_params,
                "missing_params": missing_params,
                "confidence": 0.8 if len(missing_params) == 0 else 0.5,
                "raw_response": response
            }

        except Exception as e:
            logger.error(f"Error extracting slots: {e}")
            return {
                "extracted_params": {},
                "missing_params": needed_params,
                "confidence": 0.0,
                "raw_response": ""
            }

    def extract_and_merge_slots(
        self,
        db: Session,
        dialog_context: dict[str, Any],
        table_schemas: list[dict[str, Any]],
        system_date: str,
        tenant_id: str,
        llm_name: str | None = None
    ) -> dict[str, Any]:
        """多轮对话槽位抽取和合并 - 新增方法"""
        try:
            if not dialog_context.get("matched_template"):
                raise ValueError("No matched template in dialog context")

            template = dialog_context["matched_template"]
            needed_params = template["needed_params"]
            existing_params = dialog_context.get("accumulated_params", {})
            missing_params = dialog_context.get("missing_params", needed_params)

            # 构建对话历史
            conversation_history = ""
            rounds = dialog_context.get("rounds", [])
            for round_info in rounds:
                user_input = round_info.get("user_input", round_info.get("user_query", ""))
                conversation_history += f"轮次{round_info['round_id']}: {user_input}\n"

            # 准备表结构信息
            schema_info = ""
            for schema in table_schemas:
                schema_info += f"表 {schema['table_name']}:\n"
                for col in schema['columns']:
                    schema_info += f"  - {col['name']} ({col['type']}): {col.get('description', '')}\n"

            # 构建提示词
            prompt = self.multi_round_prompt.format(
                needed_params=needed_params,
                system_date=system_date,
                table_schemas=schema_info,
                conversation_history=conversation_history.strip(),
                existing_params=json.dumps(existing_params, ensure_ascii=False, indent=2),
                missing_params=missing_params
            )

            # 调用LLM
            chat_model = LLMBundle(db, tenant_id, LLMType.CHAT.value, llm_name)

            response = chat_model.chat(
                system=prompt,
                history=[{"role": "user", "content": "请按照要求输出参数"}],
                gen_conf={"temperature": 0.1, "max_tokens": 500}
            )

            # 解析结果
            try:
                json_match = re.search(r'\{.*\}', response, re.DOTALL)
                if json_match:
                    extracted_params = json.loads(json_match.group())
                else:
                    extracted_params = json.loads(response)
            except json.JSONDecodeError:
                logger.error(f"Failed to parse multiturn LLM response: {response}")
                extracted_params = existing_params  # 保持现有参数

            # 分析缺失参数
            new_missing_params = []
            valid_params = {}

            for param in needed_params:
                if param not in extracted_params or extracted_params[param] is None:
                    new_missing_params.append(param)
                else:
                    valid_params[param] = extracted_params[param]

            return {
                "extracted_params": valid_params,
                "missing_params": new_missing_params,
                "confidence": 0.9 if len(new_missing_params) == 0 else 0.6,
                "raw_response": response
            }

        except Exception as e:
            logger.error(f"Error in multiturn slot extraction: {e}")
            return {
                "extracted_params": dialog_context.get("accumulated_params", {}),
                "missing_params": dialog_context.get("missing_params", []),
                "confidence": 0.0,
                "raw_response": ""
            }

    def extract_slots_typed(
        self,
        db: Session,
        user_query: str,
        typed_params: list[dict[str, Any]],  # [{"name": "teacher_id", "data_type": "string", "description": "教师ID", "required": True}, ...]
        table_schemas: list[dict[str, Any]],
        system_date: str,
        tenant_id: str,
        llm_name: str | None = None
    ) -> dict[str, Any]:
        """类型化单轮槽位抽取 - 支持数据类型"""
        try:
            # 准备表结构信息
            schema_info = ""
            for schema in table_schemas:
                schema_info += f"表 {schema['table_name']}:\n"
                for col in schema['columns']:
                    schema_info += f"  - {col['name']} ({col['type']}): {col.get('description', '')}\n"

            # 准备类型化参数信息
            typed_params_info = ""
            param_names = []
            for param in typed_params:
                param_name = param.get("name", "")
                param_type = param.get("data_type", "string")
                param_desc = param.get("description", "")
                required = param.get("required", True)
                
                param_names.append(param_name)
                typed_params_info += f"- {param_name} ({param_type}): {param_desc}"
                if not required:
                    typed_params_info += " [可选]"
                typed_params_info += "\n"

            # 构建提示词
            prompt = self.typed_single_round_prompt.format(
                typed_params_info=typed_params_info.strip(),
                system_date=system_date,
                table_schemas=schema_info,
                user_query=user_query
            )

            # 调用LLM
            chat_model = LLMBundle(db, tenant_id, LLMType.CHAT.value, llm_name)

            response = chat_model.chat(
                system=prompt,
                history=[{"role": "user", "content": "请按照要求输出"}],
                gen_conf={"temperature": 0.1, "max_tokens": 500}
            )

            # 解析JSON结果
            try:
                json_match = re.search(r'\{.*\}', response, re.DOTALL)
                if json_match:
                    extracted_params = json.loads(json_match.group())
                else:
                    extracted_params = json.loads(response)
            except json.JSONDecodeError:
                logger.error(f"Failed to parse typed LLM response as JSON: {response}")
                extracted_params = {}

            # 验证和转换数据类型
            validated_params = {}
            missing_params = []

            for param in typed_params:
                param_name = param.get("name")
                param_type = param.get("data_type", "string")
                required = param.get("required", True)
                
                if param_name not in extracted_params or extracted_params[param_name] is None:
                    if required:
                        missing_params.append(param_name)
                    continue
                
                raw_value = extracted_params[param_name]
                
                # 类型验证和转换
                try:
                    validated_value = self._validate_and_convert_type(raw_value, param_type)
                    validated_params[param_name] = validated_value
                except ValueError as e:
                    logger.warning(f"Type conversion failed for {param_name}: {e}")
                    if required:
                        missing_params.append(param_name)

            return {
                "extracted_params": validated_params,
                "missing_params": missing_params,
                "confidence": 0.8 if len(missing_params) == 0 else 0.5,
                "raw_response": response
            }

        except Exception as e:
            logger.error(f"Error extracting typed slots: {e}")
            return {
                "extracted_params": {},
                "missing_params": param_names,
                "confidence": 0.0,
                "raw_response": ""
            }

    def _validate_and_convert_type(self, value: Any, data_type: str) -> Any:
        """验证和转换数据类型"""
        if value is None:
            return None
            
        data_type = data_type.lower()
        
        if data_type == "string":
            return str(value)
        elif data_type == "integer":
            if isinstance(value, int):
                return value
            elif isinstance(value, str) and value.isdigit():
                return int(value)
            elif isinstance(value, float) and value.is_integer():
                return int(value)
            else:
                raise ValueError(f"Cannot convert {value} to integer")
        elif data_type == "float":
            if isinstance(value, (int, float)):
                return float(value)
            elif isinstance(value, str):
                return float(value)
            else:
                raise ValueError(f"Cannot convert {value} to float")
        elif data_type == "boolean":
            if isinstance(value, bool):
                return value
            elif isinstance(value, str):
                if value.lower() in ["true", "1", "yes", "是"]:
                    return True
                elif value.lower() in ["false", "0", "no", "否"]:
                    return False
                else:
                    raise ValueError(f"Cannot convert {value} to boolean")
            elif isinstance(value, (int, float)):
                return bool(value)
            else:
                raise ValueError(f"Cannot convert {value} to boolean")
        elif data_type == "date":
            if isinstance(value, str):
                # 简单日期格式验证
                import re
                if re.match(r'^\d{4}-\d{2}-\d{2}$', value):
                    return value
                else:
                    raise ValueError(f"Date format should be YYYY-MM-DD, got {value}")
            else:
                raise ValueError(f"Date should be string, got {type(value)}")
        else:
            # 默认转换为字符串
            return str(value)

    def extract_and_merge_slots_typed(
        self,
        db: Session,
        dialog_context: dict[str, Any],
        table_schemas: list[dict[str, Any]],
        system_date: str,
        tenant_id: str,
        llm_name: str | None = None
    ) -> dict[str, Any]:
        """V2类型化多轮对话槽位抽取和合并"""
        try:
            if not dialog_context.get("matched_template"):
                raise ValueError("No matched template in dialog context")

            template = dialog_context["matched_template"]
            typed_params = template.get("needed_params_typed", [])
            if not typed_params:
                # 如果没有类型化参数，回退到V1方法
                logger.warning("V2模板缺少类型化参数信息，回退到V1方法")
                return self.extract_and_merge_slots(db, dialog_context, table_schemas, system_date, tenant_id, llm_name)

            existing_params = dialog_context.get("accumulated_params", {})
            param_names = [param.get("name") for param in typed_params if param.get("name")]
            missing_params = dialog_context.get("missing_params", param_names)

            # 构建对话历史
            conversation_history = ""
            rounds = dialog_context.get("rounds", [])
            for round_info in rounds:
                user_input = round_info.get("user_input", round_info.get("user_query", ""))
                conversation_history += f"轮次{round_info['round_id']}: {user_input}\n"

            # 准备表结构信息
            schema_info = ""
            for schema in table_schemas:
                schema_info += f"表 {schema['table_name']}:\n"
                for col in schema['columns']:
                    schema_info += f"  - {col['name']} ({col['type']}): {col.get('description', '')}\n"

            # 准备类型化参数信息
            typed_params_info = ""
            for param in typed_params:
                param_name = param.get("name", "")
                param_type = param.get("data_type", "string")
                param_desc = param.get("description", "")
                required = param.get("required", True)
                
                typed_params_info += f"- {param_name} ({param_type}): {param_desc}"
                if not required:
                    typed_params_info += " [可选]"
                typed_params_info += "\n"

            # 构建提示词
            prompt = self.typed_multi_round_prompt.format(
                typed_params_info=typed_params_info.strip(),
                system_date=system_date,
                table_schemas=schema_info,
                conversation_history=conversation_history.strip(),
                existing_params=json.dumps(existing_params, ensure_ascii=False, indent=2),
                missing_params=missing_params
            )

            # 调用LLM
            chat_model = LLMBundle(db, tenant_id, LLMType.CHAT.value, llm_name)

            response = chat_model.chat(
                system=prompt,
                history=[{"role": "user", "content": "请按照要求输出参数"}],
                gen_conf={"temperature": 0.1, "max_tokens": 500}
            )

            # 解析结果
            try:
                json_match = re.search(r'\{.*\}', response, re.DOTALL)
                if json_match:
                    extracted_params = json.loads(json_match.group())
                else:
                    extracted_params = json.loads(response)
            except json.JSONDecodeError:
                logger.error(f"Failed to parse typed multiturn LLM response: {response}")
                extracted_params = existing_params  # 保持现有参数

            # 验证和转换数据类型
            validated_params = {}
            new_missing_params = []

            for param in typed_params:
                param_name = param.get("name")
                param_type = param.get("data_type", "string")
                required = param.get("required", True)
                
                if param_name not in extracted_params or extracted_params[param_name] is None:
                    if required:
                        new_missing_params.append(param_name)
                    continue
                
                raw_value = extracted_params[param_name]
                
                # 类型验证和转换
                try:
                    validated_value = self._validate_and_convert_type(raw_value, param_type)
                    validated_params[param_name] = validated_value
                except ValueError as e:
                    logger.warning(f"Type conversion failed for {param_name}: {e}")
                    if required:
                        new_missing_params.append(param_name)

            return {
                "extracted_params": validated_params,
                "missing_params": new_missing_params,
                "confidence": 0.9 if len(new_missing_params) == 0 else 0.6,
                "raw_response": response
            }

        except Exception as e:
            logger.error(f"Error in typed multiturn slot extraction: {e}")
            return {
                "extracted_params": dialog_context.get("accumulated_params", {}),
                "missing_params": dialog_context.get("missing_params", []),
                "confidence": 0.0,
                "raw_response": ""
            }

# ================================
# 4. 追问服务
# ================================

class ClarificationService:
    """追问服务"""

    def __init__(self):
        self.prompt_template = """请用简短、友好的中文向用户询问所缺失的信息。

缺失的字段：{missing_params}
用户原始问题：{user_query}
相关表结构：{table_schemas}

请生成一个自然、友好的追问句子，帮助用户补充缺失的信息。
- 使用简洁明了的语言
- 可以提供一些示例或提示
- 保持礼貌和友好的语调
- 一次最多询问2-3个关键信息

只返回追问文本，不要包含其他内容。"""

    def generate_clarification(
            self,
            db: Session,
            user_query: str,
            missing_params: list[str],
            table_schemas: list[dict[str, Any]],
            tenant_id: str,
            llm_name: str | None = None
    ) -> str:
        """生成追问文案"""
        try:
            # 准备相关表结构信息
            schema_info = ""
            for schema in table_schemas:
                for col in schema['columns']:
                    if col['name'] in missing_params:
                        schema_info += f"- {col['name']}: {col.get('description', '')}\n"

            # 构建提示词
            prompt = self.prompt_template.format(
                missing_params=missing_params,
                user_query=user_query,
                table_schemas=schema_info
            )

            # 调用LLM
            chat_model = LLMBundle(db, tenant_id, LLMType.CHAT.value, llm_name)

            response = chat_model.chat(
                system=prompt,
                history=[{"role": "user", "content": "按照要求输出内容"}],
                gen_conf={"temperature": 0.7, "max_tokens": 200}
            )

            return response.strip()

        except Exception as e:
            logger.error(f"Error generating clarification: {e}")
            return f"请提供以下信息：{', '.join(missing_params)}"


# ================================
# 5. 核心无状态服务
# ================================

class StatelessQAService:
    """无状态QA服务 - 主要服务类"""

    def __init__(self):
        self.template_matcher = QATemplateMatchingService()
        self.slot_extractor = StatelessSlotExtractionService()
        self.clarification_generator = ClarificationService()

    def interpret(
            self,
            db: Session,
            current_input: str,
            table_schemas: list[dict[str, Any]],
            tenant_id: str,
            dialog_context: dict[str, Any] | None = None,
            system_date: str | None = None,
            similarity_threshold: float = 0.3,
            hybrid_weight: float = 0.7,
            llm_name: str | None = None,
            force_new_template: bool = False,
            enable_slot_merge: bool = True
    ) -> dict[str, Any]:
        """
        无状态查询解释 - 核心方法

        Args:
            current_input: 当前用户输入
            dialog_context: 可选的对话上下文（多轮对话时提供）
            其他参数...

        Returns:
            包含处理结果和更新后上下文的字典
        """
        try:
            # 1. 初始化或更新上下文
            if dialog_context is None:
                # 新对话
                context = self._create_initial_context(current_input)
                is_first_round = True
            else:
                # 继续对话
                context = dialog_context.copy()
                self._add_round_to_context(context, current_input)
                is_first_round = False

            # 2. 模板匹配
            if is_first_round or force_new_template or not context.get("matched_template"):
                # 使用初始查询进行模板匹配
                query_for_matching = context["initial_query"] if not force_new_template else current_input

                matched_template = self.template_matcher.find_best_template(
                    db=db,
                    user_query=query_for_matching,
                    tenant_id=tenant_id,
                    threshold=similarity_threshold,
                    hybrid_weight=hybrid_weight
                )

                if not matched_template:
                    return self._create_error_response(
                        context, "未找到匹配的QA模板", 0.0
                    )

                context["matched_template"] = matched_template
            else:
                matched_template = context["matched_template"]

            # 3. 参数抽取和合并
            if enable_slot_merge and len(context["rounds"]) > 1:
                # 多轮对话：智能合并参数
                if matched_template.get("collection_version") == "v2" and matched_template.get("needed_params_typed"):
                    # 使用V2类型化参数抽取（多轮）
                    logger.info("使用V2类型化多轮参数抽取")
                    slot_result = self.slot_extractor.extract_and_merge_slots_typed(
                        db=db,
                        dialog_context=context,
                        table_schemas=table_schemas,
                        system_date=system_date or datetime.now().strftime("%Y-%m-%d"),
                        tenant_id=tenant_id,
                        llm_name=llm_name
                    )
                else:
                    # 使用V1参数抽取（多轮）
                    logger.info("使用V1多轮参数抽取")
                    slot_result = self.slot_extractor.extract_and_merge_slots(
                        db=db,
                        dialog_context=context,
                        table_schemas=table_schemas,
                        system_date=system_date or datetime.now().strftime("%Y-%m-%d"),
                        tenant_id=tenant_id,
                        llm_name=llm_name
                    )
            else:
                # 单轮对话：直接抽取
                if matched_template.get("collection_version") == "v2" and matched_template.get("needed_params_typed"):
                    # 使用V2类型化参数抽取（单轮）
                    logger.info("使用V2类型化单轮参数抽取")
                    slot_result = self.slot_extractor.extract_slots_typed(
                        db=db,
                        user_query=current_input,
                        typed_params=matched_template["needed_params_typed"],
                        table_schemas=table_schemas,
                        system_date=system_date or datetime.now().strftime("%Y-%m-%d"),
                        tenant_id=tenant_id,
                        llm_name=llm_name
                    )
                else:
                    # 使用V1参数抽取（单轮）
                    logger.info("使用V1单轮参数抽取")
                    slot_result = self.slot_extractor.extract_slots(
                        db=db,
                        user_query=current_input,
                        needed_params=matched_template["needed_params"],
                        table_schemas=table_schemas,
                        system_date=system_date or datetime.now().strftime("%Y-%m-%d"),
                        tenant_id=tenant_id,
                        llm_name=llm_name
                    )

            # 4. 更新上下文中的参数
            context["accumulated_params"].update(slot_result["extracted_params"])
            context["missing_params"] = slot_result["missing_params"]

            # 5. 生成响应
            if slot_result["missing_params"]:
                # 需要追问
                clarify_message = self.clarification_generator.generate_clarification(
                    db=db,
                    user_query=current_input,
                    missing_params=slot_result["missing_params"],
                    table_schemas=table_schemas,
                    tenant_id=tenant_id,
                    llm_name=llm_name
                )

                return {
                    "status": "NEED_CLARIFY",
                    "qa_id": matched_template["qa_id"],
                    "sql_template": matched_template["sql_template"],
                    "complete_params": context["accumulated_params"],
                    "missing_params": slot_result["missing_params"],
                    "clarify_message": clarify_message,
                    "rule_id": matched_template.get("rule_id"),
                    "confidence": slot_result["confidence"],
                    "processing_info": {
                        "rounds_count": len(context["rounds"]),
                        "template_source": "cached" if not is_first_round else "new_match",
                        "slot_extraction_mode": "multi_round" if len(context["rounds"]) > 1 else "single_round"
                    },
                    "updated_context": context
                }
            else:
                # 参数完整
                return {
                    "status": "OK",
                    "qa_id": matched_template["qa_id"],
                    "sql_template": matched_template["sql_template"],
                    "complete_params": context["accumulated_params"],
                    "missing_params": [],
                    "rule_id": matched_template.get("rule_id"),
                    "confidence": slot_result["confidence"],
                    "processing_info": {
                        "rounds_count": len(context["rounds"]),
                        "completion_round": len(context["rounds"])
                    },
                    "updated_context": context
                }

        except Exception as e:
            logger.error(f"Error in stateless interpret: {e}")
            return self._create_error_response(
                context if 'context' in locals() else {},
                f"处理出错: {str(e)}",
                0.0
            )

    def _create_initial_context(self, initial_query: str) -> dict[str, Any]:
        """创建初始上下文"""
        import time
        return {
            "session_id": f"sess_{int(time.time())}_{uuid.uuid4().hex[:8]}",
            "initial_query": initial_query,
            "rounds": [
                {
                    "round_id": 1,
                    "user_input": initial_query,
                    "timestamp": datetime.now().isoformat()
                }
            ],
            "matched_template": None,
            "accumulated_params": {},
            "missing_params": [],
            "metadata": {
                "created_at": datetime.now().isoformat()
            }
        }

    def _add_round_to_context(self, context: dict[str, Any], user_input: str):
        """向上下文添加新轮次"""
        next_round_id = len(context["rounds"]) + 1
        context["rounds"].append({
            "round_id": next_round_id,
            "user_input": user_input,
            "timestamp": datetime.now().isoformat()
        })
        context["metadata"]["last_updated"] = datetime.now().isoformat()

    def _create_error_response(self, context: dict[str, Any], message: str, confidence: float) -> dict[str, Any]:
        """创建错误响应"""
        return {
            "status": "ERROR",
            "message": message,
            "confidence": confidence,
            "processing_info": {
                "error_occurred": True,
                "rounds_count": len(context.get("rounds", []))
            },
            "updated_context": context
        }

# ================================
# 6. 评分和RAG服务
# ================================

class LLMScoringService:
    """LLM驱动的评分服务"""

    def __init__(self):
        self.prompt_template = """你是一个专业的教师考核评分助手。请根据提供的评分规则和数据，计算出准确的评分结果。

评分规则：
{rule_description}

数据内容：
{data_json}

上下文信息：
{context_info}

请按以下格式输出评分结果：

## 评分结果
最终得分：[具体分数]

## 评分分析
[详细的评分计算过程，包括每个项目的得分依据]

## 数据汇总
[对输入数据的关键统计信息]

## 改进建议
[基于评分结果给出的改进建议，可选]

要求：
1. 严格按照提供的评分规则进行计算
2. 计算过程要清晰透明
3. 如果数据不足或规则不明确，请明确指出
4. 最终得分要给出具体数值
5. 保持客观公正的评价态度"""

    def calculate_score(
            self,
            db: Session,
            rule_description: str,
            data: list[dict[str, Any]],
            context: dict[str, Any] | None = None,
            tenant_id: str = "",
            llm_name: str | None = None
    ) -> dict[str, Any]:
        """使用LLM计算评分"""
        try:
            if not data:
                return {
                    "score": None,
                    "score_text": "无数据可供评分",
                    "analysis": "提供的数据为空，无法进行评分计算",
                    "suggestions": "请提供有效的数据进行评分",
                    "data_summary": {"total_records": 0}
                }

            # 准备数据
            data_json = json.dumps(data, ensure_ascii=False, indent=2)
            context_info = json.dumps(context or {}, ensure_ascii=False, indent=2)

            # 构建提示词
            prompt = self.prompt_template.format(
                rule_description=rule_description,
                data_json=data_json,
                context_info=context_info
            )

            # 调用LLM
            chat_model = LLMBundle(db, tenant_id, LLMType.CHAT.value, llm_name)

            response = chat_model.chat(
                system=prompt,
                history=[{"role": "user", "content": "请开始评分"}],
                gen_conf={"temperature": 0.1, "max_tokens": 1500}
            )

            # 解析LLM响应
            parsed_result = self._parse_score_response(response, data)

            return parsed_result

        except Exception as e:
            logger.error(f"Error calculating score with LLM: {e}")
            return {
                "score": None,
                "score_text": f"评分计算失败：{str(e)}",
                "analysis": "系统在处理评分请求时发生错误",
                "suggestions": "请检查输入数据和规则描述，稍后重试",
                "data_summary": {"total_records": len(data), "error": str(e)}
            }

    def _parse_score_response(self, response: str, original_data: list[dict[str, Any]]) -> dict[str, Any]:
        """解析LLM的评分响应"""
        try:
            # 提取最终得分
            score = None
            score_patterns = [
                r'最终得分[：:]\s*([0-9]+\.?[0-9]*)',
                r'得分[：:]\s*([0-9]+\.?[0-9]*)',
                r'分数[：:]\s*([0-9]+\.?[0-9]*)'
            ]

            for pattern in score_patterns:
                score_match = re.search(pattern, response)
                if score_match:
                    score = float(score_match.group(1))
                    break

            # 分段提取内容
            sections = self._extract_sections(response)

            # 生成数据汇总
            data_summary = self._generate_data_summary(original_data)

            return {
                "score": score,
                "score_text": sections.get("评分结果", "").strip(),
                "analysis": sections.get("评分分析", "").strip(),
                "suggestions": sections.get("改进建议", "").strip() or None,
                "data_summary": data_summary,
                "raw_response": response
            }

        except Exception as e:
            logger.error(f"Error parsing score response: {e}")
            return {
                "score": None,
                "score_text": response,  # 返回原始响应
                "analysis": "响应解析失败，请查看完整文本",
                "suggestions": None,
                "data_summary": {"total_records": len(original_data), "parse_error": str(e)},
                "raw_response": response
            }

    def _extract_sections(self, response: str) -> dict[str, str]:
        """从响应中提取各个部分"""
        sections = {}
        current_section = None
        current_content = []

        lines = response.split('\n')
        for line in lines:
            # 检查是否是标题行
            if line.startswith('## '):
                # 保存前一个部分
                if current_section:
                    sections[current_section] = '\n'.join(current_content).strip()

                # 开始新部分
                current_section = line[3:].strip()
                current_content = []
            elif current_section:
                current_content.append(line)

        # 保存最后一个部分
        if current_section:
            sections[current_section] = '\n'.join(current_content).strip()

        return sections

    def _generate_data_summary(self, data: list[dict[str, Any]]) -> dict[str, Any]:
        """生成数据汇总信息"""
        if not data:
            return {"total_records": 0}

        summary = {
            "total_records": len(data),
            "fields": list(data[0].keys()) if data else []
        }

        # 统计数值字段
        numeric_fields = []
        for field in summary["fields"]:
            values = [item.get(field) for item in data if isinstance(item.get(field), (int, float))]
            if values:
                numeric_fields.append({
                    "field": field,
                    "total": sum(values),
                    "average": sum(values) / len(values),
                    "max": max(values),
                    "min": min(values)
                })

        if numeric_fields:
            summary["numeric_stats"] = numeric_fields

        return summary


class RAGService:
    """RAG回答服务（基于现有知识库）"""

    def __init__(self):
        self.prompt_template = """根据以下资料回答用户问题，请引用要点并附上来源：

资料内容：
{retrieved_chunks}

用户问题：{query}

请生成清晰、简短的中文回答，并在回答中适当引用资料内容。
如果资料中没有相关信息，请明确说明。"""

    def generate_answer(
            self,
            db: Session,
            query: str,
            kb_id: str,
            tenant_id: str,
            top_k: int = 5,
            llm_name: str | None = None
    ) -> dict[str, Any]:
        """生成RAG回答"""
        try:
            from api.db.services.knowledgebase_service import KnowledgebaseService
            from core.nlp import search

            # 获取知识库信息
            kb = KnowledgebaseService.get_by_id(db, kb_id)
            if not kb:
                raise ValueError(f"Knowledge base {kb_id} not found")

            collection_name = search.index_name_one(tenant_id, kb.name)

            # 检索相关文档
            embedding_model = LLMBundle(db, tenant_id, LLMType.EMBEDDING.value)
            query_embeddings, _ = embedding_model.encode([query])

            # 向量搜索
            search_results = settings.docStoreConn.search(
                collection_name=collection_name,
                vector=query_embeddings[0].tolist(),
                filter={"available_int": 1},  # 只搜索可用文档
                top_k=top_k
            )

            # 准备检索到的文档
            retrieved_chunks = []
            sources = []

            for idx, result in enumerate(search_results):
                chunk_content = result.get('content_with_weight', '')
                doc_name = result.get('doc_name', f'文档{idx + 1}')

                retrieved_chunks.append(f"[来源{idx + 1}: {doc_name}]\n{chunk_content}")
                sources.append({
                    "source": doc_name,
                    "content": chunk_content[:200] + "..." if len(chunk_content) > 200 else chunk_content,
                    "score": result.get('score', 0.0)
                })

            if not retrieved_chunks:
                return {
                    "answer": "抱歉，我在知识库中没有找到相关信息来回答您的问题。",
                    "sources": [],
                    "confidence": 0.0
                }

            # 构建提示词
            chunks_text = "\n\n".join(retrieved_chunks)
            prompt = self.prompt_template.format(
                retrieved_chunks=chunks_text,
                query=query
            )

            # 调用LLM生成回答
            chat_model = LLMBundle(db, tenant_id, LLMType.CHAT.value, llm_name)

            answer = chat_model.chat(
                system=prompt,
                history=[{"role": "user", "content": "请按照要求回答"}],
                gen_conf={"temperature": 0.3, "max_tokens": 1000}
            )

            # 计算置信度（基于检索结果的平均分数）
            avg_score = sum(s['score'] for s in sources) / len(sources) if sources else 0.0
            confidence = min(avg_score * 1.2, 1.0)  # 稍微提升置信度但不超过1.0

            return {
                "answer": answer.strip(),
                "sources": sources,
                "confidence": confidence
            }

        except Exception as e:
            logger.error(f"Error generating RAG answer: {e}")
            return {
                "answer": f"生成回答时发生错误：{str(e)}",
                "sources": [],
                "confidence": 0.0
            }

# 在RAGService类后面添加强化版的评分服务

class LLMScoringServiceV2:
    """LLM驱动的评分服务V2 - 强化正则提取能力"""

    def __init__(self):
        # V2强化版提示模板 - 更规范化的输出要求，专门优化处理表结构数据
        self.prompt_template_v2 = """你是一个专业的教师考核评分助手。请根据提供的评分规则和数据，计算出准确的评分结果。

评分规则：
{rule_description}

数据说明：
提供的数据包含多个表的信息，每个表都有以下结构：
- table: 表的元数据信息（表名、描述、字段结构）
- data_details: 表中的具体数据记录

数据内容：
{data_json}

上下文信息：
{context_info}

数据分析指导：
1. 请仔细分析每个表的结构和用途（table_desc字段）
2. 理解字段含义（column_desc字段）
3. 根据具体数据记录（data_details）和评分规则进行计算
4. 如果数据不足以支持精确计算，请明确说明并给出合理推断

请严格按照以下格式输出评分结果（格式非常重要，请勿更改）：

=== 最终评分结果 ===
总得分：[数字]分
评分状态：[完成/部分完成/数据不足/无法评分]
数据完整性：[完整/部分/不足]

=== 详细评分分析 ===
1. 规则匹配：
   - 匹配规则：[说明匹配到的具体规则条目]
   - 计算依据：[详细的计算过程，包括数值来源]
   - 数据映射：[说明如何从数据中提取计算所需的值]

2. 分数计算：
   - 基础得分：[数字]分 [计算过程]
   - 调整因子：[如有调整，说明原因]
   - 最终得分：[数字]分

=== 数据汇总统计 ===
- 涉及表数量：[数字]个
- 总记录数：[数字]条
- 关键指标提取：[列出从数据中提取的关键数值]
- 数据质量评估：[评估数据是否充分支持评分]

=== 改进建议 ===
[基于评分结果和数据完整性给出的改进建议，如需要补充哪些数据等]

重要要求：
1. "总得分"后面必须紧跟具体的数字
2. 数字必须是基于数据的准确计算结果
3. 如果数据不足，请在"评分状态"中明确标注
4. 详细说明数据到分数的映射过程
5. 保持格式严格一致性
6. 数字后面可以加"分"作为单位"""

    def calculate_score_v2(
            self,
            db: Session,
            user_input: str,
            rule_description: str,
            data: list[dict[str, Any]],
            context: dict[str, Any] | None = None,
            tenant_id: str = "",
            llm_name: str | None = None,
            enable_multi_extraction: bool = True,
            score_validation: bool = True,
            expected_score_range: tuple[float, float] | None = None,
            extraction_confidence_threshold: float = 0.8
    ) -> dict[str, Any]:
        """使用LLM计算评分 - V2强化版"""
        try:
            if not data:
                return self._create_empty_data_response()

            # 准备数据
            data_json = json.dumps(data, ensure_ascii=False, indent=2)
            context_info = json.dumps(context or {}, ensure_ascii=False, indent=2)

            # 构建V2强化版提示词
            prompt = self.prompt_template_v2.format(
                rule_description=rule_description,
                data_json=data_json,
                context_info=context_info
            )

            # 调用LLM
            chat_model = LLMBundle(db, tenant_id, LLMType.CHAT.value, llm_name)
            response = chat_model.chat(
                system=prompt,
                history=[{"role": "user", "content": f"{user_input}\n请严格按照格式要求进行评分"}],
                gen_conf={"temperature": 0.1, "max_tokens": 8000}
            )

            # V2强化版响应解析
            parsed_result = self._parse_score_response_v2(
                response=response,
                original_data=data,
                enable_multi_extraction=enable_multi_extraction,
                score_validation=score_validation,
                expected_score_range=expected_score_range,
                extraction_confidence_threshold=extraction_confidence_threshold
            )

            return parsed_result

        except Exception as e:
            logger.error(f"Error calculating score with LLM V2: {e}")
            return self._create_error_response_v2(data, str(e))

    def _parse_score_response_v2(
            self, 
            response: str, 
            original_data: list[dict[str, Any]],
            enable_multi_extraction: bool = True,
            score_validation: bool = True,
            expected_score_range: tuple[float, float] | None = None,
            extraction_confidence_threshold: float = 0.8
    ) -> dict[str, Any]:
        """V2强化版响应解析"""
        try:
            extraction_results = []
            extraction_methods = []
            
            # 策略1：优先从"总得分"部分提取 - 最高优先级
            score_from_final = self._extract_score_from_final_section(response)
            if score_from_final is not None:
                extraction_results.append(score_from_final)
                extraction_methods.append("final_section_primary")
            
            # 策略2：从分数计算部分提取"最终得分"
            score_from_calculation = self._extract_score_from_calculation_section(response)
            if score_from_calculation is not None:
                extraction_results.append(score_from_calculation)
                extraction_methods.append("calculation_section")
            
            if enable_multi_extraction:
                # 策略3：增强版正则表达式提取
                scores_from_regex = self._extract_scores_with_enhanced_regex(response)
                extraction_results.extend(scores_from_regex)
                extraction_methods.extend(["enhanced_regex"] * len(scores_from_regex))
                
                # 策略4：数字序列分析
                scores_from_sequence = self._extract_scores_from_number_sequence(response)
                extraction_results.extend(scores_from_sequence)
                extraction_methods.extend(["number_sequence"] * len(scores_from_sequence))

            # 去重并排序
            unique_scores = list(dict.fromkeys(extraction_results))  # 保持顺序去重
            
            # 选择最佳分数
            best_score, confidence, method = self._select_best_score(
                unique_scores, 
                extraction_methods,
                expected_score_range,
                score_validation
            )
            
            # 验证结果
            validation_results = self._validate_score(
                best_score, 
                expected_score_range, 
                original_data,
                response
            ) if score_validation else {"validated": True, "warnings": []}

            # 分段提取内容
            sections = self._extract_sections_v2(response)

            # 生成数据汇总
            data_summary = self._generate_data_summary_v2(original_data)

            # 计算提取置信度
            extraction_confidence = min(confidence, 1.0) if best_score is not None else 0.0

            return {
                "score": best_score,
                "score_text": sections.get("最终评分结果", "").strip(),
                "analysis": sections.get("详细评分分析", "").strip(),
                "suggestions": sections.get("改进建议", "").strip() or None,
                "data_summary": data_summary,
                "extraction_details": {
                    "all_extracted_scores": unique_scores,
                    "extraction_methods_used": list(set(extraction_methods)),
                    "total_extraction_attempts": len(extraction_results)
                },
                "confidence": extraction_confidence,
                "validation_results": validation_results,
                "alternative_scores": unique_scores[1:] if len(unique_scores) > 1 else [],
                "extraction_method": method,
                "raw_response": response
            }

        except Exception as e:
            logger.error(f"Error parsing score response V2: {e}")
            return self._create_parse_error_response_v2(response, original_data, str(e))

    def _extract_score_from_final_section(self, response: str) -> float | None:
        """从最终评分结果部分提取分数 - 最高优先级"""
        try:
            # 查找最终评分结果部分
            final_section_patterns = [
                r'=== 最终评分结果 ===.*?总得分[：:]?\s*([0-9]+\.?[0-9]*)\s*分?',
                r'总得分[：:]?\s*([0-9]+\.?[0-9]*)\s*分',
                r'最终得分[：:]?\s*([0-9]+\.?[0-9]*)\s*分',
                r'评分结果[：:]?\s*([0-9]+\.?[0-9]*)\s*分'
            ]
            
            for pattern in final_section_patterns:
                match = re.search(pattern, response, re.DOTALL | re.IGNORECASE)
                if match:
                    score = float(match.group(1))
                    logger.info(f"从最终评分结果部分提取到分数: {score}")
                    return score
            
            return None
        except (ValueError, AttributeError) as e:
            logger.warning(f"从最终评分结果部分提取分数失败: {e}")
            return None

    def _extract_score_from_calculation_section(self, response: str) -> float | None:
        """从分数计算部分提取最终得分"""
        try:
            calculation_patterns = [
                r'最终得分[：:]?\s*([0-9]+\.?[0-9]*)\s*分',
                r'总计[：:]?\s*([0-9]+\.?[0-9]*)\s*分',
                r'合计得分[：:]?\s*([0-9]+\.?[0-9]*)\s*分'
            ]
            
            # 只在分数计算部分查找
            calc_section_match = re.search(r'分数计算[：:]?(.*?)(?===|$)', response, re.DOTALL | re.IGNORECASE)
            search_text = calc_section_match.group(1) if calc_section_match else response
            
            for pattern in calculation_patterns:
                match = re.search(pattern, search_text, re.IGNORECASE)
                if match:
                    score = float(match.group(1))
                    logger.info(f"从分数计算部分提取到分数: {score}")
                    return score
            
            return None
        except (ValueError, AttributeError) as e:
            logger.warning(f"从分数计算部分提取分数失败: {e}")
            return None

    def _extract_scores_with_enhanced_regex(self, response: str) -> list[float]:
        """使用增强版正则表达式提取分数"""
        scores = []
        
        # 增强版正则表达式模式 - 覆盖更多表达方式
        enhanced_patterns = [
            # 中文表达
            r'得分[为是：:]?\s*([0-9]+\.?[0-9]*)\s*分',
            r'分数[为是：:]?\s*([0-9]+\.?[0-9]*)\s*分?',
            r'评分[为是：:]?\s*([0-9]+\.?[0-9]*)\s*分',
            r'总分[为是：:]?\s*([0-9]+\.?[0-9]*)\s*分?',
            r'([0-9]+\.?[0-9]*)\s*分(?![a-zA-Z])',  # 数字+分，但后面不跟字母
            
            # 英文表达
            r'score[:\s]*([0-9]+\.?[0-9]*)',
            r'points?[:\s]*([0-9]+\.?[0-9]*)',
            r'total[:\s]*([0-9]+\.?[0-9]*)',
            
            # 计算表达式
            r'=\s*([0-9]+\.?[0-9]*)\s*分?',
            r'共计\s*([0-9]+\.?[0-9]*)\s*分?',
            r'累计\s*([0-9]+\.?[0-9]*)\s*分?'
        ]
        
        for pattern in enhanced_patterns:
            matches = re.finditer(pattern, response, re.IGNORECASE)
            for match in matches:
                try:
                    score = float(match.group(1))
                    if score not in scores:  # 避免重复
                        scores.append(score)
                        logger.debug(f"增强正则提取到分数: {score} (模式: {pattern})")
                except ValueError:
                    continue
        
        return scores

    def _extract_scores_from_number_sequence(self, response: str) -> list[float]:
        """从数字序列中分析提取分数"""
        scores = []
        
        # 查找所有数字
        number_pattern = r'\b([0-9]+\.?[0-9]*)\b'
        numbers = [float(m.group(1)) for m in re.finditer(number_pattern, response)]
        
        # 过滤合理的分数范围 (通常考核分数在0-100000之间)
        reasonable_scores = [n for n in numbers if 0 <= n <= 100000]
        
        # 查找重复出现的数字（可能是最终答案）
        from collections import Counter
        number_counts = Counter(reasonable_scores)
        
        # 优先选择出现频率较高的数字
        for number, count in number_counts.most_common():
            if count >= 2 and number not in scores:  # 至少出现2次
                scores.append(number)
                logger.debug(f"数字序列分析提取到分数: {number} (出现{count}次)")
        
        return scores[:3]  # 最多返回3个候选分数

    def _select_best_score(
            self, 
            scores: list[float], 
            methods: list[str],
            expected_range: tuple[float, float] | None = None,
            validation_enabled: bool = True
    ) -> tuple[float | None, float, str]:
        """选择最佳分数"""
        if not scores:
            return None, 0.0, "no_extraction"
        
        # 方法优先级（越小优先级越高）
        method_priority = {
            "final_section_primary": 1,
            "calculation_section": 2,
            "enhanced_regex": 3,
            "number_sequence": 4
        }
        
        # 为每个分数计算置信度
        score_confidences = []
        for i, score in enumerate(scores):
            method = methods[i] if i < len(methods) else "unknown"
            
            # 基础置信度（基于提取方法）
            base_confidence = {
                "final_section_primary": 0.95,
                "calculation_section": 0.85,
                "enhanced_regex": 0.7,
                "number_sequence": 0.6
            }.get(method, 0.5)
            
            # 范围验证加分
            if expected_range and expected_range[0] <= score <= expected_range[1]:
                base_confidence += 0.1
            
            # 合理性检查
            if 0 <= score <= 100000:  # 合理的分数范围
                base_confidence += 0.05
            
            score_confidences.append((score, base_confidence, method))
        
        # 按置信度排序，置信度相同时按方法优先级排序
        score_confidences.sort(key=lambda x: (-x[1], method_priority.get(x[2], 99)))
        
        best_score, confidence, method = score_confidences[0]
        
        logger.info(f"选择最佳分数: {best_score} (置信度: {confidence:.2f}, 方法: {method})")
        return best_score, confidence, method

    def _validate_score(
            self, 
            score: float | None, 
            expected_range: tuple[float, float] | None,
            original_data: list[dict[str, Any]],
            response: str
    ) -> dict[str, Any]:
        """验证分数的合理性"""
        validation_result = {
            "validated": True,
            "warnings": [],
            "errors": []
        }
        
        if score is None:
            validation_result["validated"] = False
            validation_result["errors"].append("未能提取到有效分数")
            return validation_result
        
        # 基本范围检查
        if score < 0:
            validation_result["warnings"].append(f"分数为负数: {score}")
        elif score > 100000:
            validation_result["warnings"].append(f"分数异常偏高: {score}")
        
        # 期望范围检查
        if expected_range:
            min_score, max_score = expected_range
            if not (min_score <= score <= max_score):
                validation_result["warnings"].append(
                    f"分数 {score} 超出期望范围 [{min_score}, {max_score}]"
                )
        
        # 数据一致性检查
        data_count = len(original_data)
        if data_count == 0 and score > 0:
            validation_result["warnings"].append("数据为空但得分大于0")
        
        return validation_result

    def _extract_sections_v2(self, response: str) -> dict[str, str]:
        """V2版本的分段提取，支持新的格式"""
        sections = {}
        current_section = None
        current_content = []

        lines = response.split('\n')
        for line in lines:
            # 检查是否是V2格式的标题行
            if line.startswith('=== ') and line.endswith(' ==='):
                # 保存前一个部分
                if current_section:
                    sections[current_section] = '\n'.join(current_content).strip()

                # 开始新部分
                current_section = line[4:-4].strip()  # 去掉 "=== " 和 " ==="
                current_content = []
            elif line.startswith('## '):
                # 兼容V1格式
                if current_section:
                    sections[current_section] = '\n'.join(current_content).strip()
                current_section = line[3:].strip()
                current_content = []
            elif current_section:
                current_content.append(line)

        # 保存最后一个部分
        if current_section:
            sections[current_section] = '\n'.join(current_content).strip()

        return sections

    def _generate_data_summary_v2(self, data: list[dict[str, Any]]) -> dict[str, Any]:
        """V2版本的数据汇总，增加更多统计信息，专门处理表结构数据"""
        if not data:
            return {"total_records": 0, "version": "v2"}

        summary = {
            "total_records": len(data),
            "version": "v2"
        }

        # 检测数据结构类型
        has_table_structure = any(
            isinstance(item, dict) and "table" in item and "data_details" in item 
            for item in data
        )
        
        if has_table_structure:
            # 处理表结构数据
            summary["data_type"] = "structured_tables"
            tables_info = []
            total_data_records = 0
            all_fields = set()
            field_descriptions = {}
            
            for item in data:
                if "table" in item and "data_details" in item:
                    table_info = item["table"]
                    data_details = item["data_details"]
                    
                    table_summary = {
                        "table_name": table_info.get("table_name", "unknown"),
                        "table_desc": table_info.get("table_desc", ""),
                        "record_count": len(data_details) if isinstance(data_details, list) else 0,
                        "fields": []
                    }
                    
                    # 处理字段结构信息
                    if "structure" in table_info and isinstance(table_info["structure"], list):
                        for field_info in table_info["structure"]:
                            if isinstance(field_info, dict):
                                field_name = field_info.get("column_name", "")
                                field_desc = field_info.get("column_desc", "")
                                if field_name:
                                    table_summary["fields"].append({
                                        "name": field_name,
                                        "description": field_desc
                                    })
                                    all_fields.add(field_name)
                                    field_descriptions[field_name] = field_desc
                    
                    # 统计实际数据
                    if isinstance(data_details, list):
                        total_data_records += len(data_details)
                        # 统计每个字段的数据类型和非空情况
                        field_types = {}
                        field_non_empty_count = {}
                        
                        for record in data_details:
                            if isinstance(record, dict):
                                for field_name, value in record.items():
                                    if value is not None:
                                        field_types.setdefault(field_name, set()).add(type(value).__name__)
                                        field_non_empty_count[field_name] = field_non_empty_count.get(field_name, 0) + 1
                        
                        table_summary["field_stats"] = {
                            "field_types": {k: list(v) for k, v in field_types.items()},
                            "non_empty_counts": field_non_empty_count
                        }
                    
                    tables_info.append(table_summary)
            
            summary.update({
                "tables_count": len(tables_info),
                "total_data_records": total_data_records,
                "tables_info": tables_info,
                "all_fields": list(all_fields),
                "field_descriptions": field_descriptions
            })
            
            # 统计数值字段（跨所有表）
            numeric_stats = []
            for item in data:
                if "data_details" in item and isinstance(item["data_details"], list):
                    for record in item["data_details"]:
                        if isinstance(record, dict):
                            for field_name, value in record.items():
                                if isinstance(value, (int, float)):
                                    # 查找已存在的字段统计或创建新的
                                    existing_stat = next(
                                        (stat for stat in numeric_stats if stat["field"] == field_name), 
                                        None
                                    )
                                    if existing_stat:
                                        existing_stat["values"].append(value)
                                    else:
                                        numeric_stats.append({
                                            "field": field_name,
                                            "values": [value],
                                            "description": field_descriptions.get(field_name, "")
                                        })
            
            # 计算数值字段统计
            for stat in numeric_stats:
                values = stat["values"]
                if values:
                    stat.update({
                        "count": len(values),
                        "total": sum(values),
                        "average": sum(values) / len(values),
                        "max": max(values),
                        "min": min(values)
                    })
                    del stat["values"]  # 移除原始值列表以节省空间
            
            if numeric_stats:
                summary["numeric_stats"] = numeric_stats
                
        else:
            # 处理普通数据结构（保持原有逻辑）
            summary["data_type"] = "simple_records"
            summary["fields"] = list(data[0].keys()) if data else []

            # 统计每个字段的数据类型
            field_types = {}
            for field in summary["fields"]:
                types = set()
                for item in data:
                    value = item.get(field)
                    if value is not None:
                        types.add(type(value).__name__)
                field_types[field] = list(types)
            
            summary["field_types"] = field_types

            # 统计数值字段
            numeric_fields = []
            for field in summary["fields"]:
                values = [item.get(field) for item in data if isinstance(item.get(field), (int, float))]
                if values:
                    numeric_fields.append({
                        "field": field,
                        "count": len(values),
                        "total": sum(values),
                        "average": sum(values) / len(values),
                        "max": max(values),
                        "min": min(values)
                    })

            if numeric_fields:
                summary["numeric_stats"] = numeric_fields

            # 统计非空字段
            non_empty_counts = {}
            for field in summary["fields"]:
                non_empty_count = sum(1 for item in data if item.get(field) is not None and item.get(field) != "")
                non_empty_counts[field] = non_empty_count
            
            summary["non_empty_counts"] = non_empty_counts

        return summary

    def _create_empty_data_response(self) -> dict[str, Any]:
        """创建空数据响应"""
        return {
            "score": None,
            "score_text": "无数据可供评分",
            "analysis": "提供的数据为空，无法进行评分计算",
            "suggestions": "请提供有效的数据进行评分",
            "data_summary": {"total_records": 0, "version": "v2"},
            "extraction_details": {
                "all_extracted_scores": [],
                "extraction_methods_used": [],
                "total_extraction_attempts": 0
            },
            "confidence": 0.0,
            "validation_results": {"validated": False, "warnings": [], "errors": ["数据为空"]},
            "alternative_scores": [],
            "extraction_method": "no_data",
            "raw_response": ""
        }

    def _create_error_response_v2(self, data: list[dict[str, Any]], error_msg: str) -> dict[str, Any]:
        """创建V2错误响应"""
        return {
            "score": None,
            "score_text": f"评分计算失败：{error_msg}",
            "analysis": "系统在处理评分请求时发生错误",
            "suggestions": "请检查输入数据和规则描述，稍后重试",
            "data_summary": {"total_records": len(data), "error": error_msg, "version": "v2"},
            "extraction_details": {
                "all_extracted_scores": [],
                "extraction_methods_used": [],
                "total_extraction_attempts": 0
            },
            "confidence": 0.0,
            "validation_results": {"validated": False, "warnings": [], "errors": [error_msg]},
            "alternative_scores": [],
            "extraction_method": "error",
            "raw_response": ""
        }

    def _create_parse_error_response_v2(self, response: str, data: list[dict[str, Any]], error_msg: str) -> dict[str, Any]:
        """创建V2解析错误响应"""
        return {
            "score": None,
            "score_text": response,  # 返回原始响应
            "analysis": f"响应解析失败：{error_msg}",
            "suggestions": "请查看原始响应内容",
            "data_summary": {"total_records": len(data), "parse_error": error_msg, "version": "v2"},
            "extraction_details": {
                "all_extracted_scores": [],
                "extraction_methods_used": [],
                "total_extraction_attempts": 0
            },
            "confidence": 0.0,
            "validation_results": {"validated": False, "warnings": [], "errors": [f"解析错误: {error_msg}"]},
            "alternative_scores": [],
            "extraction_method": "parse_error",
            "raw_response": response
        }