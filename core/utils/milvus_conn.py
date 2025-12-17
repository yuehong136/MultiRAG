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
import uuid
from typing import Any
from uuid import uuid4
from datetime import datetime
from pymilvus import MilvusClient, Function
from pymilvus.client.constants import DEFAULT_CONSISTENCY_LEVEL
from pymilvus.client.types import ExceptionsMessage, LoadState, FunctionType, OmitZeroDict, ResourceGroupConfig, \
    ReplicaInfo
from pymilvus.client.utils import is_vector_type, get_params
from pymilvus.client.abstract import AnnSearchRequest, BaseRanker
from pymilvus.client.search_iterator import SearchIteratorV2
from pymilvus.exceptions import (
    DataTypeNotMatchException,
    ErrorCode,
    MilvusException,
    ParamError,
    PrimaryKeyException,
    ServerVersionIncompatibleException,
)
from pymilvus.milvus_client.index import IndexParams, IndexParam
from pymilvus.orm import utility
from pymilvus.orm.collection import CollectionSchema, FieldSchema
from pymilvus.orm.connections import connections
from pymilvus.orm.constants import FIELDS, METRIC_TYPE, TYPE, UNLIMITED
from pymilvus.orm.iterator import QueryIterator, SearchIterator
from pymilvus.orm.types import DataType
from pymilvus import __version__

from common.file_utils import get_project_base_directory
from core import settings
import numpy as np
from core.nlp import is_english
from core.settings import TAG_FLD, PAGERANK_FLD
from common.decorator import singleton
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
from common.float_utils import get_float


logger = logging.getLogger('multirag.milvus_conn')

ATTEMPT_TIME = 2


