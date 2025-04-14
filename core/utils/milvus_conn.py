# coding=utf-8
"""
@project: multirag
@Author：龙
@file： milvus_conn.py
@date：2024/7/31 10:12
@desc:
"""
import copy
import json
import logging
import os
import re
import time
from uuid import uuid4
from datetime import datetime

from pymilvus.client.constants import DEFAULT_CONSISTENCY_LEVEL
from pymilvus.client.types import ExceptionsMessage, LoadState
from pymilvus.client.utils import is_vector_type, get_params
from pymilvus.exceptions import (
    DataTypeNotMatchException,
    ErrorCode,
    MilvusException,
    ParamError,
    PrimaryKeyException,
    ServerVersionIncompatibleException,
)
from pymilvus.milvus_client.index import IndexParams
from pymilvus.orm import utility
from pymilvus.orm.collection import CollectionSchema, FieldSchema
from pymilvus.orm.connections import connections
from pymilvus.orm.types import DataType
from pymilvus import __version__

from api.utils.file_utils import get_project_base_directory
from core import settings
from core.settings import TAG_FLD, PAGERANK_FLD
from core.utils import singleton
from core.utils.doc_store_conn import (
    DocStoreConnection,
    MatchExpr,
    MatchTextExpr,
    MatchDenseExpr,
    MatchSparseExpr,
    MatchTensorExpr,
    FusionExpr,
    OrderByExpr,
)

logger = logging.getLogger('multirag.milvus_conn')

ATTEMPT_TIME = 2


# logger.info("Milvus version: " + str(__version__))

