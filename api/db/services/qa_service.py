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