def validate_param(
    param_name: str, param: Any, expected_type: type | tuple[type, ...]
) -> None:
    if param is None:
        msg = f"missing required argument: [{param_name}]"
        raise ParamError(message=msg)

    if not isinstance(param, expected_type):
        msg = (
            f"wrong type of argument [{param_name}], "
            f"expected type: [{expected_type.__name__}], "
            f"got type: [{type(param).__name__}]"
        )
        raise ParamError(message=msg)

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
        kwargs = settings.MILVUS.get("kwargs", {})
        self._using = self._create_connection(
            uri, user, password, db_name, token, timeout=timeout, **kwargs
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

        # 预加载字段检索配置（来自 mapping.json）
        try:
            self.search_field_configs = self._load_search_field_configs()
            logger.info(f"Loaded BM25 field configs: {list(self.search_field_configs.keys())}")
        except Exception as e:
            logger.warning(f"加载检索字段配置失败: {e}")
            self.search_field_configs = {}

    def field_keyword(self, field_name: str):
        # The "docnm_kwd" field is always a string, not list.
        if field_name == "source_id" or (field_name.endswith("_kwd") and field_name != "docnm_kwd" and field_name != "knowledge_graph_kwd"):
            return True
        return False

    def _load_search_field_configs(self) -> dict:
        cfg = {}
        mapping_path = os.path.join(get_project_base_directory(), "configs", "mapping.json")
        if not os.path.exists(mapping_path):
            logger.warning(f"mapping 文件不存在: {mapping_path}")
            return cfg
        try:
            with open(mapping_path, "r", encoding="utf-8") as f:
                mapping = json.load(f)
        except Exception as e:
            logger.error(f"读取 mapping 失败: {e}")
            return cfg

        dynamic_templates = mapping.get("mappings", {}).get("dynamic_templates", [])
        for template in dynamic_templates:
            for entry in template.values():
                match = entry.get("match")
                mapping_cfg = entry.get("mapping", {})
                bm25_cfg = mapping_cfg.get("bm25")
                if not match or not bm25_cfg or not bm25_cfg.get("enable"):
                    continue
                sparse_field = bm25_cfg.get("output_field", f"{match}_sparse")
                cfg[match] = {
                    "sparse_field": sparse_field,
                    "weight": bm25_cfg.get("weight", 1.0),
                    "search_params": copy.deepcopy(bm25_cfg.get("search_params", {"drop_ratio_search": 0.1})),
                    "index_params": copy.deepcopy(bm25_cfg.get("index_params", {})),
                }
        return cfg

        """
        从 mapping.json 读取需要启用 BM25/倒排索引的字段配置。
        返回 {field_name: {"sparse_field": str, "weight": float, "search_params": dict, "index_params": dict}}
        """
        cfg = {}
        mapping_path = os.path.join(get_project_base_directory(), "configs", "mapping.json")
        if not os.path.exists(mapping_path):
            return cfg
        with open(mapping_path, "r", encoding="utf-8") as f:
            mapping = json.load(f)
        dynamic_templates = mapping.get("mappings", {}).get("dynamic_templates", [])
        for template in dynamic_templates:
            for value in template.values():
                field_name = value.get("match")
                mapping_cfg = value.get("mapping", {})
                bm25_cfg = mapping_cfg.get("bm25")
                if not field_name or not bm25_cfg or not bm25_cfg.get("enable"):
                    continue
                sparse_field = bm25_cfg.get("output_field", f"{field_name}_sparse")
                cfg[field_name] = {
                    "sparse_field": sparse_field,
                    "weight": bm25_cfg.get("weight", 1.0),
                    "search_params": copy.deepcopy(bm25_cfg.get("search_params", {"drop_ratio_search": 0.1})),
                    "index_params": copy.deepcopy(bm25_cfg.get("index_params", {
                        "inverted_index_algo": "DAAT_WAND",
                        "bm25_k1": 1.5,
                        "bm25_b": 0.75,
                    })),
                }
        return cfg

    @staticmethod
    def _parse_field_weight(field_expr: str) -> tuple[str, float]:
        if "^" in field_expr:
            fname, boost = field_expr.split("^", 1)
            try:
                return fname, float(boost)
            except ValueError:
                return fname, 1.0
        return field_expr, 1.0

    @staticmethod
    def _normalize_scores(score_map: dict, reverse: bool = False) -> dict:
        """
        使用 Min-Max 归一化。
        :param score_map: 文档ID到分数的映射
        :param reverse: 是否反转分数（例如 L2 距离越小越好，需要反转），默认为 False
        """
        if not score_map:
            return {}

        keys = list(score_map.keys())
        values = np.array([get_float(score_map[k]) for k in keys], dtype=np.float64)

        if len(values) == 1:
            return {keys[0]: 1.0}

        min_v = np.min(values)
        max_v = np.max(values)

        # 如果分值差异过小，或者只有一个分值，直接返回 1.0
        if max_v - min_v < 1e-6:
            return {k: 1.0 for k in keys}

        # 使用 Min-Max 归一化
        if reverse:
            # 对于 L2 距离：(max - x) / (max - min)，使得越小的值越大
            normalized = (max_v - values) / (max_v - min_v)
        else:
            # 对于 IP/Cosine/BM25：(x - min) / (max - min)，使得越大的值越大
            normalized = (values - min_v) / (max_v - min_v)

        return {k: float(normalized[i]) for i, k in enumerate(keys)}

    def _execute_text_queries(
        self,
        collection_names: list[str],
        text_exprs: list[MatchTextExpr],
        filter_expr: str,
        select_fields: list[str],
        limit: int,
        offset: int,
    ):
        if not text_exprs:
            return [], {}, 0

        conn = self._get_connection()
        aggregated = {}
        score_map = {}
        total_hits = 0
        search_limit = max(limit + offset, 1)

        for expr in text_exprs:
            expr_limit = expr.topn or search_limit
            for field_expr in expr.fields:
                field_name, boost = self._parse_field_weight(field_expr)
                cfg = self.search_field_configs.get(field_name)
                if not cfg:
                    continue

                sparse_field = cfg["sparse_field"]
                # base_weight = cfg.get("weight", 1.0)
                final_weight = boost
                search_params = {
                    "metric_type": "BM25",
                    "params": copy.deepcopy(cfg.get("search_params", {"drop_ratio_search": 0.1}))
                }

                for collection in collection_names:
                    try:
                        query_payload = expr.raw_text if hasattr(expr, "raw_text") else expr.matching_text
                        res = conn.search(
                            collection,
                            data=[query_payload],
                            anns_field=sparse_field,
                            param=search_params,
                            limit=expr_limit,
                            expression=filter_expr if filter_expr else None,
                            output_fields=select_fields,
                        )
                    except Exception as e:
                        logger.warning(f"BM25 检索失败 field={field_name}, collection={collection}: {e}")
                        continue

                    hits = res[0] if res else []
                    total_hits += len(hits)
                    for hit in hits:
                        hit_dict = hit.to_dict()
                        doc_id = str(hit_dict.get("id", hit_dict.get("pk", "")))
                        if not doc_id:
                            continue
                        distance = get_float(hit_dict.get("distance", 0.0))
                        entry = aggregated.setdefault(doc_id, {
                            "hit": {"id": doc_id, "pk": doc_id},
                            "entity": {},
                            "distance": 0.0,
                        })
                        entry["distance"] += final_weight * distance
                        score_map[doc_id] = entry["distance"]

                        entity = hit_dict.get("entity") or {}
                        if entity:
                            entry["entity"].update(entity)
                        entry["hit"].update(hit_dict)

        results = []
        for doc_id, data in aggregated.items():
            base_hit = data["hit"]
            if isinstance(base_hit, dict):
                hit = copy.deepcopy(base_hit)
            else:
                try:
                    hit = base_hit.to_dict()
                except Exception:
                    hit = {"raw": str(base_hit)}
            hit["distance"] = data["distance"]
            hit["_score"] = data["distance"]
            if data["entity"]:
                if "entity" not in hit or not isinstance(hit["entity"], dict):
                    hit["entity"] = {}
                hit["entity"].update(data["entity"])
            if "id" not in hit:
                hit["id"] = doc_id
            if "pk" not in hit:
                hit["pk"] = doc_id
            results.append(hit)

        results.sort(key=lambda x: x.get("distance", 0.0), reverse=True)
        return results, score_map, total_hits

    def _execute_dense_query(
        self,
        collection_names: list[str],
        dense_expr: MatchDenseExpr | None,
        filter_expr: str,
        select_fields: list[str],
        limit: int,
        offset: int,
    ):
        if dense_expr is None:
            return [], {}, 0

        vector_data = dense_expr.embedding_data
        vector_field = dense_expr.vector_column_name
        if vector_data is None or vector_field is None:
            return [], {}, 0

        conn = self._get_connection()
        search_params = {
            "metric_type": dense_expr.distance_type.upper(),
            "params": {"nprobe": 10},
        }
        if dense_expr.extra_options and "similarity" in dense_expr.extra_options:
            search_params["similarity"] = dense_expr.extra_options["similarity"]

        query_limit = dense_expr.topn or max(limit + offset, 1)
        vector_results = []
        score_map = {}
        total_hits = 0

        for collection in collection_names:
            try:
                res = conn.search(
                    collection,
                    [vector_data],
                    vector_field,
                    search_params,
                    expression=filter_expr,
                    output_fields=select_fields,
                    limit=query_limit,
                )
            except Exception as e:
                logger.warning(f"向量检索失败 collection={collection}: {e}")
                continue

            hits = res[0] if res else []
            total_hits += len(hits)
            for hit in hits:
                hit_dict = hit.to_dict()
                doc_id = str(hit_dict.get("id", hit_dict.get("pk", "")))
                vector_results.append(hit_dict)
                if doc_id:
                    score_map[doc_id] = get_float(hit_dict.get("distance", 0.0))

        vector_results.sort(key=lambda x: x.get("distance", 0.0), reverse=True)
        return vector_results, score_map, total_hits

    @staticmethod
    def _fusion_weights(fusion_expr: FusionExpr | None) -> tuple[float, float]:
        default = (0.5, 0.5)
        if not fusion_expr or not fusion_expr.fusion_params:
            return default
        weights = fusion_expr.fusion_params.get("weights")
        if not weights:
            return default
        parts = [p.strip() for p in str(weights).split(",") if p.strip()]
        if len(parts) < 2:
            return default
        try:
            return float(parts[0]), float(parts[1])
        except ValueError:
            return default

    def _search_with_text_and_dense(
        self,
        select_fields: list[str],
        filter_expr: str,
        text_exprs: list[MatchTextExpr],
        dense_expr: MatchDenseExpr | None,
        fusion_expr: FusionExpr | None,
        index_names: list[str],
        limit: int,
        offset: int,
        rank_boost: dict,
    ):
        # 1. 执行文本检索
        text_results, text_scores, _ = self._execute_text_queries(
            index_names, text_exprs, filter_expr, select_fields, limit, offset
        )

        # 2. 执行向量检索
        vector_results, vector_scores, _ = self._execute_dense_query(
            index_names, dense_expr, filter_expr, select_fields, limit, offset
        )

        combined_results = []
        if text_results and vector_results:
            text_weight, vector_weight = self._fusion_weights(fusion_expr)
            # 文本检索 BM25 分数总是越大越好，reverse=False
            norm_text = self._normalize_scores(text_scores, reverse=False)
            
            # 向量检索根据 metric_type 决定是否反转
            vector_metric_type = dense_expr.distance_type.upper() if dense_expr else "COSINE"
            is_l2 = vector_metric_type == "L2"
            norm_vector = self._normalize_scores(vector_scores, reverse=is_l2)
            
            final_scores = {}
            for doc_id in set(norm_text.keys()) | set(norm_vector.keys()):
                final_scores[doc_id] = (
                    text_weight * norm_text.get(doc_id, 0.0) +
                    vector_weight * norm_vector.get(doc_id, 0.0)
                )

            lookup = {}
            for hit in vector_results + text_results:
                if isinstance(hit, dict):
                    hit_copy = copy.deepcopy(hit)
                else:
                    try:
                        hit_copy = dict(hit.to_dict())
                    except Exception:
                        hit_copy = {"raw": str(hit)}
                doc_id = str(hit_copy.get("id", hit_copy.get("pk", "")))
                if doc_id and doc_id not in lookup:
                    lookup[doc_id] = hit_copy

            for doc_id, score in final_scores.items():
                base_hit = lookup.get(doc_id, {"id": doc_id, "pk": doc_id, "entity": {}})
                if isinstance(base_hit, dict):
                    hit = copy.deepcopy(base_hit)
                else:
                    try:
                        hit = base_hit.to_dict()
                    except Exception:
                        hit = {"raw": str(base_hit)}
                if "id" not in hit:
                    hit["id"] = doc_id
                if "pk" not in hit:
                    hit["pk"] = doc_id
                hit["distance"] = score
                hit["_score"] = score
                combined_results.append(hit)
        elif text_results:
            combined_results = text_results
        elif vector_results:
            combined_results = vector_results

        for item in combined_results:
            if "_score" not in item:
                item["_score"] = get_float(item.get("distance", item.get("score", 0.0)))
        combined_results.sort(key=lambda x: x.get("distance", 0.0), reverse=True)
        total_hits = len(combined_results)
        if offset > 0:
            combined_results = combined_results[offset:]
        if limit > 0:
            combined_results = combined_results[:limit]

        return combined_results, total_hits

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

    def createIdx(self, indexName: str | list[str], knowledgebaseId: str, vectorSize: int):
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
        # 处理索引名称参数
        if isinstance(indexName, list):
            if not indexName:  # 如果是空列表
                logger.error("索引名称列表为空")
                return False
            collection_name = indexName[0]  # 取第一个元素
            logger.debug(f"索引名称是列表，使用第一个元素: {collection_name}")
        elif isinstance(indexName, str):
            collection_name = indexName
        else:
            logger.error(f"索引名称必须是字符串或字符串列表，收到: {type(indexName)}")
            return False

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

    def _create_default_collection(self, indexName: str | list[str], knowledgebaseId: str, vectorSize: int):
        """
        使用默认字段定义创建集合（当找不到mapping文件时使用）

        Args:
            indexName: 索引名称前缀
            knowledgebaseId: 知识库ID
            vectorSize: 向量维度

        Returns:
            bool: 创建成功返回True
        """
        # 处理索引名称参数
        if isinstance(indexName, list):
            if not indexName:  # 如果是空列表
                logger.error("索引名称列表为空")
                return False
            collection_name = indexName[0]  # 取第一个元素
            logger.debug(f"索引名称是列表，使用第一个元素: {collection_name}")
        elif isinstance(indexName, str):
            collection_name = indexName
        else:
            logger.error(f"索引名称必须是字符串或字符串列表，收到: {type(indexName)}")
            return False

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

    def deleteIdx(self, indexName: str | list[str], knowledgebaseId: str):
        """删除索引，对应于删除Milvus集合"""
        # 处理索引名称参数
        if isinstance(indexName, list):
            if not indexName:  # 如果是空列表
                logger.error("索引名称列表为空")
                return False
            collection_name = indexName[0]  # 取第一个元素
            logger.debug(f"索引名称是列表，使用第一个元素: {collection_name}")
        elif isinstance(indexName, str):
            collection_name = indexName
        else:
            logger.error(f"索引名称必须是字符串或字符串列表，收到: {type(indexName)}")
            return False
        try:
            conn = self._get_connection()
            if conn.has_collection(collection_name):
                conn.drop_collection(collection_name)
                logger.info(f"成功删除集合 {collection_name}")
            return True
        except Exception as e:
            logger.error(f"删除集合 {collection_name} 失败: {str(e)}")
            raise e

    def indexExist(self, indexName: str | list[str], knowledgebaseId: str = None) -> bool:
        """检查索引是否存在，对应于检查Milvus集合是否存在"""
        # 处理索引名称参数
        if isinstance(indexName, list):
            if not indexName:  # 如果是空列表
                logger.error("索引名称列表为空")
                return False
            collection_name = indexName[0]  # 取第一个元素
            logger.debug(f"索引名称是列表，使用第一个元素: {collection_name}")
        elif isinstance(indexName, str):
            collection_name = indexName
        else:
            logger.error(f"索引名称必须是字符串或字符串列表，收到: {type(indexName)}")
            return False
        try:
            conn = self._get_connection()
            return conn.has_collection(collection_name)
        except Exception as e:
            logger.warning(f"检查集合 {collection_name} 是否存在失败: {str(e)}")
            return False

    def insert(self, rows: list[dict] | dict | str, indexName: str | list[str] | dict | list[dict], knowledgebaseId: str = None) -> list[str]:
        """插入数据

        兼容两种调用方式：
        1) 标准签名：insert(rows, indexName, knowledgebaseId)
        2) 旧版/误用：insert(collection_name, data, partition_name)  # 会自动纠正
        """
        # 兼容调用：insert(collection_name, data, partition_name)
        if isinstance(rows, str) and (
            isinstance(indexName, dict)
            or (isinstance(indexName, list) and (len(indexName) == 0 or isinstance(indexName[0], dict)))
        ):
            collection_name = rows
            data = indexName
            partition_name = knowledgebaseId
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                msg = "wrong type of argument 'data',"
                msg += f" expected 'Dict' or list of 'Dict', got '{type(data).__name__}'"
                raise TypeError(msg)
            rows = data
            indexName = collection_name
            knowledgebaseId = partition_name

        if isinstance(rows, dict):
            rows = [rows]

        if not isinstance(rows, list):
            msg = "wrong type of argument 'rows',"
            msg += f" expected 'Dict' or list of 'Dict', got '{type(rows).__name__}'"
            raise TypeError(msg)
        # 处理索引名称参数
        if isinstance(indexName, list):
            if not indexName:  # 如果是空列表
                logger.error("索引名称列表为空")
                return []
            collection_name = indexName[0]  # 取第一个元素
            logger.debug(f"索引名称是列表，使用第一个元素: {collection_name}")
        elif isinstance(indexName, str):
            collection_name = indexName
        else:
            logger.error(f"索引名称必须是字符串或字符串列表，收到: {type(indexName)}")
            return []
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

            # 确保主键字段：pk 和 id 双向补齐（Milvus schema 主键为 pk）
            if "pk" not in new_row and "id" not in new_row:
                raise ValueError("Insert requires primary key 'pk' or 'id'")
            if "pk" not in new_row:
                new_row["pk"] = new_row["id"]
            if "id" not in new_row:
                new_row["id"] = new_row["pk"]

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

    def get(self, chunkId: str, indexName: str | list[str], knowledgebaseIds: list[str]) -> dict | None:
        """获取单个文档块"""
        # for kb_id in knowledgebaseIds:
        #     collection_name = f"{indexName}_{kb_id}"
        #     conn = self._get_connection()
        # 处理索引名称参数
        if isinstance(indexName, list):
            if not indexName:  # 如果是空列表
                logger.error("索引名称列表为空")
                return None
            collection_name = indexName[0]  # 取第一个元素
            logger.debug(f"索引名称是列表，使用第一个元素: {collection_name}")
        elif isinstance(indexName, str):
            collection_name = indexName
        else:
            logger.error(f"索引名称必须是字符串或字符串列表，收到: {type(indexName)}")
            return None
        conn = self._get_connection()
        try:
            if not conn.has_collection(collection_name):
                return None

            filter_expr = f"pk == '{chunkId}'"
            results = conn.query(collection_name, filter_expr, output_fields=["*"])

            if results and len(results) > 0:
                # 处理特殊字段
                result = results[0]

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
        filter_parts = []
        if condition:
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
                elif k == "content_with_weight":
                    filter_parts.append(f"{v}")
                elif k == "exists":
                    # Filter where field exists and is not empty
                    field_name = v
                    filter_parts.append(f'{field_name} != ""')
                elif k == "must_not":
                    # Handle negative conditions
                    if isinstance(v, dict):
                        for kk, vv in v.items():
                            if kk == "exists":
                                # Filter where field doesn't exist or is empty
                                filter_parts.append(f'{vv} == ""')
                elif isinstance(v, list):
                    values = [f"'{item}'" if isinstance(item, str) else str(item) for item in v]
                    filter_parts.append(f"{k} in [{','.join(values)}]")
                elif isinstance(v, str):
                    filter_parts.append(f"{k} == '{v}'")
                elif isinstance(v, (int, float)):
                    filter_parts.append(f"{k} == {v}")

            if filter_parts:
                filter_expr = " && ".join(filter_parts)
            else:
                # Milvus query requires a valid filter expression
                # Use a default expression that matches all records
                filter_expr = "pk != ''"

        text_exprs: list[MatchTextExpr] = []
        dense_expr: MatchDenseExpr | None = None
        fusion_expr: FusionExpr | None = None

        for expr in matchExprs:
            if isinstance(expr, MatchTextExpr):
                text_exprs.append(expr)
            elif isinstance(expr, MatchDenseExpr):
                dense_expr = expr
            elif isinstance(expr, FusionExpr):
                fusion_expr = expr

        # 如果存在 rank_feature，添加到查询参数中
        # Milvus 不支持直接的 rank_feature，但我们可以在后处理中模拟
        rank_boost = {}
        if rank_feature:
            for field, score in rank_feature.items():
                if field != PAGERANK_FLD:
                    field = f"{TAG_FLD}.{field}"
                rank_boost[field] = score

        has_retrieval = bool(text_exprs or dense_expr)

        if not has_retrieval:
            # 只有条件过滤，没有向量搜索
            for indexName in indexNames:
                collection_name = indexName
                try:
                    conn = self._get_connection()
                    if not conn.has_collection(collection_name):
                        continue

                    # 执行条件查询
                    # 使用一个合理的最大值来获取足够的数据，然后在内存中分页
                    # Milvus 最大限制是 16384，我们使用 10000 作为合理的默认值
                    results = conn.query(
                        collection_name,
                        filter_expr,
                        output_fields=selectFields,
                        limit=10000
                    )

                    if results:
                        # 应用 rank_feature 排序（如果有）
                        if rank_boost and PAGERANK_FLD in selectFields:
                            # 添加得分字段
                            for result in results:
                                pagerank = get_float(result.get(PAGERANK_FLD, 0))
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
            combined_results, total_hits_count = self._search_with_text_and_dense(
                select_fields=selectFields,
                filter_expr=filter_expr,
                text_exprs=text_exprs,
                dense_expr=dense_expr,
                fusion_expr=fusion_expr,
                index_names=indexNames,
                limit=limit,
                offset=offset,
                rank_boost=rank_boost or {},
            )
            return combined_results, total_hits_count

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

    def delete(self, condition: dict, indexName: str | list[str], knowledgebaseId: str) -> int:
        """删除文档"""
        # 处理索引名称参数
        if isinstance(indexName, list):
            if not indexName:  # 如果是空列表
                logger.error("索引名称列表为空")
                return False
            collection_name = indexName[0]  # 取第一个元素
            logger.debug(f"索引名称是列表，使用第一个元素: {collection_name}")
        elif isinstance(indexName, str):
            collection_name = indexName
        else:
            logger.error(f"索引名称必须是字符串或字符串列表，收到: {type(indexName)}")
            return False
        conn = self._get_connection()

        if not conn.has_collection(collection_name):
            logger.warning(f"集合 {collection_name} 不存在")
            return 0

        # 构建过滤条件
        filter_expr = ""
        # 优先使用 "id" 字段（与ES保持一致），其次使用 "pk" 字段
        chunk_ids = condition.get("id") or condition.get("pk")
        if chunk_ids:
            if not isinstance(chunk_ids, list):
                chunk_ids = [chunk_ids]

            id_strs = [f"'{id}'" for id in chunk_ids]
            filter_expr = f"pk in [{','.join(id_strs)}]"  # Milvus 内部主键字段名是 pk
        else:
            filter_parts = []
            for k, v in condition.items():
                if k in ("pk", "id") or not v:  # 排除 pk 和 id 字段
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
        # 初始化distance为空列表
        result["distance"] = []

        # 获取结果列表
        if isinstance(res, tuple):
            results = res[0]
        else:
            results = res

        if not isinstance(results, list):
            return {}

        # 处理每一个结果项
        for item in results:

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
                if field == "_score":
                    value = get_float(item.get("distance", item.get("score", 0.0)))
                    row_dict[field] = value
                    continue
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
                if "distance" in item:
                    result["distance"].append(item["distance"])

        return result

    def getHighlight(self, res, keywords: list[str], fieldnm: str):
        """
        生成高亮文本（应用层实现，支持中英文）
        - res：可以是 list 或 (list, …) 形式
        - keywords：待高亮关键词列表
        - fieldnm：要高亮的字段名
        返回 {doc_id: snippet} 格式的字典
        
        参考 infinity_conn.py 实现，支持中文高亮
        """
        ans: dict[str, str] = {}

        # 兼容 tuple 包装
        results = res[0] if isinstance(res, tuple) else res
        if not isinstance(results, list):
            logger.warning(f"getHighlight: results 不是列表，类型: {type(results)}")
            return ans

        logger.info(f"getHighlight: 收到 {len(results)} 条结果，{len(keywords)} 个关键词: {keywords}")

        for item in results:
            # 采用与 getFields 完全相同的逻辑
            # 兼容低版本使用'id'和高版本使用'pk'的情况
            doc_id = None
            if "pk" in item:
                doc_id = item.get("pk")
            elif "id" in item:
                doc_id = item.get("id")

            if doc_id is None:
                logger.warning(f"getHighlight: 无法获取 doc_id，item keys: {list(item.keys()) if hasattr(item, 'keys') else 'no keys'}")
                continue

            # 提取待高亮文本（完全按照 getFields 的逻辑）
            text = None
            # 首先检查顶层是否有该字段
            if fieldnm in item:
                text = item.get(fieldnm)
                logger.debug(f"getHighlight: doc_id={doc_id}, 从顶层获取 {fieldnm}")
            # 然后检查entity字典是否有该字段
            elif "entity" in item and isinstance(item["entity"], dict) and fieldnm in item["entity"]:
                text = item["entity"].get(fieldnm)
                logger.debug(f"getHighlight: doc_id={doc_id}, 从 entity 获取 {fieldnm}")
            
            if not isinstance(text, str):
                logger.warning(f"getHighlight: doc_id={doc_id}, 无法获取文本字段 {fieldnm}")
                continue
            
            logger.debug(f"getHighlight: doc_id={doc_id}, 获取到文本，长度: {len(text)}")

            # 检查是否已经包含高亮标签（避免重复处理）
            if re.search(r"<em>[^<>]+</em>", text, flags=re.IGNORECASE | re.MULTILINE):
                ans[doc_id] = text
                continue

            # 清理换行符
            text = re.sub(r"[\r\n]+", " ", text, flags=re.IGNORECASE | re.MULTILINE)

            # 按句子拆分并高亮关键词（中英文分别处理）
            snippets: list[str] = []
            for sentence in re.split(r"[.?!；。？！\n]", text):
                if not sentence.strip():
                    continue
                
                sent = sentence
                # 判断当前句子是否为英文
                if is_english([sent]):
                    # 英文处理：依赖单词边界，避免误匹配
                    for kw in keywords:
                        pattern = rf"(^|[ .?/'\"()!,:;-])({re.escape(kw)})([ .?/'\"()!,:;-])"
                        sent = re.sub(
                            pattern,
                            r"\1<em>\2</em>\3",
                            sent,
                            flags=re.IGNORECASE | re.MULTILINE,
                        )
                else:
                    # 中文处理：直接字符串匹配，按长度排序避免短词误匹配
                    for kw in sorted(keywords, key=len, reverse=True):
                        if not kw:
                            continue
                        sent = re.sub(
                            re.escape(kw),
                            f"<em>{kw}</em>",
                            sent,
                            flags=re.IGNORECASE | re.MULTILINE,
                        )
                
                # 只保留包含高亮的句子
                if re.search(r"<em>[^<>]+</em>", sent, flags=re.IGNORECASE | re.MULTILINE):
                    snippets.append(sent.strip())

            # 有高亮句子则拼接，否则回退全文
            if snippets:
                ans[doc_id] = "...".join(snippets)
            else:
                ans[doc_id] = text

        return ans

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
            
            # 如果没有FROM子句，尝试从WHERE条件中的kb_id推断集合名
            if not from_match:
                logger.info("SQL缺少FROM子句，尝试从kb_id推断集合名")
                # 提取kb_id
                kb_id_match = re.search(r"kb_id\s*=\s*['\"]([^'\"]+)['\"]", sql, re.IGNORECASE)
                if kb_id_match:
                    kb_id = kb_id_match.group(1)
                    logger.debug(f"从SQL中提取到kb_id: {kb_id}")
                    
                    # 查询数据库获取kb信息
                    from api.db.db_models import db_connection
                    from api.db.services.knowledgebase_service import KnowledgebaseService
                    try:
                        with db_connection() as db:
                            kb = KnowledgebaseService.get_by_id(db, kb_id)
                            if kb:
                                # 生成集合名：multirag_{tenant_id}_{kb_name}
                                collection_name = f"multirag_{kb.tenant_id}_{kb.name}"
                                logger.info(f"自动推断集合名: {collection_name}")
                                
                                # 在SELECT和WHERE之间插入FROM子句
                                where_pos = sql.upper().find("WHERE")
                                if where_pos > 0:
                                    sql = sql[:where_pos] + f"FROM {collection_name} " + sql[where_pos:]
                                else:
                                    # 如果没有WHERE，追加FROM到末尾（虽然不太可能）
                                    sql = sql + f" FROM {collection_name}"
                                logger.debug(f"补充FROM子句后的SQL: {sql}")
                                from_match = re.search(r"FROM\s+(\w+)", sql, re.IGNORECASE)
                            else:
                                raise ValueError(f"找不到kb_id={kb_id}对应的知识库")
                    except Exception as e:
                        logger.error(f"通过kb_id查询集合名失败: {e}")
                        raise ValueError(f"无法识别 FROM 子句，且无法从kb_id推断: {e}")
                else:
                    raise ValueError("无法识别 FROM 子句，且SQL中没有kb_id信息")
            
            if not from_match:
                raise ValueError("无法识别 FROM 子句")
                
            collection_name = from_match.group(1)
            
            # 提取 SELECT 字段，并处理字面量
            select_match = re.search(r"SELECT\s+(.*?)\s+FROM", sql, re.IGNORECASE)
            select_fields = []  # 实际要查询的字段
            literal_columns = {}  # 字面量列：{列名: 值}
            
            if select_match:
                fields_str = select_match.group(1).strip()
                if fields_str != "*":
                    # 解析选择的字段（处理可能包含逗号的字面量）
                    # 更智能的字段分割：考虑引号内的逗号
                    parsed_fields = []
                    current_field = ""
                    in_quotes = False
                    quote_char = None
                    
                    for char in fields_str:
                        if char in ("'", '"') and not in_quotes:
                            in_quotes = True
                            quote_char = char
                            current_field += char
                        elif char == quote_char and in_quotes:
                            in_quotes = False
                            quote_char = None
                            current_field += char
                        elif char == ',' and not in_quotes:
                            parsed_fields.append(current_field.strip())
                            current_field = ""
                        else:
                            current_field += char
                    
                    if current_field.strip():
                        parsed_fields.append(current_field.strip())
                    
                    for field in parsed_fields:
                        # 检查是否是字面量（如 'value' as alias 或 "value" as alias）
                        literal_match = re.match(r"^(['\"])(.+?)\1(\s+as\s+(['\"]?)(.+?)\4)?$", field, re.IGNORECASE)
                        if literal_match:
                            # 这是一个字面量
                            literal_value = literal_match.group(2)
                            alias = literal_match.group(5) if literal_match.group(5) else literal_value
                            literal_columns[alias] = literal_value
                            logger.debug(f"解析到字面量列: {alias} = {literal_value}")
                        else:
                            # 普通字段
                            if " as " in field.lower():
                                # 有别名，但不是字面量
                                actual_field = field.split(" as ")[0].strip()
                                select_fields.append(actual_field)
                                logger.debug(f"解析到字段（带别名）: {actual_field}")
                            else:
                                select_fields.append(field)
                                logger.debug(f"解析到字段: {field}")

            # 提取 WHERE 条件
            where_clause = ""
            where_match = re.search(r"WHERE\s+(.*?)(?:ORDER BY|GROUP BY|LIMIT|$)", sql, re.IGNORECASE)
            if where_match:
                where_clause = where_match.group(1).strip()

            # 检测恒假条件（如 WHERE 1 = 0），这是LLM表示"不相关"的方式
            if where_clause and re.match(r"^\s*(1\s*=\s*0|0\s*=\s*1|false)\s*(and|or|&&|\|\|)?", where_clause, re.IGNORECASE):
                logger.info(f"检测到恒假条件 '{where_clause}'，返回空结果")
                return {"rows": [], "columns": []}

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
            # 确定要查询的字段
            output_fields = select_fields if select_fields else ["*"]
            results = conn.query(
                collection_name,
                expr=filter_expr,
                output_fields=output_fields,
                limit=limit
            )

            # 根据请求的格式返回结果
            if format.lower() == "json":
                # 构建符合预期的返回格式 {"rows": [...], "columns": [...]}
                if not results and not literal_columns:
                    return {"rows": [], "columns": []}
                
                # 构建列信息（包括查询字段和字面量）
                columns = []
                column_names = []
                
                if len(results) > 0:
                    # 确保列的顺序一致 - 先添加实际查询的字段
                    column_names = list(results[0].keys())
                    for key in column_names:
                        columns.append({"name": key, "type": "text"})
                
                # 添加字面量列
                for literal_name in literal_columns.keys():
                    columns.append({"name": literal_name, "type": "text"})
                    column_names.append(literal_name)
                
                # 将字典列表转换为数组列表（符合ES/OpenSearch SQL格式）
                rows = []
                if len(results) > 0:
                    for result in results:
                        row = []
                        # 添加查询字段的值
                        for col_name in list(results[0].keys()):
                            row.append(result.get(col_name))
                        # 添加字面量的值
                        for literal_name, literal_value in literal_columns.items():
                            row.append(literal_value)
                        rows.append(row)
                elif literal_columns:
                    # 如果只有字面量列，没有查询结果，也要返回一行
                    row = [literal_columns[name] for name in literal_columns.keys()]
                    rows.append(row)
                
                return {"rows": rows, "columns": columns}
            else:
                return results

        except Exception as e:
            logger.error(f"SQL 解析或执行失败: {str(e)}")
            return {"error": f"SQL 查询失败: {str(e)}"}

    def update(self, condition: dict, newValue: dict, indexName: str | list[str], knowledgebaseId: str) -> bool:
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
        # 处理索引名称参数
        if isinstance(indexName, list):
            if not indexName:  # 如果是空列表
                logger.error("索引名称列表为空")
                return False
            collection_name = indexName[0]  # 取第一个元素
            logger.debug(f"索引名称是列表，使用第一个元素: {collection_name}")
        elif isinstance(indexName, str):
            collection_name = indexName
        else:
            logger.error(f"索引名称必须是字符串或字符串列表，收到: {type(indexName)}")
            return False
        conn = self._get_connection()

        # 复制新值以避免修改原始数据
        doc = copy.deepcopy(newValue)
        doc.pop("pk", None)  # 移除pk字段，避免主键冲突
        doc.pop("id", None)  # 移除id字段，避免主键冲突

        # 构建查询表达式
        filter_parts = []

        # 处理单个文档更新（通过id或pk）- 优先使用id，与ES保持一致
        chunk_id = condition.get("id") or condition.get("pk")
        if chunk_id and isinstance(chunk_id, str):
            filter_parts.append(f"pk == '{chunk_id}'")
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
    # def update(self, condition: dict, newValue: dict, indexName: str | list[str], knowledgebaseId: str) -> bool:
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
        schema.add_field(vector_field_name, DataType.FLOAT_VECTOR, dim=dimension)
        schema.verify()

        conn = self._get_connection()
        if "consistency_level" not in kwargs:
            kwargs["consistency_level"] = DEFAULT_CONSISTENCY_LEVEL
        try:
            conn.create_collection(collection_name, schema, timeout=timeout, **kwargs)
            logger.debug("Successfully created collection: %s", collection_name)
        except Exception as ex:
            logger.error("Failed to create collection: %s", collection_name)
            raise ex from ex

        index_params = IndexParams()
        index_params.add_index(vector_field_name, index_type="AUTOINDEX", metric_type=metric_type)
        self.create_index(collection_name, index_params, timeout=timeout)
        self.load_collection(collection_name, timeout=timeout)

    def create_index(
        self,
        collection_name: str,
        index_params: IndexParams | dict,
        timeout: float | None = None,
        **kwargs,
    ):
        """
            index_params 可为：
              • IndexParams  (官方 list-like 容器)
              • IndexParam   (单个官方对象)
              • dict         (你自己手写的)
              • list[dict]   (你自己手写的一组)
            """
        # -------- IndexParams ---------
        if isinstance(index_params, IndexParams):
            for ip in index_params:  # ip 是 IndexParam
                self._create_index(collection_name, ip, timeout=timeout, **kwargs)
            return

        # -------- IndexParam ----------
        if isinstance(index_params, IndexParam):
            self._create_index(collection_name, index_params, timeout=timeout, **kwargs)
            return

        # -------- list[dict] ----------
        if isinstance(index_params, (list, tuple)):
            for ip in index_params:
                self._create_index(collection_name, ip, timeout=timeout, **kwargs)
            return

        # -------- 单个 dict ------------
        self._create_index(collection_name, index_params, timeout=timeout, **kwargs)

    def _create_index(
        self, collection_name: str, index_param, timeout: float | None = None, **kwargs
    ):
        conn = self._get_connection()
        try:
            # ---------- A. 官方 IndexParam ----------
            if isinstance(index_param, IndexParam):
                field_name = index_param.field_name
                index_name = index_param.index_name or field_name
                # 这里才是真正的 nlist / metric_type / index_type / M / efConstruction ...
                params = index_param.get_index_configs()

            # ---------- B. 你自己写的 dict ----------
            else:
                field_name = index_param.get("field_name")
                index_name = index_param.get("index_name", field_name)
                # 删掉两个无关键，剩下的全部给 Milvus
                params = {k: v for k, v in index_param.items() if k not in ("field_name", "index_name")}

            conn.create_index(
                collection_name,
                field_name,
                params,
                timeout=timeout,
                index_name=index_name,
                **kwargs,
            )
            logger.debug("Successfully created an index on collection: %s", collection_name)
        except Exception as ex:
            logger.error("Failed to create an index on collection: %s", collection_name)
            raise ex from ex

    def _insert_milvus(
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

        # 确保主键字段：优先使用 pk，若无则用 id 补齐 pk
        processed = []
        for row in data:
            if not isinstance(row, dict):
                raise TypeError("each element in data must be dict")
            r = copy.deepcopy(row)
            if "pk" not in r:
                if "id" in r:
                    r["pk"] = r["id"]
                else:
                    raise ValueError("Insert requires primary key 'pk' (or provide 'id' to map to pk)")
            processed.append(r)
        data = processed

        conn = self._get_connection()
        # Insert into the collection.
        try:
            res = conn.insert_rows(
                collection_name, data, partition_name=partition_name, timeout=timeout
            )
        except Exception as ex:
            raise ex from ex
        return OmitZeroDict(
            {
                "insert_count": res.insert_count,
                "ids": res.primary_keys,
                "cost": res.cost,
            }
        )

    def hybrid_search(
        self,
        collection_name: str | list[str],
        reqs: list[AnnSearchRequest],
        ranker: BaseRanker,
        limit: int = 10,
        output_fields: list[str] | None = None,
        timeout: float | None = None,
        partition_names: list[str] | None = None,
        **kwargs,
    ) -> list[list[dict]]:
        # 1. 标准成列表
        collections = [collection_name] if isinstance(collection_name, str) else collection_name

        conn = self._get_connection()
        all_hits = []
        costs = []

        # 2. 针对每个 collection 一次性跑所有 reqs
        for coll in collections:
            try:
                res = conn.hybrid_search(
                    coll,
                    reqs,
                    ranker,
                    limit=limit,
                    partition_names=partition_names,
                    output_fields=output_fields,
                    timeout=timeout,
                    **kwargs,
                )
            except Exception as ex:
                logger.error(f"混合搜索集合失败: {coll}", exc_info=True)
                continue

            # 扁平化：res 是 SearchResult，可迭代出每个 req 对应的 Hits 列表
            for hits in res:
                all_hits.extend(hits)

            # 收集 cost
            if hasattr(res, "cost"):
                costs.append(res.cost)

        # 3. 全局按 distance 降序（distance 越大越靠前）排序，取前 limit 【因为目前用的COSINE作为索引 ｜ 对于余弦相似度（COSINE），距离值越大表示相似度越高。余弦相似度的值范围在[-1, 1]之间1】
        all_hits.sort(key=lambda h: h.distance, reverse=True)
        top_hits = all_hits[:limit]
        result = [h.to_dict() for h in top_hits]

        # 4. 如果有 cost，就把它们打包到 result 的 .extra 属性
        if costs:
            from pymilvus.client.types import construct_cost_extra
            extras = [construct_cost_extra(c) for c in costs]
            extra_val = extras[0] if len(extras) == 1 else extras
            return type("ExtraList", (list,), {"extra": extra_val})(result)

        return result

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
        # Upsert into the collection.
        try:
            res = conn.upsert_rows(
                collection_name, data, partition_name=partition_name, timeout=timeout, **kwargs
            )
        except Exception as ex:
            raise ex from ex

        return OmitZeroDict(
            {
                "upsert_count": res.upsert_count,
                "cost": res.cost,
            }
        )

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
                logger.info("Successfully upserted records to Milvus")
            except Exception as e:
                logger.error("Failed to upsert records to Milvus: " + str(e))
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
        # 1. 标准化 collection 列表
        collections = [collection_name] if isinstance(collection_name, str) else collection_name

        conn = self._get_connection()
        all_hits = []
        costs = []
        recalls = []

        # 先取出一次性参数
        expr_params = kwargs.pop("filter_params", {})

        # 2. 每个 collection 单次调用 search
        for coll in collections:
            try:
                res = conn.search(
                    coll,
                    data,
                    anns_field or "",
                    search_params or {},
                    expression=filter,
                    limit=limit,
                    output_fields=output_fields,
                    partition_names=partition_names,
                    expr_params=expr_params,
                    timeout=timeout,
                    **kwargs,
                )
            except Exception as ex:
                logger.error(f"搜索集合失败: {coll}", exc_info=True)
                continue

            # 扁平化：res 对每条 query 都是一份 hits 列表
            for hits in res:
                all_hits.extend(hits)

            # 收集 cost & recalls
            if hasattr(res, "cost"):
                costs.append(res.cost)
            if hasattr(res, "recalls"):
                recalls.append(res.recalls)

        # 3. 全局排序 & 截取
        all_hits.sort(key=lambda h: h.distance, reverse=True)
        top_hits = all_hits[:limit]
        result = [h.to_dict() for h in top_hits]

        # 4. 挂 extra / recalls
        if costs or recalls:
            extra = None
            if costs:
                from pymilvus.client.types import construct_cost_extra
                extras = [construct_cost_extra(c) for c in costs]
                extra = extras[0] if len(extras) == 1 else extras
            recalls_val = None
            if recalls:
                recalls_val = recalls[0] if len(recalls) == 1 else recalls
            return type(
                "ExtraList",
                (list,),
                {"extra": extra, "recalls": recalls_val}
            )(result)

        return result

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

        if ids:
            try:
                schema_dict = conn.describe_collection(collection_name, timeout=timeout, **kwargs)
            except Exception as ex:
                logger.error("Failed to describe collection: %s", collection_name)
                raise ex from ex
            filter = self._pack_pks_expr(schema_dict, ids)

        if not output_fields:
            output_fields = ["*"]

        try:
            res = conn.query(
                collection_name,
                expr=filter,
                output_fields=output_fields,
                partition_names=partition_names,
                timeout=timeout,
                expr_params=kwargs.pop("filter_params", {}),
                **kwargs,
            )
        except Exception as ex:
            logger.error("Failed to query collection: %s", collection_name)
            raise ex from ex

        return res

    def query_iterator(
        self,
        collection_name: str,
        batch_size: int | None = 1000,
        limit: int | None = UNLIMITED,
        filter: str | None = "",
        output_fields: list[str] | None = None,
        partition_names: list[str] | None = None,
        timeout: float | None = None,
        **kwargs,
    ):
        if filter is not None and not isinstance(filter, str):
            raise DataTypeNotMatchException(message=ExceptionsMessage.ExprType % type(filter))

        conn = self._get_connection()
        # set up schema for iterator
        try:
            schema_dict = conn.describe_collection(collection_name, timeout=timeout, **kwargs)
        except Exception as ex:
            logger.error("Failed to describe collection: %s", collection_name)
            raise ex from ex

        return QueryIterator(
            connection=conn,
            collection_name=collection_name,
            batch_size=batch_size,
            limit=limit,
            expr=filter,
            output_fields=output_fields,
            partition_names=partition_names,
            schema=schema_dict,
            timeout=timeout,
            **kwargs,
        )

    def search_iterator(
        self,
        collection_name: str,
        data: list[list] | list,
        batch_size: int = 1000,
        filter: str = None,
        limit: int | None = UNLIMITED,
        output_fields: list[str] | None = None,
        search_params: dict | None = None,
        timeout: float | None = None,
        partition_names: list[str] | None = None,
        anns_field: str | None = None,
        round_decimal: int = -1,
        **kwargs,
    ) -> SearchIteratorV2 | SearchIterator:
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

        # compatibility logic, change this when support get version from server
        try:
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
        except ServerVersionIncompatibleException:
            # for compatibility, return search_iterator V1
            logger.warning(ExceptionsMessage.SearchIteratorV2FallbackWarning)
        except Exception as ex:
            raise ex from ex

        # following is the old code for search_iterator V1
        if filter is not None and not isinstance(filter, str):
            raise DataTypeNotMatchException(message=ExceptionsMessage.ExprType % type(filter))

        # set up schema for iterator
        try:
            schema_dict = conn.describe_collection(collection_name, timeout=timeout, **kwargs)
        except Exception as ex:
            logger.error("Failed to describe collection: %s", collection_name)
            raise ex from ex
        # if anns_field is not provided
        # if only one vector field, use to search
        # if multiple vector fields, raise exception and abort
        if anns_field is None or anns_field == "":
            vec_field = None
            fields = schema_dict[FIELDS]
            vec_field_count = 0
            for field in fields:
                if is_vector_type(field[TYPE]):
                    vec_field_count += 1
                    vec_field = field
            if vec_field is None:
                raise MilvusException(
                    code=ErrorCode.UNEXPECTED_ERROR,
                    message="there should be at least one vector field in milvus collection",
                )
            if vec_field_count > 1:
                raise MilvusException(
                    code=ErrorCode.UNEXPECTED_ERROR,
                    message="must specify anns_field when there are more than one vector field",
                )
            anns_field = vec_field["name"]
            if anns_field is None or anns_field == "":
                raise MilvusException(
                    code=ErrorCode.UNEXPECTED_ERROR,
                    message=f"cannot get anns_field name for search iterator, got:{anns_field}",
                )
        # set up metrics type for search_iterator which is mandatory
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
                ParamError, f"Cannot set up metrics type for anns_field:{anns_field}"
            )

        search_params["params"] = get_params(search_params)

        return SearchIterator(
            connection=self._get_connection(),
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

    # def get(
    #         self,
    #         collection_name: str,
    #         ids: list | str | int,
    #         output_fields: list[str] | None = None,
    #         timeout: float | None = None,
    #         partition_names: list[str] | None = None,
    #         **kwargs,
    # ) -> list[dict]:
    #     if not isinstance(ids, list):
    #         ids = [ids]
    #
    #     if len(ids) == 0:
    #         return []
    #
    #     conn = self._get_connection()
    #     try:
    #         schema_dict, _ = conn._get_schema_from_cache_or_remote(collection_name, timeout=timeout)
    #     except Exception as ex:
    #         logger.error("Failed to describe collection: %s", collection_name)
    #         raise ex from ex
    #
    #     if not output_fields:
    #         output_fields = ["*"]
    #
    #     expr = self._pack_pks_expr(schema_dict, ids)
    #     try:
    #         res = conn.query(
    #             collection_name,
    #             expr=expr,
    #             output_fields=output_fields,
    #             partition_names=partition_names,
    #             timeout=timeout,
    #             **kwargs,
    #         )
    #     except Exception as ex:
    #         logger.error("Failed to get collection: %s", collection_name)
    #         raise ex from ex
    #
    #     return res

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
                msg = f"wrong type of argument pks, expect list, int or str, got '{type(pk).__name__}'"
                raise TypeError(msg)

        if ids is not None:
            if isinstance(ids, (int, str)):
                pks.append(ids)
            elif isinstance(ids, list):
                for id in ids:
                    if not isinstance(id, (int, str)):
                        msg = f"wrong type of argument ids, expect list, int or str, got '{type(id).__name__}'"
                        raise TypeError(msg)
                pks.extend(ids)
            else:
                msg = f"wrong type of argument ids, expect list, int or str, got '{type(ids).__name__}'"
                raise TypeError(msg)

        # validate ambiguous delete filter param before describe collection rpc
        if filter and len(pks) > 0:
            raise ParamError(message=ExceptionsMessage.AmbiguousDeleteFilterParam)

        expr = ""
        conn = self._get_connection()
        if len(pks) > 0:
            try:
                schema_dict, _ = conn._get_schema_from_cache_or_remote(
                    collection_name, timeout=timeout
                )
            except Exception as ex:
                logger.error("Failed to describe collection: %s", collection_name)
                raise ex from ex
            expr = self._pack_pks_expr(schema_dict, pks)
        else:
            if not isinstance(filter, str):
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
            logger.error("Failed to delete primary keys in collection: %s", collection_name)
            raise ex from ex

        # compatible with deletions that returns primary keys
        if ret_pks:
            return ret_pks

        return OmitZeroDict({"delete_count": res.delete_count, "cost": res.cost})

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

    def rename_collection(
        self,
        old_name: str,
        new_name: str,
        target_db: str | None = "",
        timeout: float | None = None,
        **kwargs,
    ):
        conn = self._get_connection()
        conn.rename_collections(old_name, new_name, target_db, timeout=timeout, **kwargs)

    @classmethod
    def create_schema(cls, **kwargs):
        kwargs["check_fields"] = False  # do not check fields for now
        return CollectionSchema([], **kwargs)

    @classmethod
    def prepare_index_params(cls, field_name: str = "", **kwargs) -> IndexParams:
        index_params = IndexParams()
        if field_name:
            validate_param("field_name", field_name, str)
            index_params.add_index(field_name, **kwargs)
        return index_params

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
            logger.debug("Successfully created collection: %s", collection_name)
        except Exception as ex:
            logger.error("Failed to create collection: %s", collection_name)
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

        bm25_configs = []
        bm25_config_map = {}
        existing_field_names = set()

        # 解析动态模板中的字段信息
        for template in dynamic_templates:
            for key, value in template.items():
                match_pattern = value.get("match_pattern", "")
                # 如果是正则匹配模式，跳过直接创建字段
                if match_pattern == "regex":
                    continue

                match = value.get("match", "")
                mapping_cfg = value.get("mapping", {})
                mapping_type = mapping_cfg.get("type", "")

                # 处理向量字段的维度
                if mapping_type == "FLOAT_VECTOR":
                    dims = mapping_cfg.get("dims", 1024)
                    if match in auto_dimensions:
                        dims = auto_dimensions.get(match, 1024)
                    fields.append(FieldSchema(name=match, dtype=DataType.FLOAT_VECTOR, dim=dims))
                    existing_field_names.add(match)
                elif mapping_type == "VARCHAR":
                    max_length = mapping_cfg.get("max_length", 256)
                    is_primary = mapping_cfg.get("is_primary", False)
                    bm25_cfg = mapping_cfg.get("bm25")
                    nullable_value = not is_primary
                    if bm25_cfg and bm25_cfg.get("enable"):
                        nullable_value = False
                    enable_analyzer = mapping_cfg.get("enable_analyzer", False)
                    enable_match = mapping_cfg.get("enable_match", False)
                    analyzer_params = mapping_cfg.get("analyzer_params")

                    field_kwargs = {
                        "name": match,
                        "dtype": DataType.VARCHAR,
                        "max_length": max_length,
                        "is_primary": is_primary,
                        "nullable": nullable_value,
                    }
                    if enable_analyzer:
                        field_kwargs["enable_analyzer"] = True
                    if analyzer_params:
                        field_kwargs["analyzer_params"] = analyzer_params
                    if enable_match:
                        field_kwargs["enable_match"] = True

                    fields.append(FieldSchema(**field_kwargs))
                    existing_field_names.add(match)

                    if bm25_cfg and bm25_cfg.get("enable"):
                        sparse_field = bm25_cfg.get("output_field", f"{match}_sparse")
                        cfg_entry = {
                            "input_field": match,
                            "sparse_field": sparse_field,
                            "search_params": copy.deepcopy(bm25_cfg.get("search_params", {"drop_ratio_search": 0.1})),
                            "index_params": copy.deepcopy(bm25_cfg.get("index_params", {
                                "inverted_index_algo": "DAAT_WAND",
                                "bm25_k1": 1.5,
                                "bm25_b": 0.75
                            }))
                        }
                        bm25_configs.append(cfg_entry)
                        bm25_config_map[sparse_field] = cfg_entry
                elif mapping_type == "FLOAT":
                    fields.append(FieldSchema(name=match, dtype=DataType.FLOAT, nullable=True))
                    existing_field_names.add(match)
                elif mapping_type == "INT64":
                    fields.append(FieldSchema(name=match, dtype=DataType.INT64, nullable=True))
                    existing_field_names.add(match)
                elif mapping_type == "JSON":
                    fields.append(FieldSchema(name=match, dtype=DataType.JSON, nullable=True))
                    existing_field_names.add(match)
                elif mapping_type == "ARRAY":
                    element_type = mapping_cfg.get("element_type", DataType.VARCHAR)
                    max_length = mapping_cfg.get("max_length", 256)
                    max_capacity = mapping_cfg.get("max_capacity", 4096)
                    fields.append(FieldSchema(
                        name=match,
                        dtype=DataType.ARRAY,
                        element_type=getattr(DataType, element_type) if isinstance(element_type, str) else element_type,
                        max_length=max_length,
                        max_capacity=max_capacity,
                        nullable=True
                    ))
                    existing_field_names.add(match)

        # 添加维度特定的向量字段
        for vector_field, dim in auto_dimensions.items():
            # 跳过已添加的字段
            if vector_field in existing_field_names:
                continue

            # 如果是维度特定字段模式 (q_{dim}_vec)
            if re.match(r'q_\d+_vec', vector_field):
                fields.append(FieldSchema(name=vector_field, dtype=DataType.FLOAT_VECTOR, dim=dim))
                existing_field_names.add(vector_field)

        # 补充 BM25 稀疏向量字段
        for cfg in bm25_configs:
            if cfg["sparse_field"] in existing_field_names:
                continue
            fields.append(FieldSchema(
                name=cfg["sparse_field"],
                dtype=DataType.SPARSE_FLOAT_VECTOR
            ))
            existing_field_names.add(cfg["sparse_field"])

        # 创建集合模式
        schema = CollectionSchema(fields=fields, description="根据mapping.json创建，当前版本支持混合检索", enable_dynamic_field=True)
        for cfg in bm25_configs:
            bm25_function = Function(
                name=f"bm25_function_{cfg['sparse_field']}",
                function_type=FunctionType.BM25,
                input_field_names=[cfg["input_field"]],
                output_field_names=cfg["sparse_field"],
            )
            schema.add_function(bm25_function)
        # 创建集合
        self.create_collection(collection_name, schema=schema)
        logger.info(f"成功创建集合: {collection_name} 包含字段: {[field.name for field in fields]}")

        # 处理索引相关的逻辑
        for field in fields:
            if field.dtype == DataType.FLOAT_VECTOR:
                index_params = IndexParams()
                index_params.add_index(
                    field.name,
                    "IVF_FLAT",
                    field.name,
                    metric_type="COSINE",
                    params={"nlist": 128}
                )
                try:
                    self.create_index(collection_name, index_params)
                    # 直接把 index_params 作为 %s 参数，它会调用 __str__ 输出你的 [{…}] 列表结构
                    logger.info(
                        "索引创建成功 | 集合：%s | 索引配置：%s",
                        collection_name,
                        index_params,
                    )
                except Exception:
                    # exception 自动打印 traceback
                    logger.exception(
                        "索引创建失败 | 集合：%s | 索引配置：%s",
                        collection_name,
                        index_params,
                    )
            # 为稀疏向量创建索引 - 使用最新推荐的SPARSE_INVERTED_INDEX
            elif field.dtype == DataType.SPARSE_FLOAT_VECTOR:
                sparse_index_params = IndexParams()
                cfg = bm25_config_map.get(field.name, {})
                idx_params = cfg.get("index_params", {})
                sparse_index_params.add_index(
                    field.name,
                    "SPARSE_INVERTED_INDEX",
                    field.name,
                    metric_type=idx_params.get("metric_type", "BM25"),
                    params={
                        "inverted_index_algo": idx_params.get("inverted_index_algo", "DAAT_WAND"),
                        "bm25_k1": idx_params.get("bm25_k1", 1.5),
                        "bm25_b": idx_params.get("bm25_b", 0.75),
                    }
                )
                try:
                    self.create_index(collection_name, sparse_index_params)
                    logger.info(
                        "稀疏索引创建成功 | 集合：%s | 字段：%s | 索引配置：%s",
                        collection_name,
                        field.name,
                        sparse_index_params,
                    )
                except Exception:
                    logger.exception(
                        "稀疏索引创建失败 | 集合：%s | 字段：%s | 索引配置：%s",
                        collection_name,
                        field.name,
                        sparse_index_params,
                    )
        # 加载集合
        try:
            self.load_collection(collection_name)
            logger.info(f"集合 {collection_name} 已加载")
        except Exception as e:
            logger.warning(f"集合 {collection_name} 加载失败: {str(e)}")

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
            logger.error(f"集合 {collection_name} 不存在")
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
                logger.info(f"字段 q_{vector_dimension}_vec 已存在于集合 {collection_name} 中")
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
            logger.info(f"成功向集合 {collection_name} 添加字段 q_{vector_dimension}_vec")

            # 为新字段创建索引
            index_params = {
                "field_name": f"q_{vector_dimension}_vec",
                "index_type": "IVF_FLAT",
                "params": {"nlist": 128},
                "metric_type": "COSINE"
            }
            self.create_index(collection_name, index_params)
            logger.info(f"成功为字段 q_{vector_dimension}_vec 创建索引")

            return True
        except Exception as e:
            logger.error(f"更新集合模式失败: {e}")
            return False


    def close(self):
        connections.remove_connection(self._using)

    def _get_connection(self):
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
        **kwargs,
    ) -> str:
        """Create the connection to the Milvus server."""
        # TODO: Implement reuse with new uri style
        using = kwargs.pop("alias", None) or uuid4().hex
        try:
            connections.connect(using, user, password, db_name, token, uri=uri, **kwargs)
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

    # def _get_vector_field_name(self, schema_dict: dict):
    #     fields = schema_dict.get("fields", [])
    #     if not fields:
    #         return {}
    #
    #     for field_dict in fields:
    #         if field_dict.get("type", None) == DataType.FLOAT_VECTOR:
    #             return field_dict.get("name", "")
    #     return ""

    def _pack_pks_expr(self, schema_dict: dict, pks: list) -> str:
        primary_field = self._extract_primary_field(schema_dict)
        pk_field_name = primary_field["name"]
        data_type = primary_field["type"]

        # Varchar pks need double quotes around the values
        if data_type == DataType.VARCHAR:
            ids = ["'" + str(entry) + "'" for entry in pks]
            expr = f"""{pk_field_name} in [{",".join(ids)}]"""
        else:
            ids = [str(entry) for entry in pks]
            expr = f"{pk_field_name} in [{','.join(ids)}]"
        return expr

    def load_collection(self, collection_name: str, timeout: float | None = None, **kwargs):
        """Loads the collection."""
        conn = self._get_connection()
        try:
            conn.load_collection(collection_name, timeout=timeout, **kwargs)
        except MilvusException as ex:
            logger.error("Failed to load collection: %s", collection_name)
            raise ex from ex

    def release_collection(self, collection_name: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        try:
            conn.release_collection(collection_name, timeout=timeout, **kwargs)
        except MilvusException as ex:
            logger.error("Failed to load collection: %s", collection_name)
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

    def alter_index_properties(
        self,
        collection_name: str,
        index_name: str,
        properties: dict,
        timeout: float | None = None,
        **kwargs,
    ):
        conn = self._get_connection()
        conn.alter_index_properties(
            collection_name, index_name, properties=properties, timeout=timeout, **kwargs
        )

    def drop_index_properties(
        self,
        collection_name: str,
        index_name: str,
        property_keys: list[str],
        timeout: float | None = None,
        **kwargs,
    ):
        conn = self._get_connection()
        conn.drop_index_properties(
            collection_name, index_name, property_keys=property_keys, timeout=timeout, **kwargs
        )

    def alter_collection_properties(
        self, collection_name: str, properties: dict, timeout: float | None = None, **kwargs
    ):
        conn = self._get_connection()
        conn.alter_collection_properties(
            collection_name,
            properties=properties,
            timeout=timeout,
            **kwargs,
        )

    def drop_collection_properties(
        self,
        collection_name: str,
        property_keys: list[str],
        timeout: float | None = None,
        **kwargs,
    ):
        conn = self._get_connection()
        conn.drop_collection_properties(
            collection_name, property_keys=property_keys, timeout=timeout, **kwargs
        )

    def alter_collection_field(
        self,
        collection_name: str,
        field_name: str,
        field_params: dict,
        timeout: float | None = None,
        **kwargs,
    ):
        conn = self._get_connection()
        conn.alter_collection_field_properties(
            collection_name,
            field_name=field_name,
            field_params=field_params,
            timeout=timeout,
            **kwargs,
        )

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

    def drop_role(
        self, role_name: str, force_drop: bool = False, timeout: float | None = None, **kwargs
    ):
        conn = self._get_connection()
        conn.drop_role(role_name, force_drop=force_drop, timeout=timeout, **kwargs)

    def describe_role(self, role_name: str, timeout: float | None = None, **kwargs) -> dict:
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

    def grant_privilege_v2(
        self,
        role_name: str,
        privilege: str,
        collection_name: str,
        db_name: str | None = None,
        timeout: float | None = None,
        **kwargs,
    ):
        """Grant a privilege or a privilege group to a role.

        Args:
            role_name (``str``): The name of the role.
            privilege (``str``): The privilege or privilege group to grant.
            collection_name (``str``): The name of the collection.
            db_name (``str``, optional): The name of the database. It will use default database
                if not specified.
            timeout (``float``, optional): An optional duration of time in seconds to allow
                for the RPC. When timeout is set to None, client waits until server response
                or error occur.

        Raises:
            MilvusException: If anything goes wrong.
        """
        conn = self._get_connection()
        conn.grant_privilege_v2(
            role_name,
            privilege,
            collection_name,
            db_name=db_name,
            timeout=timeout,
            **kwargs,
        )

    def revoke_privilege_v2(
        self,
        role_name: str,
        privilege: str,
        collection_name: str,
        db_name: str | None = None,
        timeout: float | None = None,
        **kwargs,
    ):
        """Revoke a privilege or a privilege group from a role.

        Args:
            role_name (``str``): The name of the role.
            privilege (``str``): The privilege or privilege group to revoke.
            collection_name (``str``): The name of the collection.
            db_name (``str``, optional): The name of the database. It will use default database
                if not specified.
            timeout (``float``, optional): An optional duration of time in seconds to allow
                for the RPC. When timeout is set to None, client waits until server response
                or error occur.

        Raises:
            MilvusException: If anything goes wrong.
        """
        conn = self._get_connection()
        conn.revoke_privilege_v2(
            role_name,
            privilege,
            collection_name,
            db_name=db_name,
            timeout=timeout,
            **kwargs,
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

    # deprecated same to use_database
    def using_database(self, db_name: str, **kwargs):
        conn = self._get_connection()
        conn.reset_db_name(db_name)

    def use_database(self, db_name: str, **kwargs):
        conn = self._get_connection()
        conn.reset_db_name(db_name)

    def create_database(
            self,
            db_name: str,
            properties: dict | None = None,
            timeout: float | None = None,
            **kwargs,
    ):
        conn = self._get_connection()
        conn.create_database(db_name=db_name, properties=properties, timeout=timeout, **kwargs)

    def drop_database(self, db_name: str, **kwargs):
        conn = self._get_connection()
        conn.drop_database(db_name, **kwargs)

    def list_databases(self, timeout: float | None = None, **kwargs) -> list[str]:
        conn = self._get_connection()
        return conn.list_database(timeout=timeout, **kwargs)

    def describe_database(self, db_name: str, **kwargs) -> dict:
        conn = self._get_connection()
        return conn.describe_database(db_name, **kwargs)

    def alter_database_properties(self, db_name: str, properties: dict, **kwargs):
        conn = self._get_connection()
        conn.alter_database(db_name, properties, **kwargs)

    def drop_database_properties(self, db_name: str, property_keys: list[str], **kwargs):
        conn = self._get_connection()
        conn.drop_database_properties(db_name, property_keys, **kwargs)

    def flush(
        self,
        collection_name: str,
        timeout: float | None = None,
        **kwargs,
    ):
        """Seal all segments in the collection. Inserts after flushing will be written into
            new segments.

        Args:
            collection_name(``string``): The name of collection.
            timeout (float): an optional duration of time in seconds to allow for the RPCs.
                If timeout is not set, the client keeps waiting until the server
                responds or an error occurs.

        Raises:
            MilvusException: If anything goes wrong.
        """
        conn = self._get_connection()
        conn.flush([collection_name], timeout=timeout, **kwargs)

    def compact(
        self,
        collection_name: str,
        is_clustering: bool | None = False,
        timeout: float | None = None,
        **kwargs,
    ) -> int:
        """Compact merge the small segments in a collection

        Args:
            timeout (``float``, optional): An optional duration of time in seconds to allow
                for the RPC. When timeout is set to None, client waits until server response
                or error occur.

            is_clustering (``bool``, optional): Option to trigger clustering compaction.

        Raises:
            MilvusException: If anything goes wrong.

        Returns:
            int: An integer represents the server's compaction job. You can use this job ID
            for subsequent state inquiries.
        """
        conn = self._get_connection()
        return conn.compact(collection_name, is_clustering=is_clustering, timeout=timeout, **kwargs)

    def get_compaction_state(
        self,
        job_id: int,
        timeout: float | None = None,
        **kwargs,
    ) -> str:
        """Get the state of compaction job

        Args:
            timeout (``float``, optional): An optional duration of time in seconds to allow
                for the RPC. When timeout is set to None, client waits until server response
                or error occur.

        Raises:
            MilvusException: If anything goes wrong.

        Returns:
            str: the state of this compaction job. Possible values are "UndefiedState", "Executing"
            and "Completed".
        """
        conn = self._get_connection()
        result = conn.get_compaction_state(job_id, timeout=timeout, **kwargs)
        return result.state_name

    def get_server_version(
        self,
        timeout: float | None = None,
        **kwargs,
    ) -> str:
        """Get the running server's version

        Args:
            timeout (``float``, optional): A duration of time in seconds to allow for the RPC.
                If timeout is set to None, the client keeps waiting until the server
                responds or an error occurs.

        Returns:
            str: A string represent the server's version.

        Raises:
            MilvusException: If anything goes wrong
        """
        conn = self._get_connection()
        return conn.get_server_version(timeout=timeout, **kwargs)

    def create_privilege_group(
        self,
        group_name: str,
        timeout: float | None = None,
        **kwargs,
    ):
        """Create a new privilege group.

        Args:
            group_name (``str``): The name of the privilege group.
            timeout (``float``, optional): An optional duration of time in seconds to allow
                for the RPC. When timeout is set to None, client waits until server response
                or error occur.

        Raises:
            MilvusException: If anything goes wrong.
        """
        conn = self._get_connection()
        conn.create_privilege_group(group_name, timeout=timeout, **kwargs)

    def drop_privilege_group(
        self,
        group_name: str,
        timeout: float | None = None,
        **kwargs,
    ):
        """Drop a privilege group.

        Args:
            group_name (``str``): The name of the privilege group.
            timeout (``float``, optional): An optional duration of time in seconds to allow
                for the RPC. When timeout is set to None, client waits until server response
                or error occur.

        Raises:
            MilvusException: If anything goes wrong.
        """
        conn = self._get_connection()
        conn.drop_privilege_group(group_name, timeout=timeout, **kwargs)

    def list_privilege_groups(
        self,
        timeout: float | None = None,
        **kwargs,
    ) -> list[dict[str, str]]:
        """List all privilege groups.

        Args:
            timeout (``float``, optional): An optional duration of time in seconds to allow
                for the RPC. When timeout is set to None, client waits until server response
                or error occur.

        Returns:
            List[Dict[str, str]]: A list of privilege groups.

        Raises:
            MilvusException: If anything goes wrong.
        """
        conn = self._get_connection()
        try:
            res = conn.list_privilege_groups(timeout=timeout, **kwargs)
        except Exception as ex:
            logger.exception("Failed to list privilege groups.")
            raise ex from ex
        ret = []
        for g in res.groups:
            ret.append({"privilege_group": g.privilege_group, "privileges": g.privileges})
        return ret

    def add_privileges_to_group(
        self,
        group_name: str,
        privileges: list[str],
        timeout: float | None = None,
        **kwargs,
    ):
        """Add privileges to a privilege group.

        Args:
            group_name (``str``): The name of the privilege group.
            privileges (``List[str]``): A list of privileges to be added to the group.
                Privileges should be the same type in a group otherwise it will raise an exception.
            timeout (``float``, optional): An optional duration of time in seconds to allow
                for the RPC. When timeout is set to None, client waits until server response
                or error occur.

        Raises:
            MilvusException: If anything goes wrong.
        """
        conn = self._get_connection()
        conn.add_privileges_to_group(group_name, privileges, timeout=timeout, **kwargs)

    def remove_privileges_from_group(
        self,
        group_name: str,
        privileges: list[str],
        timeout: float | None = None,
        **kwargs,
    ):
        """Remove privileges from a privilege group.

        Args:
            group_name (``str``): The name of the privilege group.
            privileges (``List[str]``): A list of privileges to be removed from the group.
            timeout (``float``, optional): An optional duration of time in seconds to allow
                for the RPC. When timeout is set to None, client waits until server response
                or error occur.

        Raises:
            MilvusException: If anything goes wrong.
        """
        conn = self._get_connection()
        conn.remove_privileges_from_group(group_name, privileges, timeout=timeout, **kwargs)

    def create_resource_group(self, name: str, timeout: float | None = None, **kwargs):
        """Create a resource group
            It will success whether or not the resource group exists.

        Args:
            name: The name of the resource group.
        Raises:
            MilvusException: If anything goes wrong.
        """
        conn = self._get_connection()
        return conn.create_resource_group(name, timeout, **kwargs)

    def update_resource_groups(
        self,
        configs: dict[str, ResourceGroupConfig],
        timeout: float | None = None,
    ):
        """Update resource groups.
            This function updates the resource groups based on the provided configurations.

        Args:
            configs: A mapping of resource group names to their configurations.
            timeout: The timeout value in seconds. Defaults to None.
        Raises:
            MilvusException: If anything goes wrong.
        """
        conn = self._get_connection()
        return conn.update_resource_groups(configs, timeout)

    def drop_resource_group(
        self,
        name: str,
        timeout: float | None = None,
    ):
        """Drop a resource group
            It will success if the resource group is existed and empty, otherwise fail.

        Args:
            name: The name of the resource group.
            timeout: The timeout value in seconds. Defaults to None.
        Raises:
            MilvusException: If anything goes wrong.
        """
        conn = self._get_connection()
        return conn.drop_resource_group(name, timeout)

    def describe_resource_group(self, name: str, timeout: float | None = None):
        """Drop a resource group
            It will success if the resource group is existed and empty, otherwise fail.

        Args:
            name: The name of the resource group.
            timeout: The timeout value in seconds. Defaults to None.
        Returns:
            ResourceGroupInfo: The detail info of the resource group.
        Raises:
            MilvusException: If anything goes wrong.
        """
        conn = self._get_connection()
        return conn.describe_resource_group(name, timeout)

    def list_resource_groups(self, timeout: float | None = None):
        """list all resource group names

        Args:
            timeout: The timeout value in seconds. Defaults to None.
        Returns:
            list[str]: all resource group names
        Raises:
            MilvusException: If anything goes wrong.
        """
        conn = self._get_connection()
        return conn.list_resource_groups(timeout)

    def transfer_replica(
        self,
        source_group: str,
        target_group: str,
        collection_name: str,
        num_replicas: int,
        timeout: float | None = None,
    ):
        """transfer num_replica from source resource group to target resource group

        Args:
            source_group: source resource group name
            target_group: target resource group name
            collection_name: collection name which replica belong to
            num_replicas: transfer replica num
            timeout: The timeout value in seconds. Defaults to None.

        Raises:
            MilvusException: If anything goes wrong.
        """
        conn = self._get_connection()
        return conn.transfer_replica(
            source_group, target_group, collection_name, num_replicas, timeout
        )

    def describe_replica(
        self, collection_name: str, timeout: float | None = None, **kwargs
    ) -> list[ReplicaInfo]:
        """Get the current loaded replica information

        Args:
            collection_name (``str``): The name of the given collection.
            timeout (``float``, optional): An optional duration of time in seconds to allow
                for the RPC. When timeout is set to None, client waits until server response
                or error occur.
        Returns:
            List[ReplicaInfo]: All the replica information.
        """
        conn = self._get_connection()
        return conn.describe_replica(collection_name, timeout=timeout, **kwargs)

    def run_analyzer(
        self,
        texts: str | list[str],
        analyzer_params: str | dict | None = None,
        with_hash: bool = False,
        with_detail: bool = False,
        collection_name: str | None = None,
        field_name: str | None = None,
        analyzer_names: str | list[str] | None = None,
        timeout: float | None = None,
    ):
        """Run analyzer. Return result tokens of analysis.
        Args:
            text(``str``,``List[str]``): The input text (string or string list).
            analyzer_params(``str``,``Dict``,``None``): The parameters of analyzer.
            timeout(``float``, optional): The timeout value in seconds. Defaults to None.
        Returns:
                (``List[str]``,``List[List[str]]``): The result tokens of analysis.
        """

        return self._get_connection().run_analyzer(
            texts,
            analyzer_params=analyzer_params,
            with_hash=with_hash,
            with_detail=with_detail,
            collection_name=collection_name,
            field_name=field_name,
            analyzer_names=analyzer_names,
            timeout=timeout,
        )


    def get_cluster_stats(self):
        """
        获取 Milvus 集群统计信息

        返回集群的整体状态，包括：
        - 服务器信息（版本、类型）
        - 集合统计（数量、文档数、分片、副本等）
        - 索引信息
        - 加载状态和内存使用
        - 分区信息
        - 资源组信息
        """
        try:
            from api.utils.common import convert_bytes
            from pymilvus import Collection

            # 基础信息
            server_version = utility.get_server_version(using=self._using)
            server_type = utility.get_server_type(using=self._using)

            res = {
                'cluster_name': settings.MILVUS.get("hosts", "milvus"),
                'status': 'green',  # Milvus 连接成功即为 green
                'server_version': server_version,
                'server_type': server_type,
                'is_self_hosted': self.is_self_hosted
            }

            # 获取资源组信息（如果是自托管版本）
            try:
                resource_groups = utility.list_resource_groups(using=self._using)
                res['resource_groups'] = resource_groups
                res['resource_groups_count'] = len(resource_groups)
            except Exception as e:
                logger.debug(f"无法获取资源组信息（可能是云版本）: {e}")
                res['resource_groups'] = []
                res['resource_groups_count'] = 0

            # 获取所有集合
            collections = self.list_collections()
            res['collections'] = len(collections)

            # 统计所有集合的详细信息
            total_rows = 0
            total_fields = 0
            total_shards = 0
            total_partitions = 0
            total_indexes = 0
            total_replicas = 0
            total_loaded_collections = 0
            collection_details = []

            for collection_name in collections:
                try:
                    # 创建集合对象
                    coll = Collection(name=collection_name, using=self._using)

                    # 基础统计信息
                    stats = self.get_collection_stats(collection_name)
                    row_count = int(stats.get('row_count', 0))
                    total_rows += row_count

                    # 集合描述信息
                    desc = self.describe_collection(collection_name)
                    field_count = len(desc.get('fields', []))
                    total_fields += field_count

                    # 分片数量
                    num_shards = coll.num_shards
                    total_shards += num_shards

                    # 分区信息
                    partitions = coll.partitions
                    partition_count = len(partitions)
                    total_partitions += partition_count

                    # 索引信息
                    indexes = coll.indexes
                    index_count = len(indexes)
                    total_indexes += index_count

                    index_details = []
                    for idx in indexes:
                        try:
                            index_details.append({
                                'field_name': idx.field_name,
                                'index_type': idx.params.get('index_type', 'Unknown'),
                                'metric_type': idx.params.get('metric_type', 'Unknown')
                            })
                        except Exception:
                            pass

                    # 加载状态
                    try:
                        load_status = utility.load_state(collection_name, using=self._using)
                        is_loaded = (load_status.name == 'Loaded')
                        if is_loaded:
                            total_loaded_collections += 1
                    except Exception:
                        load_status = None
                        is_loaded = False

                    # 副本信息
                    replica_info = []
                    replica_count = 0
                    try:
                        if is_loaded:
                            replicas = coll.get_replicas()
                            replica_count = len(replicas.groups)
                            total_replicas += replica_count
                            for replica in replicas.groups:
                                replica_info.append({
                                    'replica_id': replica.id,
                                    'shard_count': len(replica.shards),
                                    'node_ids': replica.node_ids
                                })
                    except Exception as e:
                        logger.debug(f"获取集合 {collection_name} 副本信息失败: {e}")

                    # 段信息（仅对已加载的集合）
                    segment_info = None
                    if is_loaded:
                        try:
                            segments = utility.get_query_segment_info(collection_name, using=self._using)
                            segment_info = {
                                'segment_count': len(segments),
                                'total_rows': sum(seg.num_rows for seg in segments)
                            }
                        except Exception as e:
                            logger.debug(f"获取集合 {collection_name} 段信息失败: {e}")

                    # 压缩状态
                    compaction_state = None
                    try:
                        comp_state = coll.get_compaction_state()
                        compaction_state = comp_state.state.name if comp_state else None
                    except Exception:
                        pass

                    collection_details.append({
                        'name': collection_name,
                        'row_count': row_count,
                        'field_count': field_count,
                        'shard_count': num_shards,
                        'partition_count': partition_count,
                        'index_count': index_count,
                        'indexes': index_details,
                        'is_loaded': is_loaded,
                        'load_state': load_status.name if load_status else 'Unknown',
                        'replica_count': replica_count,
                        'replicas': replica_info,
                        'segment_info': segment_info,
                        'compaction_state': compaction_state
                    })

                except Exception as e:
                    logger.warning(f"获取集合 {collection_name} 的统计信息失败: {e}")
                    continue

            # 更新汇总统计信息
            res.update({
                'total_rows': total_rows,
                'total_fields': total_fields,
                'total_shards': total_shards,
                'total_partitions': total_partitions,
                'total_indexes': total_indexes,
                'total_replicas': total_replicas,
                'loaded_collections': total_loaded_collections,
                'collection_details': collection_details
            })

            logger.debug(f"MilvusConnection.get_cluster_stats: {res}")
            return res

        except Exception as e:
            logger.exception(f"MilvusConnection.get_cluster_stats: {e}")
            return None