@singleton
class MilvusConnection(DocStoreConnection):
    def __init__(self):
        uri = settings.MILVUS.get("hosts", "http://localhost:19530")
        user = settings.MILVUS.get("username", "")
        password = settings.MILVUS.get("password", "")
        db_name = settings.MILVUS.get("db_name", "")
        token = settings.MILVUS.get("token", "")
        timeout = settings.MILVUS.get("timeout", None)

        self._using = self._create_connection(
            uri, user, password, db_name, token, timeout=timeout
        )
        self.is_self_hosted = bool(utility.get_server_type(using=self._using) == "milvus")
        logger.info(f"使用 Milvus {uri} 作为文档存储引擎")

        # 尝试连接
        for _ in range(ATTEMPT_TIME):
            try:
                conn = self._get_connection()
                version = utility.get_server_version(using=self._using)
                logger.info(f"Milvus {uri} 已连接，服务器版本: {version}")
                break
            except Exception as e:
                logger.warning(f"{str(e)}. 等待 Milvus {uri} 恢复健康状态...")
                time.sleep(5)

        # 检查连接情况
        try:
            server_status = self.health()
            if server_status["status"] != "green":
                msg = f"Milvus {uri} 不健康，状态: {server_status}"
                logger.error(msg)
                raise Exception(msg)
            logger.info(f"Milvus {uri} 健康状态良好")
        except Exception as e:
            msg = f"Milvus {uri} 连接检查失败: {str(e)}"
            logger.error(msg)
            raise Exception(msg)

    """
        数据库操作
        """

    def dbType(self) -> str:
        return "milvus"

    def health(self) -> dict:
        try:
            version = utility.get_server_version(using=self._using)
            return {"type": "milvus", "status": "green", "version": version}
        except MilvusException as e:
            return {"type": "milvus", "status": "red", "error": str(e)}

    """
    表操作
    """

    def createIdx(self, indexName: str, knowledgebaseId: list[str], vectorSize: int):
        """
        创建索引（对应于创建Milvus集合），根据提供的mapping文件定义字段

        Args:
            indexName: 索引名称前缀
            knowledgebaseId: 知识库ID
            vectorSize: 向量维度

        Returns:
            bool: 创建成功返回True
        """
        # collection_name = f"{indexName}_{knowledgebaseId}"
        collection_name = indexName

        try:
            # 检查集合是否已存在
            if self.indexExist(indexName, knowledgebaseId):
                logger.info(f"集合 {collection_name} 已存在")
                return True

            # 加载mapping定义
            mapping_path = os.path.join(get_project_base_directory(), "configs", "milvus_mapping.json")
            if not os.path.exists(mapping_path):
                logger.warning(f"未找到Milvus mapping文件，使用默认字段定义: {mapping_path}")
                # 使用默认字段定义创建集合
                return self._create_default_collection(indexName, knowledgebaseId, vectorSize)

            # 从JSON文件加载mapping定义
            with open(mapping_path, 'r', encoding='utf-8') as f:
                mapping = json.load(f)

            # 准备字段列表
            fields = []
            dynamic_templates = mapping.get("mappings", {}).get("dynamic_templates", [])

            # 设置向量维度，用于"auto"维度的字段
            auto_dimensions = {f"q_{vectorSize}_vec": vectorSize}

            # 先处理匹配正则表达式的模板
            regex_patterns = []
            for template in dynamic_templates:
                for key, value in template.items():
                    if value.get("match_pattern", "") == "regex":
                        regex_patterns.append((value.get("match", ""), value.get("mapping", {})))

            # 处理所有动态模板
            primary_field_added = False
            vector_field_added = False

            for template in dynamic_templates:
                for key, value in template.items():
                    match_pattern = value.get("match_pattern", "")
                    # 跳过正则匹配模式，稍后处理
                    if match_pattern == "regex":
                        continue

                    match = value.get("match", "")
                    mapping_type = value.get("mapping", {}).get("type", "")

                    # 主键字段
                    if value.get("mapping", {}).get("is_primary", False):
                        max_length = value.get("mapping", {}).get("max_length", 512)
                        fields.append(FieldSchema(
                            name=match,
                            dtype=DataType.VARCHAR,
                            max_length=max_length,
                            is_primary=True
                        ))
                        primary_field_added = True
                        continue

                    # 向量字段
                    if mapping_type == "FLOAT_VECTOR":
                        dims = value.get("mapping", {}).get("dims", vectorSize)
                        if dims == "auto":
                            if match in auto_dimensions:
                                dims = auto_dimensions[match]
                            else:
                                dims = vectorSize

                        fields.append(FieldSchema(
                            name=match,
                            dtype=DataType.FLOAT_VECTOR,
                            dim=dims
                        ))
                        vector_field_added = True
                        continue

                    # 其他类型的字段
                    if mapping_type == "VARCHAR":
                        max_length = value.get("mapping", {}).get("max_length", 256)
                        fields.append(FieldSchema(
                            name=match,
                            dtype=DataType.VARCHAR,
                            max_length=max_length
                        ))

                    elif mapping_type == "FLOAT":
                        fields.append(FieldSchema(
                            name=match,
                            dtype=DataType.FLOAT
                        ))

                    elif mapping_type == "INT64":
                        fields.append(FieldSchema(
                            name=match,
                            dtype=DataType.INT64
                        ))

                    elif mapping_type == "JSON":
                        fields.append(FieldSchema(
                            name=match,
                            dtype=DataType.JSON
                        ))

                    elif mapping_type == "ARRAY":
                        element_type = value.get("mapping", {}).get("element_type", "VARCHAR")
                        max_length = value.get("mapping", {}).get("max_length", 256)
                        max_capacity = value.get("mapping", {}).get("max_capacity", 100)

                        element_data_type = getattr(DataType, element_type) if isinstance(element_type,
                                                                                          str) else element_type

                        fields.append(FieldSchema(
                            name=match,
                            dtype=DataType.ARRAY,
                            element_type=element_data_type,
                            max_length=max_length,
                            max_capacity=max_capacity
                        ))

            # 处理特定维度的向量字段
            if not vector_field_added:
                fields.append(FieldSchema(
                    name=f"q_{vectorSize}_vec",
                    dtype=DataType.FLOAT_VECTOR,
                    dim=vectorSize
                ))

            # 确保有主键字段
            if not primary_field_added:
                fields.append(FieldSchema(
                    name="id",
                    dtype=DataType.VARCHAR,
                    max_length=512,
                    is_primary=True
                ))

            # 确保有PageRank字段
            if not any(field.name == PAGERANK_FLD for field in fields):
                fields.append(FieldSchema(
                    name=PAGERANK_FLD,
                    dtype=DataType.FLOAT
                ))

            # 创建集合模式
            schema = CollectionSchema(
                fields=fields,
                description=f"Collection for {indexName} with {knowledgebaseId}",
                enable_dynamic_field=True
            )

            # 创建集合
            conn = self._get_connection()
            conn.create_collection(
                collection_name,
                schema,
                consistency_level=DEFAULT_CONSISTENCY_LEVEL
            )

            # 为向量字段创建索引
            for field in fields:
                if field.dtype == DataType.FLOAT_VECTOR:
                    index_params = {
                        "index_type": "IVF_FLAT",
                        "metric_type": "COSINE",
                        "params": {"nlist": 1024}
                    }
                    conn.create_index(
                        collection_name,
                        field.name,
                        index_params
                    )

            # 加载集合
            conn.load_collection(collection_name)
            logger.info(f"成功创建集合 {collection_name}，向量维度 {vectorSize}")
            return True

        except Exception as e:
            logger.error(f"创建集合 {collection_name} 失败: {str(e)}")
            raise e

    def _create_default_collection(self, indexName: str, knowledgebaseId: list[str], vectorSize: int):
        """
        使用默认字段定义创建集合（当找不到mapping文件时使用）

        Args:
            indexName: 索引名称前缀
            knowledgebaseId: 知识库ID
            vectorSize: 向量维度

        Returns:
            bool: 创建成功返回True
        """
        # collection_name = f"{indexName}_{knowledgebaseId}"
        collection_name = indexName

        # 创建包含基本字段的模式
        fields = []

        # 添加ID字段 (主键)
        fields.append(FieldSchema(
            name="id",
            dtype=DataType.VARCHAR,
            is_primary=True,
            max_length=512
        ))

        # 添加向量字段
        fields.append(FieldSchema(
            name=f"q_{vectorSize}_vec",
            dtype=DataType.FLOAT_VECTOR,
            dim=vectorSize
        ))

        # 添加其他常用字段
        fields.append(FieldSchema(name="kb_id", dtype=DataType.VARCHAR, max_length=128))
        fields.append(FieldSchema(name="available_int", dtype=DataType.INT64))
        fields.append(FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=65535))
        fields.append(FieldSchema(name="text_tks", dtype=DataType.VARCHAR, max_length=65535))
        fields.append(FieldSchema(name="create_time", dtype=DataType.VARCHAR, max_length=64))
        fields.append(FieldSchema(name="create_timestamp_flt", dtype=DataType.FLOAT))

        # 对于数组类型的字段，使用VARCHAR暂存（Milvus处理特殊字段的方式）
        fields.append(FieldSchema(name="important_kwd", dtype=DataType.VARCHAR, max_length=4096))
        fields.append(FieldSchema(name="question_kwd", dtype=DataType.VARCHAR, max_length=4096))

        # JSON或复杂类型字段
        fields.append(FieldSchema(name="position_int", dtype=DataType.JSON))
        fields.append(FieldSchema(name="page_num_int", dtype=DataType.JSON))
        fields.append(FieldSchema(name="top_int", dtype=DataType.JSON))

        # PageRank字段
        fields.append(FieldSchema(name=PAGERANK_FLD, dtype=DataType.FLOAT))
        fields.append(FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=128))

        # 创建集合模式
        schema = CollectionSchema(
            fields=fields,
            description=f"Default collection for {indexName} with {knowledgebaseId}",
            enable_dynamic_field=True
        )

        # 创建集合
        conn = self._get_connection()
        conn.create_collection(
            collection_name,
            schema,
            consistency_level=DEFAULT_CONSISTENCY_LEVEL
        )

        # 为向量字段创建索引
        conn.create_index(
            collection_name,
            f"q_{vectorSize}_vec",
            {
                "index_type": "IVF_FLAT",
                "metric_type": "COSINE",
                "params": {"nlist": 1024}
            }
        )

        # 加载集合
        conn.load_collection(collection_name)
        logger.info(f"成功创建集合 {collection_name}（使用默认字段），向量维度 {vectorSize}")
        return True

    def deleteIdx(self, indexName: str, knowledgebaseId: list[str]):
        """删除索引，对应于删除Milvus集合"""
        # collection_name = f"{indexName}_{knowledgebaseId}"
        collection_name = indexName
        try:
            conn = self._get_connection()
            if conn.has_collection(collection_name):
                conn.drop_collection(collection_name)
                logger.info(f"成功删除集合 {collection_name}")
            return True
        except Exception as e:
            logger.error(f"删除集合 {collection_name} 失败: {str(e)}")
            raise e

    def indexExist(self, indexName: str, knowledgebaseId: list[str]) -> bool:
        """检查索引是否存在，对应于检查Milvus集合是否存在"""
        # collection_name = f"{indexName}_{knowledgebaseId}"
        collection_name = indexName
        try:
            conn = self._get_connection()
            return conn.has_collection(collection_name)
        except Exception as e:
            logger.warning(f"检查集合 {collection_name} 是否存在失败: {str(e)}")
            return False

    def insert(self, rows: list[dict], indexName: str, knowledgebaseId: list[str] | None = None) -> list[str]:
        """插入数据"""
        # collection_name = f"{indexName}_{knowledgebaseId}"
        collection_name = indexName
        conn = self._get_connection()

        # 检查集合是否存在
        if not conn.has_collection(collection_name):
            # 尝试确定向量维度
            vector_size = 0
            pattern = re.compile(r"q_(\d+)_vec")

            for row in rows:
                for key in row.keys():
                    match = pattern.match(key)
                    if match:
                        vector_size = int(match.group(1))
                        break
                if vector_size > 0:
                    break

            if vector_size == 0:
                raise ValueError(f"无法从数据中确定向量维度，无法创建集合 {collection_name}")

            # 创建集合
            self.createIdx(indexName, knowledgebaseId, vector_size)

        # 预处理数据
        processed_rows = []
        for row in rows:
            # 创建副本避免修改原始数据
            new_row = copy.deepcopy(row)

            # 处理特殊字段
            if "kb_id" in new_row and isinstance(new_row["kb_id"], list):
                new_row["kb_id"] = new_row["kb_id"][0] if new_row["kb_id"] else ""

            # 处理关键词字段
            for kwd_field in ["important_kwd", "question_kwd", "entities_kwd"]:
                if kwd_field in new_row and isinstance(new_row[kwd_field], list):
                    # Milvus 2.x不支持字符串数组，转换为分隔符连接的字符串
                    new_row[kwd_field] = "###".join(new_row[kwd_field])

            # 处理位置信息字段
            if "position_int" in new_row and isinstance(new_row["position_int"], list):
                # 将嵌套数组展平并转换为十六进制字符串
                flat_array = [num for row in new_row["position_int"] for num in row]
                new_row["position_int"] = "_".join(f"{num:08x}" for num in flat_array)

            # 处理页码和顶部位置字段
            for pos_field in ["page_num_int", "top_int"]:
                if pos_field in new_row and isinstance(new_row[pos_field], list):
                    new_row[pos_field] = "_".join(f"{num:08x}" for num in new_row[pos_field])

            processed_rows.append(new_row)

        # 执行插入操作
        try:
            # 对于已存在的记录，先尝试删除
            ids = ["'{}'".format(row["id"]) for row in processed_rows]
            if ids:
                id_filter = f"id in [{','.join(ids)}]"
                try:
                    conn.delete(collection_name, expression=id_filter)
                except Exception as e:
                    logger.debug(f"删除现有记录失败 {collection_name}: {str(e)}")

            # 插入新记录
            res = conn.insert_rows(collection_name, processed_rows)
            logger.debug(f"成功插入 {res.insert_count} 条记录到 {collection_name}")
            return []  # 成功返回空列表表示没有错误
        except Exception as e:
            error_msg = str(e)
            logger.error(f"插入到 {collection_name} 失败: {error_msg}")
            return [error_msg]  # 返回错误信息

    def get(self, chunkId: str, indexName: str, knowledgebaseIds: list[str]) -> dict | None:
        """获取单个文档块"""
        # for kb_id in knowledgebaseIds:
        #     collection_name = f"{indexName}_{kb_id}"
        #     conn = self._get_connection()
        collection_name = indexName
        conn = self._get_connection()
        try:
            if not conn.has_collection(collection_name):
                return None

            filter_expr = f"id == '{chunkId}'"
            results = conn.query(collection_name, filter_expr, output_fields=["*"])

            if results and len(results) > 0:
                # 处理特殊字段
                result = results[0]

                # 处理关键词字段
                for kwd_field in ["important_kwd", "question_kwd", "entities_kwd"]:
                    if kwd_field in result and isinstance(result[kwd_field], str):
                        result[kwd_field] = result[kwd_field].split("###") if result[kwd_field] else []

                # 处理位置信息字段
                if "position_int" in result and isinstance(result["position_int"], str):
                    if result["position_int"]:
                        arr = [int(hex_val, 16) for hex_val in result["position_int"].split('_')]
                        result["position_int"] = [arr[i:i + 5] for i in range(0, len(arr), 5)]
                    else:
                        result["position_int"] = []

                # 处理页码和顶部位置字段
                for pos_field in ["page_num_int", "top_int"]:
                    if pos_field in result and isinstance(result[pos_field], str):
                        if result[pos_field]:
                            result[pos_field] = [int(hex_val, 16) for hex_val in result[pos_field].split('_')]
                        else:
                            result[pos_field] = []

                return result
        except Exception as e:
            logger.warning(f"从 {collection_name} 获取文档 {chunkId} 失败: {str(e)}")

        return None

    """
    CRUD 操作
    """

    def search(
            self, selectFields: list[str],  # 要返回的字段列表
            highlightFields: list[str],  # 要高亮的字段列表（Milvus不直接支持）
            condition: dict,  # 过滤条件
            matchExprs: list[MatchExpr],  # 匹配表达式列表（如向量查询）
            orderBy: OrderByExpr,  # 排序表达式
            offset: int,  # 分页偏移量
            limit: int,  # 结果数量限制
            indexNames: str | list[str],  # 索引名称（集合名）
            knowledgebaseIds: list[str],  # 知识库ID列表(ps：我们这边统一用kb_names,不用ID)
            aggFields: list[str] = [],  # 聚合字段（Milvus不直接支持）
            rank_feature: dict | None = None  # 排名特征调整
    ):
        """
        执行搜索操作，支持向量搜索和条件过滤
        """
        if isinstance(indexNames, str):
            indexNames = indexNames.split(",")
        assert isinstance(indexNames, list) and len(indexNames) > 0

        # 准备返回结果
        all_results = []
        total_hits_count = 0

        # 构建过滤条件
        filter_expr = ""
        if condition:
            filter_parts = []
            for k, v in condition.items():
                if k == "pk" or not v:
                    continue
                if k == "doc_id":
                    if isinstance(v, list):
                        kb_exprs = [f"doc_id == '{kb}'" for kb in v]
                        filter_parts.append(f"({' || '.join(kb_exprs)})")
                    else:
                        filter_parts.append(f"doc_id == '{v}'")
                elif k == "available_int":
                    filter_parts.append(f"available_int != {v-1}") # 为了兼容老版本不存在available_int字段才这么写
                elif k == "auth":
                    filter_parts.append(f"{v}")
                elif isinstance(v, list):
                    values = [f"'{item}'" if isinstance(item, str) else str(item) for item in v]
                    filter_parts.append(f"{k} in [{','.join(values)}]")
                elif isinstance(v, str):
                    filter_parts.append(f"{k} == '{v}'")
                elif isinstance(v, (int, float)):
                    filter_parts.append(f"{k} == {v}")

            if filter_parts:
                filter_expr = " && ".join(filter_parts)

        # 提取搜索参数
        search_params = {"metric_type": "COSINE"}
        vector_data = None
        vector_field = None
        vector_similarity_weight = 0.5

        # 处理融合参数，类似 es_conn.py 中的处理
        for m in matchExprs:
            if isinstance(m, FusionExpr) and m.method == "weighted_sum" and "weights" in m.fusion_params:
                # 确认是文本+向量的融合搜索
                if len(matchExprs) == 3 and isinstance(matchExprs[0], MatchTextExpr) and isinstance(matchExprs[1],
                                                                                                    MatchDenseExpr) and isinstance(
                        matchExprs[2], FusionExpr):
                    weights = m.fusion_params["weights"]
                    vector_similarity_weight = float(weights.split(",")[1])

        # 处理不同类型的匹配表达式
        for expr in matchExprs:
            # todo if isinstance(expr, MatchTextExpr) 使用milvus的全文检索特性
            # if isinstance(expr, MatchTextExpr):
            #     # 处理文本搜索
            #     text_query = expr.matching_text
            #     text_fields = expr.fields
            #     if "minimum_should_match" in expr.extra_options:
            #         minimum_should_match = expr.extra_options["minimum_should_match"]
            #         if isinstance(minimum_should_match, float):
            #             minimum_should_match = str(int(minimum_should_match * 100)) + "%"
            #     # 将文本查询条件添加到过滤表达式中
            #     if text_query and text_fields:
            #         text_conditions = []
            #         for field in text_fields:
            #             text_conditions.append(f"match_phrase({field}, '{text_query}')")
            #         if text_conditions:
            #             text_filter = " || ".join(text_conditions)
            #             if filter_expr:
            #                 filter_expr = f"({filter_expr}) && ({text_filter})"
            #             else:
            #                 filter_expr = text_filter
            #
            #     logger.debug(f"文本搜索条件: {text_query}, 字段: {text_fields}, 最小匹配率: {minimum_should_match}")

            if isinstance(expr, MatchDenseExpr):
                vector_data = expr.embedding_data
                vector_field = expr.vector_column_name
                # 从额外选项中获取设置
                if "similarity" in expr.extra_options:
                    search_params["similarity"] = expr.extra_options["similarity"]

        # 如果存在 rank_feature，添加到查询参数中
        # Milvus 不支持直接的 rank_feature，但我们可以在后处理中模拟
        rank_boost = {}
        if rank_feature:
            for field, score in rank_feature.items():
                if field != PAGERANK_FLD:
                    field = f"{TAG_FLD}.{field}"
                rank_boost[field] = score

        if not vector_data:
            # 只有条件过滤，没有向量搜索
            for indexName in indexNames:
                collection_name = indexName
                try:
                    conn = self._get_connection()
                    if not conn.has_collection(collection_name):
                        continue

                    # 执行条件查询
                    results = conn.query(
                        collection_name,
                        filter_expr,
                        output_fields=selectFields,
                        # limit=limit
                    )

                    if results:
                        # 应用 rank_feature 排序（如果有）
                        if rank_boost and PAGERANK_FLD in selectFields:
                            # 添加得分字段
                            for result in results:
                                pagerank = float(result.get(PAGERANK_FLD, 0))
                                result["SCORE"] = pagerank * rank_boost.get(PAGERANK_FLD, 1.0)

                            # 按分数排序
                            results = sorted(results, key=lambda x: x.get("SCORE", 0), reverse=True)

                        all_results.extend(results)
                        total_hits_count += len(results)
                except Exception as e:
                    logger.warning(f"查询集合 {collection_name} 失败: {str(e)}")

            # 构建返回结果
            if all_results:
                # 应用偏移和限制
                if offset > 0:
                    all_results = all_results[offset:]
                if limit > 0:
                    all_results = all_results[:limit]

                return all_results, total_hits_count
            else:
                # 返回空结果
                return [], 0

        else:
            # 向量搜索
            for indexName in indexNames:
                # collection_name = f"{indexName}_{knowledgebaseId}"
                collection_name = indexName
                try:
                    conn = self._get_connection()
                    if not conn.has_collection(collection_name):
                        continue

                    # 准备搜索参数
                    search_data = [vector_data]

                    # 执行向量搜索
                    results = conn.search(
                        collection_name,
                        search_data,
                        vector_field,
                        search_params,
                        expression=filter_expr,
                        output_fields=selectFields,
                        limit=limit
                    )

                    # 处理搜索结果
                    if results and results[0]:
                        hit_results = []
                        for hit in results[0]:
                            hit_dict = hit.to_dict()
                            # 将距离信息添加到结果中
                            # base_score = 1.0 - hit_dict.get("distance", 0)
                            base_score = hit_dict.get("distance", 0) # 我们目前用的是consin，[-1,1],值越大越准
                            hit_dict["SCORE"] = base_score

                            # 应用 rank_feature 调整分数
                            if rank_boost and PAGERANK_FLD in hit_dict["entity"]:
                                pagerank = float(hit_dict["entity"].get(PAGERANK_FLD, 0))
                                # 应用向量相似度权重和PageRank权重
                                # todo 原始 ES 实现可能同时包含了全文检索和向量搜索，而在那种情况下，(1.0 - vector_similarity_weight) 部分可能是分配给全文检索得分的权重。但在我们目前的 Milvus 实现中，由于没有实现全文检索部分，这个权重被分配给了 PageRank，后续再调整。
                                hit_dict["SCORE"] = (base_score * vector_similarity_weight +
                                                     pagerank * rank_boost.get(PAGERANK_FLD, 1.0) * (
                                                                 1.0 - vector_similarity_weight))

                            hit_results.append(hit_dict)

                        all_results.extend(hit_results)
                        total_hits_count += len(hit_results)
                except Exception as e:
                    logger.warning(f"搜索集合 {collection_name} 失败: {str(e)}")

            # 应用排序
            if all_results:
                if matchExprs:
                    # 按分数降序排序
                    all_results.sort(key=lambda x: x.get("SCORE", 0), reverse=True)

                # 如果有自定义排序，应用它
                if orderBy and orderBy.fields:
                    for field, order in reversed(orderBy.fields):
                        reverse_sort = (order == 1)  # 1表示降序
                        all_results.sort(key=lambda x: x.get(field), reverse=reverse_sort)

                    # 应用偏移和限制
                paginated_results = all_results[offset:offset + limit]

                return paginated_results, total_hits_count
            else:
                # 返回空结果
                return [], 0

    # todo 暂时不需要支持更复杂的更新逻辑（如批量更新、复杂条件查询以及特殊字段格式转换）
    # def update(self, condition: dict, newValue: dict, indexName: str, knowledgebaseId: list[str]) -> bool:
    #     """更新文档，Milvus不支持原地更新，使用查询+删除+重新插入实现"""
    #     # collection_name = f"{indexName}_{knowledgebaseId}"
    #     collection_name = indexName
    #     conn = self._get_connection()
    #
    #     if not conn.has_collection(collection_name):
    #         logger.error(f"集合 {collection_name} 不存在")
    #         return False
    #
    #     # 构建查询条件
    #     filter_parts = []
    #     for k, v in condition.items():
    #         if k == "pk" or not v:
    #             continue
    #         if isinstance(v, list):
    #             values = [f"'{item}'" if isinstance(item, str) else str(item) for item in v]
    #             filter_parts.append(f"{k} in [{','.join(values)}]")
    #         elif isinstance(v, str):
    #             filter_parts.append(f"{k} == '{v}'")
    #         elif isinstance(v, (int, float)):
    #             filter_parts.append(f"{k} == {v}")
    #
    #     if not filter_parts:
    #         logger.error("更新操作需要有效的条件")
    #         return False
    #
    #     filter_expr = " && ".join(filter_parts)
    #
    #     # 查询现有记录
    #     try:
    #         existing_docs = conn.query(collection_name, filter_expr, output_fields=["*"])
    #         if not existing_docs:
    #             logger.error(f"没有找到符合条件的记录: {filter_expr}")
    #             return False
    #     except Exception as e:
    #         logger.error(f"查询现有记录失败: {str(e)}")
    #         return False
    #
    #     # 处理新值中的特殊字段
    #     processed_new_value = copy.deepcopy(newValue)
    #
    #     # 处理关键词字段
    #     for kwd_field in ["important_kwd", "question_kwd", "entities_kwd"]:
    #         if kwd_field in processed_new_value and isinstance(processed_new_value[kwd_field], list):
    #             processed_new_value[kwd_field] = "###".join(processed_new_value[kwd_field])
    #
    #     # 处理kb_id字段
    #     if "kb_id" in processed_new_value and isinstance(processed_new_value["kb_id"], list):
    #         processed_new_value["kb_id"] = processed_new_value["kb_id"][0] if processed_new_value["kb_id"] else ""
    #
    #     # 处理位置信息字段
    #     if "position_int" in processed_new_value and isinstance(processed_new_value["position_int"], list):
    #         # 将嵌套数组展平并转换为十六进制字符串
    #         flat_array = [num for row in processed_new_value["position_int"] for num in row]
    #         processed_new_value["position_int"] = "_".join(f"{num:08x}" for num in flat_array)
    #
    #     # 处理页码和顶部位置字段
    #     for pos_field in ["page_num_int", "top_int"]:
    #         if pos_field in processed_new_value and isinstance(processed_new_value[pos_field], list):
    #             processed_new_value[pos_field] = "_".join(f"{num:08x}" for num in processed_new_value[pos_field])
    #
    #     # 更新所有符合条件的记录
    #     updated_docs = []
    #     for doc in existing_docs:
    #         # 合并原始文档和新值
    #         updated_doc = {**doc, **processed_new_value}
    #         updated_docs.append(updated_doc)
    #
    #     # 删除旧记录
    #     try:
    #         delete_res = conn.delete(collection_name, expression=filter_expr)
    #         logger.debug(f"删除了 {delete_res.delete_count} 条记录")
    #     except Exception as e:
    #         logger.error(f"删除记录失败: {str(e)}")
    #         return False
    #
    #     # 插入更新后的记录
    #     try:
    #         insert_res = conn.insert_rows(collection_name, updated_docs)
    #         logger.debug(f"插入了 {insert_res.insert_count} 条更新后的记录")
    #         return True
    #     except Exception as e:
    #         logger.error(f"插入更新后的记录失败: {str(e)}")
    #         return False

    def delete(self, condition: dict, indexName: str, knowledgebaseId: list[str]) -> int:
        """删除文档"""
        # collection_name = f"{indexName}_{knowledgebaseId}"
        collection_name = indexName
        conn = self._get_connection()

        if not conn.has_collection(collection_name):
            logger.warning(f"集合 {collection_name} 不存在")
            return 0

        # 构建过滤条件
        filter_expr = ""
        if "id" in condition:
            ids = condition["id"]
            if not isinstance(ids, list):
                ids = [ids]

            id_strs = [f"'{id}'" for id in ids]
            filter_expr = f"id in [{','.join(id_strs)}]"
        else:
            filter_parts = []
            for k, v in condition.items():
                if k == "pk" or not v:
                    continue
                if isinstance(v, list):
                    values = [f"'{item}'" if isinstance(item, str) else str(item) for item in v]
                    filter_parts.append(f"{k} in [{','.join(values)}]")
                elif isinstance(v, str):
                    filter_parts.append(f"{k} == '{v}'")
                elif isinstance(v, (int, float)):
                    filter_parts.append(f"{k} == {v}")

            if filter_parts:
                filter_expr = " && ".join(filter_parts)

        if not filter_expr:
            logger.error("删除操作需要有效的条件")
            return 0

        # 执行删除
        try:
            res = conn.delete(collection_name, expression=filter_expr)
            return res.delete_count
        except Exception as e:
            logger.error(f"从 {collection_name} 删除记录失败: {str(e)}")
            return 0

    """
    搜索结果的辅助函数
    """

    def getTotal(self, res):
        """获取结果总数"""
        if isinstance(res, tuple):
            return res[1]
        elif isinstance(res, list):
            return len(res)
        return 0

    def getChunkIds(self, res):
        """获取块ID列表，兼容不同版本的Milvus"""
        # 检查结果是否为元组，如果是则提取第一个元素
        if isinstance(res, tuple):
            results = res[0]
        else:
            results = res

        chunk_ids = []
        if isinstance(results, list):
            for item in results:
                # 优先检查'id'字段(低版本)，如果不存在则尝试'pk'字段(高版本)
                if "id" in item:
                    chunk_ids.append(item.get("id"))
                elif "pk" in item:
                    chunk_ids.append(item.get("pk"))

        return chunk_ids

    def getFields(self, res, fields: list[str]) -> dict[str, dict]:
        """获取指定字段的值，兼容不同版本的Milvus"""
        result = {}

        # 获取结果列表
        if isinstance(res, tuple):
            results = res[0]
        else:
            results = res

        if not isinstance(results, list):
            return {}

        # 处理每一个结果项
        for item in results:
            if not isinstance(item, dict):
                continue

            # 兼容低版本使用'id'和高版本使用'pk'的情况
            doc_id = None
            if "pk" in item:
                doc_id = item.get("pk")
            elif "id" in item:
                doc_id = item.get("id")

            if doc_id is None:
                continue

            row_dict = {}

            # 处理常规字段和嵌套的entity字段
            for field in fields:
                # 首先检查顶层是否有该字段
                if field in item:
                    value = item.get(field)
                # 然后检查entity字典是否有该字段
                elif "entity" in item and isinstance(item["entity"], dict) and field in item["entity"]:
                    value = item["entity"].get(field)
                else:
                    continue

                # 特殊字段处理
                if field in ["important_kwd", "question_kwd", "entities_kwd"] and isinstance(value, str):
                    value = value.split("###") if value else []
                elif field == "position_int" and isinstance(value, str):
                    if value:
                        try:
                            if value.startswith('[') and value.endswith(']'):
                                # 处理JSON格式的字符串
                                value = eval(value) if value != '[]' else []
                            else:
                                # 处理下划线分隔的十六进制格式
                                arr = [int(hex_val, 16) for hex_val in value.split('_')]
                                value = [arr[i:i + 5] for i in range(0, len(arr), 5)]
                        except:
                            value = []
                    else:
                        value = []
                elif field in ["page_num_int", "top_int"] and isinstance(value, str):
                    if value:
                        try:
                            if value.startswith('[') and value.endswith(']'):
                                # 处理JSON格式的字符串
                                value = eval(value) if value != '[]' else []
                            else:
                                # 处理下划线分隔的十六进制格式
                                value = [int(hex_val, 16) for hex_val in value.split('_')]
                        except:
                            value = []
                    else:
                        value = []

                row_dict[field] = value

            # 将主键加入结果字典，兼容不同版本
            if "pk" not in row_dict and "id" not in row_dict:
                if "pk" in item:
                    row_dict["pk"] = item["pk"]
                elif "id" in item:
                    row_dict["id"] = item["id"]

            # 添加到结果字典，使用主键作为键
            if row_dict:
                result[doc_id] = row_dict

        return result

    def getHighlight(self, res, keywords: list[str], fieldnm: str) -> dict[str, str]:
        """生成高亮文本 (Milvus不支持高亮，需要在应用层实现)"""
        result = {}

        # 获取结果列表
        if isinstance(res, tuple):
            results = res[0]
        else:
            results = res

        if not isinstance(results, list):
            return {}

        # 处理每一个结果项
        for item in results:
            if not isinstance(item, dict):
                continue

            # 兼容低版本使用'id'和高版本使用'pk'的情况
            doc_id = None
            if "pk" in item:
                doc_id = item.get("pk")
            elif "id" in item:
                doc_id = item.get("id")

            if doc_id is None:
                continue

            # 尝试从item或者entity中获取字段值
            text = None
            if fieldnm in item:
                text = item.get(fieldnm)
            elif "entity" in item and isinstance(item["entity"], dict) and fieldnm in item["entity"]:
                text = item["entity"].get(fieldnm)

            if not isinstance(text, str):
                continue

            # 清理文本
            text = re.sub(r"[\r\n]", " ", text, flags=re.IGNORECASE | re.MULTILINE)
            highlighted_parts = []

            # 为每个句子应用高亮
            for sentence in re.split(r"[.?!;\n]", text):
                for keyword in keywords:
                    sentence = re.sub(
                        r"(^|[ .?/'\"\(\)!,:;-])(%s)([ .?/'\"\(\)!,:;-])" % re.escape(keyword),
                        r"\1<em>\2</em>\3",
                        sentence,
                        flags=re.IGNORECASE | re.MULTILINE,
                    )

                # 只保留包含高亮的部分
                if re.search(r"<em>[^<>]+</em>", sentence, flags=re.IGNORECASE | re.MULTILINE):
                    highlighted_parts.append(sentence.strip())

            # 如果找到高亮部分，将其合并
            if highlighted_parts:
                result[doc_id] = "...".join(highlighted_parts)

        return result

    def getAggregation(self, res, fieldnm: str) -> list[tuple]:
        """获取聚合结果 (Milvus不支持直接聚合，需要在应用层实现)"""
        # 获取结果列表
        if isinstance(res, tuple):
            results = res[0]
        else:
            results = res

        if not isinstance(results, list):
            return []

        # 在应用层实现简单的计数聚合
        try:
            # 手动计算字段值计数
            value_counts = {}
            for item in results:
                if not isinstance(item, dict):
                    continue

                # 尝试从item或者entity中获取字段值
                value = None
                if fieldnm in item:
                    value = item.get(fieldnm)
                elif "entity" in item and isinstance(item["entity"], dict) and fieldnm in item["entity"]:
                    value = item["entity"].get(fieldnm)

                if value is None:
                    continue

                # 如果是集合类型，转换为字符串
                if not isinstance(value, str) and not isinstance(value, int) and not isinstance(value, float):
                    value = str(value)

                if value in value_counts:
                    value_counts[value] += 1
                else:
                    value_counts[value] = 1

            # 将结果转换为(值, 计数)的元组列表
            result = [(str(value), count) for value, count in value_counts.items()]
            return result
        except Exception as e:
            logger.warning(f"聚合计算失败: {str(e)}")
            return []

    """
    SQL 功能
    """

    def sql(self, sql: str, fetch_size: int, format: str):
        """
        Milvus 不支持直接执行 SQL 查询，此方法提供有限的 SQL 到 Milvus 查询的转换
        """
        logger.debug(f"尝试解析 SQL 查询: {sql}")

        # 尝试提取基本 SQL 组件 (非常简化的解析)
        try:
            # 移除多余空格和引号
            sql = re.sub(r" +", " ", sql).strip()

            # 判断是否是 SELECT 查询
            if not sql.upper().startswith("SELECT"):
                raise ValueError("只支持 SELECT 语句")

            # 提取表名
            from_match = re.search(r"FROM\s+(\w+)", sql, re.IGNORECASE)
            if not from_match:
                raise ValueError("无法识别 FROM 子句")

            collection_name = from_match.group(1)

            # 提取 WHERE 条件
            where_clause = ""
            where_match = re.search(r"WHERE\s+(.*?)(?:ORDER BY|GROUP BY|LIMIT|$)", sql, re.IGNORECASE)
            if where_match:
                where_clause = where_match.group(1).strip()

            # 提取 LIMIT
            limit = fetch_size
            limit_match = re.search(r"LIMIT\s+(\d+)", sql, re.IGNORECASE)
            if limit_match:
                limit = int(limit_match.group(1))

            # 将 SQL WHERE 转换为 Milvus 过滤表达式
            filter_expr = ""
            if where_clause:
                # 替换 SQL 运算符为 Milvus 运算符
                filter_expr = where_clause
                filter_expr = re.sub(r"=", "==", filter_expr)
                filter_expr = re.sub(r"AND", "&&", filter_expr, flags=re.IGNORECASE)
                filter_expr = re.sub(r"OR", "||", filter_expr, flags=re.IGNORECASE)
                filter_expr = re.sub(r"NOT", "!", filter_expr, flags=re.IGNORECASE)

            # 使用 Milvus query 方法执行查询
            conn = self._get_connection()
            results = conn.query(
                collection_name,
                expr=filter_expr,
                output_fields=["*"],
                limit=limit
            )

            # 根据请求的格式返回结果
            if format.lower() == "json":
                return {"data": results}
            else:
                return results

        except Exception as e:
            logger.error(f"SQL 解析或执行失败: {str(e)}")
            raise ValueError(f"SQL 查询失败: {str(e)}")

    def update(self, condition: dict, newValue: dict, indexName: str, knowledgebaseId: list[str]) -> bool:
        """
        更新 Milvus 中的文档，基于条件查询并应用新值。
        支持处理特殊操作如移除字段（通过设置为默认值）和处理时间字段更新。

        Args:
            condition: 查询条件字典
            newValue: 要更新的新值字典
            indexName: 索引名称
            knowledgebaseId: 知识库ID(ps：我们这边统一用kb_names,不用ID)

        Returns:
            bool: 操作是否成功
        """
        # collection_name = f"{indexName}_{knowledgebaseId}"
        collection_name = indexName
        conn = self._get_connection()

        # 复制新值以避免修改原始数据
        doc = copy.deepcopy(newValue)
        doc.pop("pk", None)  # 移除pk字段，避免主键冲突

        # 构建查询表达式
        filter_parts = []

        # 处理单个文档更新（通过PK）
        if "pk" in condition and isinstance(condition["pk"], str):
            filter_parts.append(f"pk == '{condition['pk']}'")
        else:
            # 处理其他条件
            for k, v in condition.items():
                if k == "_id" or not v:
                    continue
                if k == "exists":
                    # Milvus不支持exists查询，提供警告但继续执行
                    logger.warning(f"Milvus不支持exists查询: {k}={v}，此条件将被忽略")
                    continue
                if isinstance(v, list):
                    values = [f"'{item}'" if isinstance(item, str) else str(item) for item in v]
                    filter_parts.append(f"{k} in [{','.join(values)}]")
                elif isinstance(v, str):
                    filter_parts.append(f"{k} == '{v}'")
                elif isinstance(v, (int, float)):
                    filter_parts.append(f"{k} == {v}")
                else:
                    logger.warning(f"条件 `{k}={v}` 类型 {type(v)} 不支持，将被忽略")

        # # 添加知识库ID条件（如果未指定）
        # if knowledgebaseId and not any(part.startswith("kb_id") for part in filter_parts):
        #     filter_parts.append(f"kb_id == '{knowledgebaseId}'")

        if not filter_parts:
            logger.warning("没有有效的查询条件")
            return False

        filter_expr = " && ".join(filter_parts)

        # 处理特殊操作（如remove和add）
        remove_fields = []
        add_operations = {}

        if "remove" in doc:
            remove_val = doc.pop("remove")
            if isinstance(remove_val, str):
                remove_fields.append(remove_val)
            elif isinstance(remove_val, dict):
                for k, v in remove_val.items():
                    logger.warning(f"Milvus不支持移除列表中的特定元素: {k}={v}，将被忽略")

        if "add" in doc:
            add_val = doc.pop("add")
            if isinstance(add_val, dict):
                add_operations = add_val

        # 重试机制
        for attempt in range(ATTEMPT_TIME):
            try:
                # 查询匹配的文档
                results = conn.query(collection_name, expr=filter_expr, output_fields=["*"])

                if not results:
                    logger.warning(f"没有找到匹配条件的记录: {filter_expr}")
                    return False

                # 更新文档
                updated_records = []

                # 自动更新时间字段（与原方法保持一致）
                current_time = datetime.now()
                time_str = current_time.strftime("%Y-%m-%d %H:%M:%S")
                timestamp = current_time.timestamp()

                for record in results:
                    # 处理需要移除的字段
                    for field in remove_fields:
                        if field in record:
                            # 根据字段类型设置默认值
                            if field == "pagerank_fea":
                                record[field] = 0.0  # 浮点型
                            elif field.endswith("_int"):
                                record[field] = 0  # 整型
                            elif field.endswith("_flt"):
                                record[field] = 0.0  # 浮点型
                            elif isinstance(record[field], list):
                                record[field] = []  # 列表
                            elif isinstance(record[field], str):
                                record[field] = ""  # 字符串

                    # 处理添加操作
                    for field, value in add_operations.items():
                        if field in record and isinstance(record[field], list):
                            if value not in record[field]:
                                record[field].append(value)
                        elif field in record and isinstance(record[field], str):
                            record[field] += value.strip()
                        else:
                            # 如果字段不存在或类型不匹配，则直接设置
                            record[field] = value

                    # 合并常规更新
                    updated_record = {**record, **doc}

                    # 更新时间字段
                    updated_record["create_time"] = time_str
                    updated_record["create_timestamp_flt"] = timestamp

                    updated_records.append(updated_record)

                # 删除匹配的文档
                try:
                    delete_res = conn.delete(collection_name, expression=filter_expr)
                    logger.debug(f"删除了 {getattr(delete_res, 'delete_count', 0)} 条记录")
                except Exception as e:
                    logger.error(f"删除记录失败: {str(e)}")
                    return False

                # 插入更新后的文档
                if updated_records:
                    try:
                        insert_res = conn.insert_rows(collection_name, updated_records)
                        logger.debug(
                            f"插入了 {getattr(insert_res, 'insert_count', len(updated_records))} 条更新后的记录")
                        return True
                    except Exception as e:
                        logger.error(f"插入更新后的记录失败: {str(e)}")
                        return False

                return True

            except Exception as e:
                logger.error(f"更新操作失败(尝试{attempt + 1}/{ATTEMPT_TIME}): {str(e)}")
                if re.search(r"(timeout|connection|conflict)", str(e).lower()):
                    time.sleep(1)  # 添加短暂延迟再重试
                    continue  # 超时、连接或冲突错误时重试
                break  # 其他错误直接中断

        return False
    # def update(self, condition: dict, newValue: dict, indexName: str, knowledgebaseId: list[str]) -> bool:
    #     """
    #     使用“查询原始数据 + 删除 + 重新插入”来模拟更新操作。
    #     自动更新时间字段 create_time 和 create_timestamp_flt。
    #
    #     :param condition: 查询条件，指定要更新的记录
    #     :param newValue: 更新后的新数据
    #     :param indexName: 集合名称
    #     :param knowledgebaseId: 知识库ID(ps：我们这边统一用kb_names,不用ID)
    #     :return: 操作是否成功
    #     """
    #     if "pk" not in condition:
    #         raise ValueError("Update operation requires 'pk' in condition")
    #
    #     # Step 1: 获取集合连接
    #     conn = self._get_connection()
    #     try:
    #         collection_info = conn.describe_collection(indexName)
    #     except Exception as e:
    #         logging.error(f"Failed to get collection: {indexName}. Error: {e}")
    #         return False
    #
    #     # Step 2: 查询原始记录
    #     try:
    #         expr = f"pk == '{condition['pk']}'"
    #         results = conn.query(indexName, expr=expr, output_fields=["*"])
    #         if not results:
    #             logging.error(f"No record found with pk: {condition['pk']}")
    #             return False
    #         original_data = results[0]  # 获取查到的第一条记录
    #         logging.info(f"Original data fetched for update: {original_data}")
    #     except Exception as e:
    #         logging.error(f"Failed to fetch original record for update: {e}")
    #         return False
    #
    #     # Step 3: 合并用户提供的数据与原始数据
    #     merged_data = {**original_data, **newValue}  # 用新数据覆盖原始数据
    #
    #     # Step 4: 自动更新时间字段
    #     current_time = datetime.now()
    #     merged_data["create_time"] = current_time.strftime("%Y-%m-%d %H:%M:%S")
    #     merged_data["create_timestamp_flt"] = current_time.timestamp()
    #     logging.info(
    #         f"Updated time fields: create_time={merged_data['create_time']}, create_timestamp_flt={merged_data['create_timestamp_flt']}")
    #
    #     # Step 5: 删除原始记录
    #     try:
    #         delete_res = conn.delete(indexName, expression=expr)
    #         logging.info(f"Deleted record with condition: {expr}")
    #     except Exception as e:
    #         logging.error(f"Failed to delete record(s) for update: {e}")
    #         return False
    #
    #     # Step 6: 插入更新后的记录
    #     try:
    #         insert_res = self.insert(indexName, data=[merged_data])
    #         logging.info(f"Inserted updated record: {merged_data}")
    #         return True
    #     except Exception as e:
    #         logging.error(f"Failed to insert updated record(s): {e}")
    #         return False

    """
    Existing methods from the original MilvusConnection class
    """

    def create_collection(
            self,
            collection_name: str,
            dimension: int | None = None,
            primary_field_name: str = "id",
            id_type: str = "int",
            vector_field_name: str = "vector",
            metric_type: str = "COSINE",
            auto_id: bool = False,
            timeout: float | None = None,
            schema: CollectionSchema | None = None,
            index_params: IndexParams | None = None,
            **kwargs,
    ):
        if schema is None:
            return self._fast_create_collection(
                collection_name,
                dimension,
                primary_field_name=primary_field_name,
                id_type=id_type,
                vector_field_name=vector_field_name,
                metric_type=metric_type,
                auto_id=auto_id,
                timeout=timeout,
                **kwargs,
            )

        return self._create_collection_with_schema(
            collection_name, schema, index_params, timeout=timeout, **kwargs
        )

    def _fast_create_collection(
            self,
            collection_name: str,
            dimension: int,
            primary_field_name: str = "id",
            id_type: DataType | str = DataType.INT64,
            vector_field_name: str = "vector",
            metric_type: str = "COSINE",
            auto_id: bool = False,
            timeout: float | None = None,
            **kwargs,
    ):
        if dimension is None:
            msg = "missing required argument: 'dimension'"
            raise TypeError(msg)
        if "enable_dynamic_field" not in kwargs:
            kwargs["enable_dynamic_field"] = True

        schema = self.create_schema(auto_id=auto_id, **kwargs)

        if id_type in ("int", DataType.INT64):
            pk_data_type = DataType.INT64
        elif id_type in ("string", "str", DataType.VARCHAR):
            pk_data_type = DataType.VARCHAR
        else:
            raise PrimaryKeyException(message=ExceptionsMessage.PrimaryFieldType)

        pk_args = {}
        if "max_length" in kwargs and pk_data_type == DataType.VARCHAR:
            pk_args["max_length"] = kwargs["max_length"]

        schema.add_field(primary_field_name, pk_data_type, is_primary=True, **pk_args)
        vector_type = DataType.FLOAT_VECTOR
        schema.add_field(vector_field_name, vector_type, dim=dimension)
        schema.verify()

        conn = self._get_connection()
        if "consistency_level" not in kwargs:
            kwargs["consistency_level"] = DEFAULT_CONSISTENCY_LEVEL
        try:
            conn.create_collection(collection_name, schema, timeout=timeout, **kwargs)
            logging.debug("Successfully created collection: %s", collection_name)
        except Exception as ex:
            logging.error("Failed to create collection: %s", collection_name)
            raise ex from ex

        index_params = IndexParams()
        index_params.add_index(vector_field_name, "", "", metric_type=metric_type)
        self.create_index(collection_name, index_params, timeout=timeout)
        self.load_collection(collection_name, timeout=timeout)

    def create_index(
            self,
            collection_name: str,
            index_params: IndexParams | dict,
            timeout: float | None = None,
            **kwargs,
    ):
        # 确保 index_params 是字典形式
        if isinstance(index_params, IndexParams):
            index_params = {
                "field_name": index_params.field_name,
                "index_type": index_params.index_type,
                "params": index_params.params,
                "index_name": index_params.index_name,
                "metric_type": index_params.metric_type
            }

        self._create_index(collection_name, index_params, timeout=timeout, **kwargs)

    def _create_index(
            self, collection_name: str, index_param: dict, timeout: float | None = None, **kwargs
    ):
        conn = self._get_connection()
        try:
            field_name = index_param.get("field_name", "")
            index_name = index_param.get("index_name", "")
            metric_type = index_param.get("metric_type", "")

            conn.create_index(
                collection_name,
                field_name,
                {"metric_type": metric_type},
                timeout=timeout,
                index_name=index_name,
                **kwargs,
            )
            logging.debug("Successfully created an index on collection: %s", collection_name)
        except Exception as ex:
            logging.error("Failed to create an index on collection: %s", collection_name)
            raise ex from ex

    # def create_index(
    #         self,
    #         collection_name: str,
    #         index_params: IndexParams,
    #         timeout: float | None = None,
    #         **kwargs,
    # ):
    #     for index_param in index_params:
    #         self._create_index(collection_name, index_param, timeout=timeout, **kwargs)
    #
    # def _create_index(
    #         self, collection_name: str, index_param: Dict, timeout: float | None = None, **kwargs
    # ):
    #     conn = self._get_connection()
    #     try:
    #         params = index_param.pop("params", {})
    #         field_name = index_param.pop("field_name", "")
    #         index_name = index_param.pop("index_name", "")
    #         params.update(index_param)
    #         conn.create_index(
    #             collection_name,
    #             field_name,
    #             params,
    #             timeout=timeout,
    #             index_name=index_name,
    #             **kwargs,
    #         )
    #         logging.debug("Successfully created an index on collection: %s", collection_name)
    #     except Exception as ex:
    #         logging.error("Failed to create an index on collection: %s", collection_name)
    #         raise ex from ex

    def insert(
            self,
            collection_name: str,
            data: dict | list[dict],
            timeout: float | None = None,
            partition_name: str | None = None,
            **kwargs,
    ) -> dict:
        if isinstance(data, dict):
            data = [data]

        msg = "wrong type of argument 'data',"
        msg += f"expected 'Dict' or list of 'Dict', got '{type(data).__name__}'"

        if not isinstance(data, list):
            raise TypeError(msg)

        if len(data) == 0:
            return {"insert_count": 0, "ids": []}

        conn = self._get_connection()
        try:
            res = conn.insert_rows(
                collection_name, data, partition_name=partition_name, timeout=timeout
            )
        except Exception as ex:
            raise ex from ex
        return {"insert_count": res.insert_count, "ids": res.primary_keys}

    def hybrid_search(
            self,
            collection_name: str,
            reqs: list,  # List[AnnSearchRequest]
            ranker,  # BaseRanker
            limit: int = 10,
            output_fields: list[str] | None = None,
            timeout: float | None = None,
            partition_names: list[str] | None = None,
            **kwargs,
    ) -> list[list[dict]]:
        """执行多向量混合搜索并进行重排序。

        Args:
            collection_name: 集合名称
            reqs: 向量搜索请求列表
            ranker: 用于重排结果的排序器
            limit: 返回结果的最大数量
            output_fields: 要返回的字段列表
            timeout: 超时时间
            partition_names: 要搜索的分区名称列表
            **kwargs: 额外参数

        Returns:
            list[list[dict]]: 搜索结果的嵌套列表
        """
        conn = self._get_connection()
        try:
            res = conn.hybrid_search(
                collection_name,
                reqs,
                ranker,
                limit=limit,
                partition_names=partition_names,
                output_fields=output_fields,
                timeout=timeout,
                **kwargs,
            )
        except Exception as ex:
            logging.error(f"混合搜索集合失败: {collection_name}")
            raise ex from ex

        ret = []
        for hits in res:
            ret.append([hit.to_dict() for hit in hits])

        # 支持官方版本返回额外信息的特性
        from pymilvus.client.types import construct_cost_extra
        if hasattr(res, "cost"):
            return type("ExtraList", (list,), {"extra": construct_cost_extra(res.cost)})(ret)
        return ret

    def upsert(
            self,
            collection_name: str,
            data: dict | list[dict],
            timeout: float | None = None,
            partition_name: str | None = None,
            **kwargs,
    ) -> dict:
        """更新或插入数据到集合中。

        Args:
            collection_name: 集合名称
            data: 要插入的数据
            timeout: 超时时间
            partition_name: 分区名称
            **kwargs: 额外参数

        Returns:
            dict: 包含更新计数的字典
        """
        if isinstance(data, dict):
            data = [data]

        msg = "data参数类型错误,"
        msg += f"期望'Dict'或'Dict'列表，得到'{type(data).__name__}'"

        if not isinstance(data, list):
            raise TypeError(msg)

        if len(data) == 0:
            return {"upsert_count": 0}

        conn = self._get_connection()
        try:
            res = conn.upsert_rows(
                collection_name, data, partition_name=partition_name, timeout=timeout, **kwargs
            )
        except Exception as ex:
            raise ex from ex

        return {"upsert_count": res.upsert_count, "cost": getattr(res, "cost", None)}

    def bulk_upsert_to_milvus(self, collection_name, docs):
        # 获取集合的schema
        schema = self.describe_collection(collection_name)
        # 初始化包含字段名的空列表的字典
        data = {field['name']: [] for field in schema['fields']}

        # 填充数据
        for d in docs:
            for field in schema['fields']:
                field_name = field['name']
                if field_name in d:
                    # 如果字段是kb_id且是列表，转换为字符串
                    if field_name == "kb_id" and isinstance(d[field_name], list):
                        data[field_name].append(','.join(d[field_name]))
                    elif field['type'] == DataType.FLOAT_VECTOR:
                        data[field_name].append(d[field_name])
                    else:
                        data[field_name].append(str(d[field_name]))
                else:
                    data[field_name].append(None)  # 如果字段不存在，填充为 None

        # 准备要插入的记录
        records = []
        length = len(docs)
        for i in range(length):
            record = {}
            for field in data:
                record[field] = data[field][i]
            records.append(record)

        if records:
            try:
                self.upsert(collection_name, records)
                logging.info("Successfully upserted records to Milvus")
            except Exception as e:
                logging.error("Failed to upsert records to Milvus: " + str(e))
                raise e

    # 使用示例
    # docs = [
    #     {"id": 1, "q_128_vec": [0.1, 0.2, 0.3], "field1": "value1"},
    #     {"id": 2, "q_128_vec": [0.4, 0.5, 0.6], "field2": "value2"}
    # ]
    # milvus_conn.bulk_upsert_to_milvus("your_collection_name", docs)

    def search_by_milvus(
            self,
            collection_name: str,
            data: list[list] | list,
            filter: str = "",
            limit: int = 10,
            output_fields: list[str] | None = None,
            search_params: dict | None = None,
            timeout: float | None = None,
            partition_names: list[str] | None = None,
            anns_field: str | None = None,
            **kwargs,
    ) -> list[list[dict]]:
        """执行向量相似度搜索。

        Args:
            collection_name: 集合名称
            data: 要搜索的向量/向量组
            filter: 用于筛选的表达式
            limit: 每次搜索返回的最大结果数
            output_fields: 要返回的字段列表
            search_params: 搜索参数
            timeout: 超时时间
            partition_names: 要搜索的分区名称列表
            anns_field: 向量字段名称
            **kwargs: 额外参数

        Returns:
            list[list[dict]]: 搜索结果的嵌套列表
        """
        conn = self._get_connection()
        try:
            res = conn.search(
                collection_name,
                data,
                anns_field or "",
                search_params or {},
                expression=filter,
                limit=limit,
                output_fields=output_fields,
                partition_names=partition_names,
                expr_params=kwargs.pop("filter_params", {}),
                timeout=timeout,
                **kwargs,
            )
        except Exception as ex:
            logging.error(f"搜索集合失败: {collection_name}")
            raise ex from ex

        ret = []
        for hits in res:
            query_result = []
            for hit in hits:
                query_result.append(hit.to_dict())
            ret.append(query_result)

        # 支持官方版本返回额外信息的特性
        from pymilvus.client.types import construct_cost_extra
        if hasattr(res, "cost"):
            return type("ExtraList", (list,),
                        {"extra": construct_cost_extra(res.cost), "recalls": getattr(res, "recalls", None)})(ret)
        return ret

    def query(
            self,
            collection_name: str,
            filter: str = "",
            output_fields: list[str] | None = None,
            timeout: float | None = None,
            ids: list | str | int | None = None,
            partition_names: list[str] | None = None,
            **kwargs,
    ) -> list[dict]:
        if filter and not isinstance(filter, str):
            raise DataTypeNotMatchException(message=ExceptionsMessage.ExprType % type(filter))

        if filter and ids is not None:
            raise ParamError(message=ExceptionsMessage.AmbiguousQueryFilterParam)

        if isinstance(ids, (int, str)):
            ids = [ids]

        conn = self._get_connection()
        try:
            schema_dict = conn.describe_collection(collection_name, timeout=timeout, **kwargs)
        except Exception as ex:
            logging.error("Failed to describe collection: %s", collection_name)
            raise ex from ex

        if ids:
            filter = self._pack_pks_expr(schema_dict, ids)

        if not output_fields:
            output_fields = ["*"]
            vec_field_name = self._get_vector_field_name(schema_dict)
            if vec_field_name:
                output_fields.append(vec_field_name)

        try:
            res = conn.query(
                collection_name,
                expr=filter,
                output_fields=output_fields,
                partition_names=partition_names,
                timeout=timeout,
                **kwargs,
            )
        except Exception as ex:
            logging.error("Failed to query collection: %s", collection_name)
            raise ex from ex

        return res

    def search_iterator(
            self,
            collection_name: str,
            data: list[list] | list,
            batch_size: int = 1000,
            filter: str = None,
            limit: int = None,  # 使用默认的UNLIMITED
            output_fields: list[str] | None = None,
            search_params: dict | None = None,
            timeout: float | None = None,
            partition_names: list[str] | None = None,
            anns_field: str | None = None,
            round_decimal: int = -1,
            **kwargs,
    ):
        """创建一个迭代器，用于批量搜索向量。

        Args:
            collection_name: 要搜索的集合名称
            data: 用于搜索的向量数据
            batch_size: 每批获取的结果数量
            filter: 过滤表达式
            limit: 返回结果的总数量上限
            output_fields: 返回结果中包含的字段
            search_params: 搜索参数
            timeout: 每次RPC调用的超时时间
            partition_names: 要搜索的分区名称
            anns_field: 向量字段名称
            round_decimal: 距离值的小数位数
            **kwargs: 额外参数

        Returns:
            SearchIterator: 一个迭代器对象，用于分批获取搜索结果
        """
        conn = self._get_connection()

        # 尝试使用SearchIteratorV2
        try:
            from pymilvus.client.search_iterator import SearchIteratorV2
            return SearchIteratorV2(
                connection=conn,
                collection_name=collection_name,
                data=data,
                batch_size=batch_size,
                limit=limit,
                filter=filter,
                output_fields=output_fields,
                search_params=search_params or {},
                timeout=timeout,
                partition_names=partition_names,
                anns_field=anns_field or "",
                round_decimal=round_decimal,
                **kwargs,
            )
        except Exception as ex:
            from pymilvus.exceptions import ServerVersionIncompatibleException
            if isinstance(ex, ServerVersionIncompatibleException):
                logging.warning("SearchIteratorV2不受支持，回退到SearchIteratorV1")
            else:
                raise ex from ex

        # 回退到SearchIteratorV1
        from pymilvus.orm.iterator import SearchIterator
        from pymilvus.orm.constants import UNLIMITED, METRIC_TYPE

        if filter is not None and not isinstance(filter, str):
            raise DataTypeNotMatchException(message=ExceptionsMessage.ExprType % type(filter))

        # 设置迭代器的schema
        try:
            schema_dict = conn.describe_collection(collection_name, timeout=timeout, **kwargs)
        except Exception as ex:
            logging.error(f"获取集合描述失败: {collection_name}")
            raise ex from ex

        # 如果未提供anns_field，尝试自动确定
        if anns_field is None or anns_field == "":
            vec_field = None
            fields = schema_dict["fields"]
            vec_field_count = 0

            for field in fields:
                if is_vector_type(field["type"]):
                    vec_field_count += 1
                    vec_field = field

            if vec_field is None:
                raise MilvusException(
                    code=ErrorCode.UNEXPECTED_ERROR,
                    message="集合中应至少有一个向量字段",
                )

            if vec_field_count > 1:
                raise MilvusException(
                    code=ErrorCode.UNEXPECTED_ERROR,
                    message="当有多个向量字段时必须指定anns_field",
                )

            anns_field = vec_field["name"]
            if anns_field is None or anns_field == "":
                raise MilvusException(
                    code=ErrorCode.UNEXPECTED_ERROR,
                    message=f"无法获取搜索迭代器的anns_field名称，得到:{anns_field}",
                )

        # 设置搜索参数中的度量类型
        if search_params is None:
            search_params = {}

        if METRIC_TYPE not in search_params:
            indexes = conn.list_indexes(collection_name)
            for index in indexes:
                if anns_field == index.index_name:
                    params = index.params
                    for param in params:
                        if param.key == METRIC_TYPE:
                            search_params[METRIC_TYPE] = param.value

        if METRIC_TYPE not in search_params:
            raise MilvusException(
                ParamError, f"无法为anns_field设置度量类型:{anns_field}"
            )

        search_params["params"] = get_params(search_params)

        if limit is None:
            limit = UNLIMITED

        return SearchIterator(
            connection=conn,
            collection_name=collection_name,
            data=data,
            ann_field=anns_field,
            param=search_params,
            batch_size=batch_size,
            limit=limit,
            expr=filter,
            partition_names=partition_names,
            output_fields=output_fields,
            timeout=timeout,
            round_decimal=round_decimal,
            schema=schema_dict,
            **kwargs,
        )

    def get(
            self,
            collection_name: str,
            ids: list | str | int,
            output_fields: list[str] | None = None,
            timeout: float | None = None,
            partition_names: list[str] | None = None,
            **kwargs,
    ) -> list[dict]:
        if not isinstance(ids, list):
            ids = [ids]

        if len(ids) == 0:
            return []

        conn = self._get_connection()
        try:
            schema_dict = conn.describe_collection(collection_name, timeout=timeout, **kwargs)
        except Exception as ex:
            logging.error("Failed to describe collection: %s", collection_name)
            raise ex from ex

        if not output_fields:
            output_fields = ["*"]
            vec_field_name = self._get_vector_field_name(schema_dict)
            if vec_field_name:
                output_fields.append(vec_field_name)

        expr = self._pack_pks_expr(schema_dict, ids)
        try:
            res = conn.query(
                collection_name,
                expr=expr,
                output_fields=output_fields,
                partition_names=partition_names,
                timeout=timeout,
                **kwargs,
            )
        except Exception as ex:
            logging.error("Failed to get collection: %s", collection_name)
            raise ex from ex

        return res

    def delete(
            self,
            collection_name: str,
            ids: list | str | int | None = None,
            timeout: float | None = None,
            filter: str | None = None,
            partition_name: str | None = None,
            **kwargs,
    ) -> dict:
        """删除集合中的数据。

        Args:
            collection_name: 集合名称
            ids: 要删除的ID或ID列表
            timeout: 超时时间
            filter: 过滤表达式
            partition_name: 分区名称
            **kwargs: 额外参数

        Returns:
            dict: 包含删除计数或删除的主键ID列表的字典
        """
        pks = kwargs.get("pks", [])
        if isinstance(pks, (int, str)):
            pks = [pks]

        for pk in pks:
            if not isinstance(pk, (int, str)):
                msg = f"pks参数类型错误，期望list、int或str，得到'{type(pk).__name__}'"
                raise TypeError(msg)

        if ids is not None:
            if isinstance(ids, (int, str)):
                pks.append(ids)
            elif isinstance(ids, list):
                for id in ids:
                    if not isinstance(id, (int, str)):
                        msg = f"ids参数类型错误，期望list、int或str，得到'{type(id).__name__}'"
                        raise TypeError(msg)
                pks.extend(ids)
            else:
                msg = f"ids参数类型错误，期望list、int或str，得到'{type(ids).__name__}'"
                raise TypeError(msg)

        # 验证filter和ids参数的冲突情况
        if filter and ids is not None:
            from pymilvus.exceptions import ParamError
            from pymilvus.client.types import ExceptionsMessage
            raise ParamError(message=ExceptionsMessage.AmbiguousDeleteFilterParam)

        expr = ""
        conn = self._get_connection()
        if len(pks) > 0:
            try:
                schema_dict = conn.describe_collection(collection_name, timeout=timeout, **kwargs)
            except Exception as ex:
                logging.error(f"获取集合描述失败: {collection_name}")
                raise ex from ex
            expr = self._pack_pks_expr(schema_dict, pks)
        else:
            if not isinstance(filter, str):
                from pymilvus.exceptions import DataTypeNotMatchException
                from pymilvus.client.types import ExceptionsMessage
                raise DataTypeNotMatchException(message=ExceptionsMessage.ExprType % type(filter))
            expr = filter

        ret_pks = []
        try:
            res = conn.delete(
                collection_name=collection_name,
                expression=expr,
                partition_name=partition_name,
                expr_params=kwargs.pop("filter_params", {}),
                timeout=timeout,
                **kwargs,
            )
            if res.primary_keys:
                ret_pks.extend(res.primary_keys)
        except Exception as ex:
            logging.error(f"删除集合中的主键失败: {collection_name}")
            raise ex from ex

        # 兼容返回主键的情况
        if ret_pks:
            return ret_pks

        return {"delete_count": res.delete_count, "cost": getattr(res, "cost", None)}

    def get_collection_stats(self, collection_name: str, timeout: float | None = None) -> dict:
        conn = self._get_connection()
        stats = conn.get_collection_stats(collection_name, timeout=timeout)
        result = {stat.key: stat.value for stat in stats}
        if "row_count" in result:
            result["row_count"] = int(result["row_count"])
        return result

    def describe_collection(self, collection_name: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        return conn.describe_collection(collection_name, timeout=timeout, **kwargs)

    def has_collection(self, collection_name: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        return conn.has_collection(collection_name, timeout=timeout, **kwargs)

    def list_collections(self, **kwargs):
        conn = self._get_connection()
        return conn.list_collections(**kwargs)

    def drop_collection(self, collection_name: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        conn.drop_collection(collection_name, timeout=timeout, **kwargs)

    @classmethod
    def create_schema(cls, **kwargs):
        kwargs["check_fields"] = False
        return CollectionSchema([], **kwargs)

    @classmethod
    def prepare_index_params(cls, field_name: str = "", **kwargs):
        return IndexParams(field_name, **kwargs)

    def _create_collection_with_schema(
            self,
            collection_name: str,
            schema: CollectionSchema,
            index_params: IndexParams | None = None,
            timeout: float | None = None,
            **kwargs,
    ):
        schema.verify()

        conn = self._get_connection()
        if "consistency_level" not in kwargs:
            kwargs["consistency_level"] = DEFAULT_CONSISTENCY_LEVEL
        try:
            conn.create_collection(collection_name, schema, timeout=timeout, **kwargs)
            logging.debug("Successfully created collection: %s", collection_name)
        except Exception as ex:
            logging.error("Failed to create collection: %s", collection_name)
            raise ex from ex

        if index_params:
            self.create_index(collection_name, index_params, timeout=timeout)
            self.load_collection(collection_name, timeout=timeout)


    def create_collection_with_mapping(self, collection_name, mapping, auto_dimensions=None):
        """
        根据mapping配置创建Milvus集合，支持动态向量维度

        Args:
            collection_name: 集合名称
            mapping: 映射配置，包含字段定义和索引设置
            auto_dimensions: 维度特定值的字典，格式为 {"字段名": 实际维度}
        """
        dynamic_templates = mapping.get("mappings", {}).get("dynamic_templates", [])
        fields = []

        # 初始化自动维度字典
        if auto_dimensions is None:
            auto_dimensions = {}

        # 先找出所有regex模式的模板
        regex_patterns = []
        for template in dynamic_templates:
            for key, value in template.items():
                if value.get("match_pattern", "") == "regex":
                    regex_patterns.append((value.get("match", ""), value.get("mapping", {})))

        # 解析动态模板中的字段信息
        for template in dynamic_templates:
            for key, value in template.items():
                match_pattern = value.get("match_pattern", "")
                # 如果是正则匹配模式，跳过直接创建字段
                if match_pattern == "regex":
                    continue

                match = value.get("match", "")
                mapping_type = value.get("mapping", {}).get("type", "")

                # 处理向量字段的维度
                if mapping_type == "FLOAT_VECTOR":
                    dims = value.get("mapping", {}).get("dims", 768)
                    if dims == "auto":
                        if match in auto_dimensions:
                            dims = auto_dimensions[match]
                        else:
                            # 默认使用768维
                            dims = 768

                    fields.append(FieldSchema(name=match, dtype=DataType.FLOAT_VECTOR, dim=dims))
                elif mapping_type == "VARCHAR":
                    max_length = value.get("mapping", {}).get("max_length", 256)
                    is_primary = value.get("mapping", {}).get("is_primary", False)
                    fields.append(FieldSchema(name=match, dtype=DataType.VARCHAR,
                                              max_length=max_length, is_primary=is_primary,
                                              nullable=not is_primary))
                elif mapping_type == "FLOAT":
                    fields.append(FieldSchema(name=match, dtype=DataType.FLOAT, nullable=True))
                elif mapping_type == "INT64":
                    fields.append(FieldSchema(name=match, dtype=DataType.INT64, nullable=True))
                elif mapping_type == "JSON":
                    fields.append(FieldSchema(name=match, dtype=DataType.JSON, nullable=True))
                elif mapping_type == "ARRAY":
                    element_type = value.get("mapping", {}).get("element_type", DataType.VARCHAR)
                    max_length = value.get("mapping", {}).get("max_length", 256)
                    max_capacity = value.get("mapping", {}).get("max_capacity", 4096)
                    fields.append(FieldSchema(name=match, dtype=DataType.ARRAY,
                                              element_type=getattr(DataType, element_type) if isinstance(element_type,
                                                                                                         str) else element_type,
                                              max_length=max_length, max_capacity=max_capacity,
                                              nullable=True))

        # 添加维度特定的向量字段
        for vector_field, dim in auto_dimensions.items():
            # 跳过已添加的字段
            if any(field.name == vector_field for field in fields):
                continue

            # 如果是维度特定字段模式 (q_{dim}_vec)
            if re.match(r'q_\d+_vec', vector_field):
                fields.append(FieldSchema(name=vector_field, dtype=DataType.FLOAT_VECTOR, dim=dim))
                logging.info(f"添加维度特定向量字段: {vector_field}, 维度: {dim}")

        # 创建集合模式
        schema = CollectionSchema(fields=fields, description="Created from mapping file", enable_dynamic_field=True)

        # 创建集合
        self.create_collection(collection_name, schema=schema)
        logging.info(f"成功创建集合: {collection_name} 包含字段: {[field.name for field in fields]}")

        # 处理索引相关的逻辑
        for field in fields:
            if field.dtype == DataType.FLOAT_VECTOR:
                index_params = {
                    "field_name": field.name,
                    "index_type": "IVF_FLAT",
                    "params": {"nlist": 128},
                    "index_name": field.name,
                    "metric_type": "COSINE"
                }
                try:
                    self.create_index(collection_name, index_params)
                    logging.info(f"为字段 {field.name} 创建索引成功")
                except Exception as e:
                    logging.warning(f"为字段 {field.name} 创建索引失败: {str(e)}")

        # 加载集合
        try:
            self.load_collection(collection_name)
            logging.info(f"成功加载集合 {collection_name}")
        except Exception as e:
            logging.warning(f"加载集合 {collection_name} 失败: {str(e)}")

    def update_collection_schema(self, collection_name, vector_dimension):
        """
        更新现有集合的模式，添加支持新的向量维度

        Args:
            collection_name: 集合名称
            vector_dimension: 要添加的向量维度

        Returns:
            bool: 成功返回True，失败返回False
        """
        if not self.has_collection(collection_name):
            logging.error(f"集合 {collection_name} 不存在")
            return False

        try:
            # 检查字段是否已存在
            schema = self.describe_collection(collection_name)
            field_exists = False

            for field in schema['fields']:
                if field['name'] == f"q_{vector_dimension}_vec":
                    field_exists = True
                    break

            if field_exists:
                logging.info(f"字段 q_{vector_dimension}_vec 已存在于集合 {collection_name} 中")
                return True

            # 向集合添加新字段
            from pymilvus.orm import FieldSchema, DataType
            field_schema = FieldSchema(
                name=f"q_{vector_dimension}_vec",
                dtype=DataType.FLOAT_VECTOR,
                dim=vector_dimension
            )

            conn = self._get_connection()
            conn.create_field(collection_name, field_schema)
            logging.info(f"成功向集合 {collection_name} 添加字段 q_{vector_dimension}_vec")

            # 为新字段创建索引
            index_params = {
                "field_name": f"q_{vector_dimension}_vec",
                "index_type": "IVF_FLAT",
                "params": {"nlist": 128},
                "metric_type": "COSINE"
            }
            self.create_index(collection_name, index_params)
            logging.info(f"成功为字段 q_{vector_dimension}_vec 创建索引")

            return True
        except Exception as e:
            logging.error(f"更新集合模式失败: {e}")
            return False


    def close(self):
        connections.disconnect(self._using)

    def _get_connection(self):
        """获取连接处理器"""
        return connections._fetch_handler(self._using)

    """
    连接和辅助方法
    """

    def _create_connection(
            self,
            uri: str,
            user: str = "",
            password: str = "",
            db_name: str = "",
            token: str = "",
            timeout: float | None = None,
            **kwargs,
    ) -> str:
        """创建到 Milvus 服务器的连接"""
        using = uuid4().hex
        try:
            connections.connect(using, user, password, db_name, token, uri=uri, timeout=timeout, **kwargs)
        except Exception as ex:
            logger.error(f"创建新连接失败 {using}: {str(ex)}")
            raise ex from ex
        else:
            logger.debug(f"创建新连接成功: {using}")
            return using

    def _extract_primary_field(self, schema_dict: dict) -> dict:
        """从schema中提取主键字段信息"""
        fields = schema_dict.get("fields", [])
        if not fields:
            return {}

        for field_dict in fields:
            if field_dict.get("is_primary", None) is not None:
                return field_dict

        return {}

    def _get_vector_field_name(self, schema_dict: dict):
        fields = schema_dict.get("fields", [])
        if not fields:
            return {}

        for field_dict in fields:
            if field_dict.get("type", None) == DataType.FLOAT_VECTOR:
                return field_dict.get("name", "")
        return ""

    def _pack_pks_expr(self, schema_dict: dict, pks: list) -> str:
        primary_field = self._extract_primary_field(schema_dict)
        pk_field_name = primary_field["name"]
        data_type = primary_field["type"]

        if data_type == DataType.VARCHAR:
            ids = ["'" + str(entry) + "'" for entry in pks]
            expr = f"""{pk_field_name} in [{','.join(ids)}]"""
        else:
            ids = [str(entry) for entry in pks]
            expr = f"{pk_field_name} in [{','.join(ids)}]"
        return expr

    def load_collection(self, collection_name: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        try:
            conn.load_collection(collection_name, timeout=timeout, **kwargs)
        except MilvusException as ex:
            logging.error("Failed to load collection: %s", collection_name)
            raise ex from ex

    def release_collection(self, collection_name: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        try:
            conn.release_collection(collection_name, timeout=timeout, **kwargs)
        except MilvusException as ex:
            logging.error("Failed to load collection: %s", collection_name)
            raise ex from ex

    def get_load_state(
            self,
            collection_name: str,
            partition_name: str | None = "",
            timeout: float | None = None,
            **kwargs,
    ) -> dict:
        conn = self._get_connection()
        partition_names = None
        if partition_name:
            partition_names = [partition_name]
        try:
            state = conn.get_load_state(collection_name, partition_names, timeout=timeout, **kwargs)
        except Exception as ex:
            raise ex from ex

        ret = {"state": state}
        if state == LoadState.Loading:
            progress = conn.get_loading_progress(collection_name, partition_names, timeout=timeout)
            ret["progress"] = progress

        return ret

    def refresh_load(self, collection_name: str, timeout: float | None = None, **kwargs):
        kwargs.pop("_refresh", None)
        conn = self._get_connection()
        conn.load_collection(collection_name, timeout=timeout, _refresh=True, **kwargs)

    def list_indexes(self, collection_name: str, field_name: str | None = "", **kwargs):
        conn = self._get_connection()
        indexes = conn.list_indexes(collection_name, **kwargs)
        index_name_list = []
        for index in indexes:
            if not index:
                continue
            if not field_name or index.field_name == field_name:
                index_name_list.append(index.index_name)
        return index_name_list

    def drop_index(
            self, collection_name: str, index_name: str, timeout: float | None = None, **kwargs
    ):
        conn = self._get_connection()
        conn.drop_index(collection_name, "", index_name, timeout=timeout, **kwargs)

    def describe_index(
            self, collection_name: str, index_name: str, timeout: float | None = None, **kwargs
    ) -> dict:
        conn = self._get_connection()
        return conn.describe_index(collection_name, index_name, timeout=timeout, **kwargs)

    def create_partition(
            self, collection_name: str, partition_name: str, timeout: float | None = None, **kwargs
    ):
        conn = self._get_connection()
        conn.create_partition(collection_name, partition_name, timeout=timeout, **kwargs)

    def drop_partition(
            self, collection_name: str, partition_name: str, timeout: float | None = None, **kwargs
    ):
        conn = self._get_connection()
        conn.drop_partition(collection_name, partition_name, timeout=timeout, **kwargs)

    def has_partition(
            self, collection_name: str, partition_name: str, timeout: float | None = None, **kwargs
    ) -> bool:
        conn = self._get_connection()
        return conn.has_partition(collection_name, partition_name, timeout=timeout, **kwargs)

    def list_partitions(
            self, collection_name: str, timeout: float | None = None, **kwargs
    ) -> list[str]:
        conn = self._get_connection()
        return conn.list_partitions(collection_name, timeout=timeout, **kwargs)

    def load_partitions(
            self,
            collection_name: str,
            partition_names: str | list[str],
            timeout: float | None = None,
            **kwargs,
    ):
        if isinstance(partition_names, str):
            partition_names = [partition_names]

        conn = self._get_connection()
        conn.load_partitions(collection_name, partition_names, timeout=timeout, **kwargs)

    def release_partitions(
            self,
            collection_name: str,
            partition_names: str | list[str],
            timeout: float | None = None,
            **kwargs,
    ):
        if isinstance(partition_names, str):
            partition_names = [partition_names]
        conn = self._get_connection()
        conn.release_partitions(collection_name, partition_names, timeout=timeout, **kwargs)

    def get_partition_stats(
            self, collection_name: str, partition_name: str, timeout: float | None = None, **kwargs
    ) -> dict:
        conn = self._get_connection()
        if not isinstance(partition_name, str):
            msg = f"wrong type of argument 'partition_name', str expected, got '{type(partition_name).__name__}'"
            raise TypeError(msg)
        ret = conn.get_partition_stats(collection_name, partition_name, timeout=timeout, **kwargs)
        result = {stat.key: stat.value for stat in ret}
        if "row_count" in result:
            result["row_count"] = int(result["row_count"])
        return result

    def create_user(self, user_name: str, password: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        return conn.create_user(user_name, password, timeout=timeout, **kwargs)

    def drop_user(self, user_name: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        return conn.delete_user(user_name, timeout=timeout, **kwargs)

    def update_password(
            self,
            user_name: str,
            old_password: str,
            new_password: str,
            reset_connection: bool | None = False,
            timeout: float | None = None,
            **kwargs,
    ):
        conn = self._get_connection()
        conn.update_password(user_name, old_password, new_password, timeout=timeout, **kwargs)
        if reset_connection:
            conn._setup_authorization_interceptor(user_name, new_password, None)
            conn._setup_grpc_channel()

    def list_users(self, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        return conn.list_usernames(timeout=timeout, **kwargs)

    def describe_user(self, user_name: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        try:
            res = conn.select_one_user(user_name, True, timeout=timeout, **kwargs)
        except Exception as ex:
            raise ex from ex
        if res.groups:
            item = res.groups[0]
            return {"user_name": user_name, "roles": item.roles}
        return {}

    def grant_role(self, user_name: str, role_name: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        conn.add_user_to_role(user_name, role_name, timeout=timeout, **kwargs)

    def revoke_role(
            self, user_name: str, role_name: str, timeout: float | None = None, **kwargs
    ):
        conn = self._get_connection()
        conn.remove_user_from_role(user_name, role_name, timeout=timeout, **kwargs)

    def create_role(self, role_name: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        conn.create_role(role_name, timeout=timeout, **kwargs)

    def drop_role(self, role_name: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        conn.drop_role(role_name, timeout=timeout, **kwargs)

    def describe_role(
            self, role_name: str, timeout: float | None = None, **kwargs
    ) -> list[dict]:
        conn = self._get_connection()
        db_name = kwargs.pop("db_name", "")
        try:
            res = conn.select_grant_for_one_role(role_name, db_name, timeout=timeout, **kwargs)
        except Exception as ex:
            raise ex from ex
        ret = {}
        ret["role"] = role_name
        ret["privileges"] = [dict(i) for i in res.groups]
        return ret

    def list_roles(self, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        try:
            res = conn.select_all_role(False, timeout=timeout, **kwargs)
        except Exception as ex:
            raise ex from ex

        groups = res.groups
        return [g.role_name for g in groups]

    def grant_privilege(
            self,
            role_name: str,
            object_type: str,
            privilege: str,
            object_name: str,
            db_name: str | None = "",
            timeout: float | None = None,
            **kwargs,
    ):
        conn = self._get_connection()
        conn.grant_privilege(
            role_name, object_type, object_name, privilege, db_name, timeout=timeout, **kwargs
        )

    def revoke_privilege(
            self,
            role_name: str,
            object_type: str,
            privilege: str,
            object_name: str,
            db_name: str | None = "",
            timeout: float | None = None,
            **kwargs,
    ):
        conn = self._get_connection()
        conn.revoke_privilege(
            role_name, object_type, object_name, privilege, db_name, timeout=timeout, **kwargs
        )

    def create_alias(
            self, collection_name: str, alias: str, timeout: float | None = None, **kwargs
    ):
        conn = self._get_connection()
        conn.create_alias(collection_name, alias, timeout=timeout, **kwargs)

    def drop_alias(self, alias: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        conn.drop_alias(alias, timeout=timeout, **kwargs)

    def alter_alias(
            self, collection_name: str, alias: str, timeout: float | None = None, **kwargs
    ):
        conn = self._get_connection()
        conn.alter_alias(collection_name, alias, timeout=timeout, **kwargs)

    def describe_alias(self, alias: str, timeout: float | None = None, **kwargs) -> dict:
        conn = self._get_connection()
        return conn.describe_alias(alias, timeout=timeout, **kwargs)

    def list_aliases(
            self, collection_name: str = "", timeout: float | None = None, **kwargs
    ) -> list[str]:
        conn = self._get_connection()
        return conn.list_aliases(collection_name, timeout=timeout, **kwargs)

    def using_database(self, db_name: str, **kwargs):
        conn = self._get_connection()
        conn.reset_db_name(db_name)


# Create a singleton instance of MilvusConnection
# MILVUS_CONNECTION = MilvusConnection()
