import logging
import os
import re
import json
import time
import copy
import psycopg2
from psycopg2 import pool, sql
from psycopg2.extras import RealDictCursor
from core import settings
from core.settings import PAGERANK_FLD, TAG_FLD
from core.utils import singleton
import pandas as pd
from api.utils.file_utils import get_project_base_directory
from core.nlp import is_english

from core.utils.doc_store_conn import (
    DocStoreConnection,
    MatchExpr,
    MatchTextExpr,
    MatchDenseExpr,
    FusionExpr,
    OrderByExpr,
)

logger = logging.getLogger("multirag.vastbase_conn")


def field_keyword(field_name: str):
    # The "docnm_kwd" field is always a string, not list.
    if field_name == "source_id" or (field_name.endswith("_kwd") and field_name != "docnm_kwd" and field_name != "knowledge_graph_kwd"):
        return True
    return False


@singleton
class VastBaseConnection(DocStoreConnection):
    def __init__(self):
        # 从配置加载连接参数
        self.host = str(settings.VASTBASE.get("host", "127.0.0.1"))
        self.port = str(settings.VASTBASE.get("port", 5433))  # 确保是字符串
        self.database = str(settings.VASTBASE.get("database", "datav"))
        self.user = str(settings.VASTBASE.get("user", "datav"))
        self.password = str(settings.VASTBASE.get("password", ""))
        self.max_connections = settings.VASTBASE.get("max_connections", 20)
        self.schema = str(settings.VASTBASE.get("schema", "public"))

        self.connection_pool = None
        logger.info(f"使用 VastBase {self.host}:{self.port}/{self.database} 作为文档存储引擎")

        # 测试连接并等待健康状态
        for attempt in range(24):
            try:
                # 创建连接池
                self._create_connection_pool()

                # 测试基本连接
                conn = self._get_connection()
                cursor = conn.cursor()

                # 检查数据库基本连接
                cursor.execute("SELECT version()")
                version = cursor.fetchone()[0]
                logger.info(f"VastBase连接成功，版本: {version}")

                # 尝试注册向量扩展（非阻塞）
                self._register_vector_extension(conn)

                # 检查向量功能支持（可选）
                try:
                    cursor.execute("SELECT vb_version()")
                    vb_version = cursor.fetchone()[0]
                    logger.info(f"VastBase版本信息: {vb_version}")
                except Exception as e:
                    logger.debug(f"无法获取VastBase版本信息（可能是旧版本）: {e}")

                cursor.close()
                self._release_connection(conn)
                break

            except Exception as e:
                logger.warning(f"{str(e)}. 等待VastBase {self.host}:{self.port} 恢复健康状态... (尝试 {attempt + 1}/24)")
                time.sleep(5)
        else:
            # 所有尝试都失败了
            msg = f"VastBase {self.host}:{self.port} 在120秒内无法建立连接"
            logger.error(msg)
            raise Exception(msg)

        # 最终健康检查
        try:
            health_status = self.health()
            if health_status["status"] != "green":
                msg = f"VastBase {self.host}:{self.port} 健康检查失败: {health_status}"
                logger.error(msg)
                raise Exception(msg)
            logger.info(f"VastBase {self.host}:{self.port} 健康状态良好")
        except Exception as e:
            msg = f"VastBase {self.host}:{self.port} 最终健康检查失败: {str(e)}"
            logger.error(msg)
            raise Exception(msg)

        # 加载表结构配置
        try:
            self.table_schema = self._load_table_schema()
            logger.info(f"加载VastBase表结构配置成功: {len(self.table_schema)} 个字段")
        except Exception as e:
            logger.warning(f"加载表结构配置失败: {e}")
            self.table_schema = {}

    def _create_connection_pool(self):
        """创建数据库连接池"""
        try:
            # 不使用cursor_factory，避免在连接池级别出问题
            self.connection_pool = psycopg2.pool.ThreadedConnectionPool(
                1,  # minconn
                self.max_connections,  # maxconn
                host=self.host,
                port=self.port,
                database=self.database,
                user=self.user,
                password=self.password,
                options=f'-c search_path={self.schema}'
            )
            logger.debug(f"VastBase连接池创建成功: 最大连接数 {self.max_connections}")
        except Exception as e:
            logger.error(f"创建VastBase连接池失败: {e}")
            raise

    def _get_connection(self):
        """从连接池获取连接，并验证连接有效性"""
        if not self.connection_pool:
            raise Exception("连接池未初始化")

        conn = self.connection_pool.getconn()

        # 验证连接是否有效
        try:
            if conn.closed:
                # 连接已关闭，标记为坏连接并重新获取
                logger.debug("连接已关闭，尝试重新获取")
                self.connection_pool.putconn(conn, close=True)
                conn = self.connection_pool.getconn()
            else:
                # 测试连接是否可用
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.close()
        except Exception as e:
            logger.warning(f"连接验证失败，尝试重新获取: {e}")
            try:
                self.connection_pool.putconn(conn, close=True)
            except Exception:
                pass
            # 重新创建连接池中的连接
            try:
                conn = self.connection_pool.getconn()
                # 再次验证新连接
                if conn.closed:
                    raise Exception("无法获取有效连接")
            except Exception as e2:
                logger.error(f"重新获取连接失败: {e2}")
                raise

        return conn

    def _release_connection(self, conn):
        """释放连接回连接池"""
        if self.connection_pool and conn:
            self.connection_pool.putconn(conn)

    def _register_vector_extension(self, conn):
        """注册向量扩展（仅在需要时）"""
        try:
            # 先测试是否已经支持向量类型
            cursor = conn.cursor()
            try:
                # 尝试创建一个临时向量来测试扩展是否已注册
                cursor.execute("SELECT '[1,2,3]'::floatvector")
                cursor.fetchone()
                logger.debug("VastBase向量扩展已可用，无需重新注册")
                cursor.close()
                return
            except Exception:
                # 向量扩展未注册，需要注册
                cursor.close()
                pass

            # 导入并注册VastBase向量扩展
            from vastbase.psycopg2 import register_vector
            register_vector(conn)
            logger.debug("VastBase向量扩展注册成功")
        except ImportError as e:
            logger.error("无法导入vastbase.psycopg2模块，请安装pyvector-vastbase包")
            raise Exception("缺少VastBase向量扩展依赖: pyvector-vastbase") from e
        except Exception as e:
            # 向量扩展注册失败不应该阻止基本连接
            logger.warning(f"注册VastBase向量扩展失败（可以稍后重试）: {e}")

    def _load_table_schema(self) -> dict:
        """加载表结构配置"""
        mapping_path = os.path.join(get_project_base_directory(), "configs", "vastbase_mapping.json")
        if not os.path.exists(mapping_path):
            logger.warning(f"VastBase mapping文件不存在: {mapping_path}")
            return {}

        try:
            with open(mapping_path, "r", encoding="utf-8") as f:
                schema = json.load(f)
            logger.debug(f"成功加载VastBase表结构配置: {mapping_path}")
            return schema
        except Exception as e:
            logger.error(f"读取VastBase mapping文件失败: {e}")
            return {}

    """
    Database operations
    """

    def dbType(self) -> str:
        return "vastbase"

    def health(self) -> dict:
        """
        Return the health status of the database.
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            result = cursor.fetchone()
            self._release_connection(conn)

            status = "green" if result else "red"
            return {
                "type": "vastbase",
                "status": status,
                "error": None,
            }
        except Exception as e:
            return {
                "type": "vastbase",
                "status": "red",
                "error": str(e),
            }

    """
    Table operations
    """

    def createIdx(self, indexName: str | list[str], knowledgebaseId: str, vectorSize: int):
        """
        Create an index with given name
        """
        # 处理索引名称参数
        if isinstance(indexName, list):
            if not indexName:  # 如果是空列表
                logger.error("索引名称列表为空")
                return
            table_name = f"{indexName[0]}_{knowledgebaseId}"  # 取第一个元素
            logger.debug(f"索引名称是列表，使用第一个元素: {indexName[0]}")
        elif isinstance(indexName, str):
            table_name = f"{indexName}_{knowledgebaseId}"
        else:
            logger.error(f"索引名称必须是字符串或字符串列表，收到: {type(indexName)}")
            return
        conn = None

        try:
            conn = self._get_connection()
            self._register_vector_extension(conn)
            cursor = conn.cursor()

            # 验证向量维度
            if not (1 <= vectorSize <= 16384):
                raise ValueError(f"向量维度必须在1-16384之间，当前: {vectorSize}")

            # 检查表是否已存在
            if self._table_exists(cursor, table_name):
                logger.info(f"VastBase表 {table_name} 已存在，跳过创建")
                return

            # 构建表结构SQL
            create_table_sql = self._build_create_table_sql(table_name, vectorSize)

            logger.info(f"创建VastBase表: {table_name}, 向量维度: {vectorSize}")
            cursor.execute(create_table_sql)

            # 创建向量索引
            self._create_vector_index(cursor, table_name, vectorSize)

            # 创建全文索引
            self._create_fulltext_indexes(cursor, table_name)

            conn.commit()
            logger.info(f"VastBase表 {table_name} 创建成功")

        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"创建VastBase表 {table_name} 失败: {e}")
            raise
        finally:
            if conn:
                self._release_connection(conn)

    def deleteIdx(self, indexName: str | list[str], knowledgebaseId: str):
        """
        Delete an index with given name
        """
        # 处理索引名称参数
        if isinstance(indexName, list):
            if not indexName:  # 如果是空列表
                logger.error("索引名称列表为空")
                return
            table_name = f"{indexName[0]}_{knowledgebaseId}"  # 取第一个元素
            logger.debug(f"索引名称是列表，使用第一个元素: {indexName[0]}")
        elif isinstance(indexName, str):
            table_name = f"{indexName}_{knowledgebaseId}"
        else:
            logger.error(f"索引名称必须是字符串或字符串列表，收到: {type(indexName)}")
            return
        conn = None

        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # 检查表是否存在
            if not self._table_exists(cursor, table_name):
                logger.warning(f"VastBase表 {table_name} 不存在，跳过删除")
                return

            # 删除表（CASCADE会自动删除所有关联的索引）
            drop_sql = f"DROP TABLE IF EXISTS {self.schema}.{table_name} CASCADE"
            logger.info(f"删除VastBase表: {table_name}")
            cursor.execute(drop_sql)

            conn.commit()
            logger.info(f"VastBase表 {table_name} 删除成功")

        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"删除VastBase表 {table_name} 失败: {e}")
            raise
        finally:
            if conn:
                self._release_connection(conn)

    def indexExist(self, indexName: str | list[str], knowledgebaseId: str = None) -> bool:
        """
        Check if an index with given name exists
        """
        # 处理索引名称参数
        if isinstance(indexName, list):
            if not indexName:  # 如果是空列表
                logger.error("索引名称列表为空")
                return False
            table_name = f"{indexName[0]}_{knowledgebaseId}"  # 取第一个元素
            logger.debug(f"索引名称是列表，使用第一个元素: {indexName[0]}")
        elif isinstance(indexName, str):
            table_name = f"{indexName}_{knowledgebaseId}"
        else:
            logger.error(f"索引名称必须是字符串或字符串列表，收到: {type(indexName)}")
            return False
        conn = None

        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            exists = self._table_exists(cursor, table_name)
            logger.debug(f"VastBase表 {table_name} 存在检查: {exists}")
            return exists

        except Exception as e:
            logger.error(f"检查VastBase表 {table_name} 是否存在失败: {e}")
            return False
        finally:
            if conn:
                self._release_connection(conn)

    def _table_exists(self, cursor, table_name: str) -> bool:
        """检查表是否存在"""
        check_sql = """
            SELECT EXISTS (
                SELECT 1 FROM pg_tables
                WHERE schemaname = %s AND tablename = %s
            )
        """
        cursor.execute(check_sql, (self.schema, table_name))
        return cursor.fetchone()[0]

    def _build_create_table_sql(self, table_name: str, vectorSize: int) -> str:
        """构建创建表的SQL语句"""
        # 从配置加载基础字段
        schema = self.table_schema.copy()

        # 添加动态向量字段
        vector_column = f"q_{vectorSize}_vec"
        schema[vector_column] = {"type": f"floatvector({vectorSize})"}

        # 构建字段定义
        columns = []
        primary_keys = []

        for field_name, field_config in schema.items():
            field_type = field_config["type"]

            # 处理主键
            if field_config.get("primary_key", False):
                columns.append(f"{field_name} {field_type}")
                primary_keys.append(field_name)
            else:
                # 处理默认值
                default_value = field_config.get("default")
                if default_value is not None:
                    if isinstance(default_value, str):
                        columns.append(f"{field_name} {field_type} DEFAULT '{default_value}'")
                    else:
                        columns.append(f"{field_name} {field_type} DEFAULT {default_value}")
                else:
                    columns.append(f"{field_name} {field_type}")

        # 添加主键约束
        if primary_keys:
            columns.append(f"PRIMARY KEY ({', '.join(primary_keys)})")

        # 添加时间戳字段
        columns.append("created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        columns.append("updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

        create_sql = f"""
            CREATE TABLE {self.schema}.{table_name} (
                {',\n                '.join(columns)}
            )
        """

        logger.debug(f"表创建SQL: {create_sql}")
        return create_sql

    def _create_vector_index(self, cursor, table_name: str, vectorSize: int):
        """创建向量索引"""
        vector_column = f"q_{vectorSize}_vec"
        index_name = f"idx_{table_name}_vector"

        # 创建Graph_Index索引（HNSW算法）
        index_sql = f"""
            CREATE INDEX {index_name}
            ON {self.schema}.{table_name}
            USING graph_index({vector_column} floatvector_l2_ops)
            WITH (m=16, ef_construction=64)
        """

        try:
            logger.debug(f"创建向量索引: {index_name}")
            cursor.execute(index_sql)
            logger.info(f"向量索引 {index_name} 创建成功")
        except Exception as e:
            logger.warning(f"创建向量索引 {index_name} 失败: {e}")
            # 向量索引创建失败不影响表创建

    def _create_fulltext_indexes(self, cursor, table_name: str):
        """创建全文搜索索引（VastBase fulltext索引，基于BM25）"""
        # 获取需要全文索引的字段
        fulltext_fields = []
        for field_name, field_config in self.table_schema.items():
            if field_config.get("fulltext", False):
                fulltext_fields.append(field_name)

        if not fulltext_fields:
            logger.debug(f"表 {table_name} 没有需要创建全文索引的字段")
            return

        # VastBase fulltext索引支持多列索引
        # 创建一个综合的全文索引，包含所有需要全文检索的字段
        index_name = f"idx_{table_name}_fulltext"
        columns_str = ", ".join(fulltext_fields)

        # 创建VastBase fulltext索引（使用默认BM25算法）
        # 使用默认词典和默认参数
        index_sql = f"""
            CREATE INDEX {index_name}
            ON {self.schema}.{table_name}
            USING fulltext({columns_str})
        """

        try:
            logger.debug(f"创建VastBase fulltext索引: {index_name} on fields: {columns_str}")
            cursor.execute(index_sql)
            logger.info(f"VastBase fulltext索引 {index_name} 创建成功")
        except Exception as e:
            logger.warning(f"创建VastBase fulltext索引 {index_name} 失败: {e}")
            # 全文索引创建失败不影响表创建
            # 可能的原因：1) VastBase版本不支持 2) 字段类型不兼容

    """
    CRUD operations
    """

    def search(
        self,
        selectFields: list[str],
        highlightFields: list[str],
        condition: dict,
        matchExprs: list[MatchExpr],
        orderBy: OrderByExpr,
        offset: int,
        limit: int,
        indexNames: str | list[str],
        knowledgebaseIds: list[str],
        aggFields: list[str] = [],
        rank_feature: dict | None = None,
    ) -> tuple[list[dict], int]:
        """
        Search with given conjunctive equivalent filtering condition and return all fields of matched documents

        Args:
            selectFields: 要返回的字段列表
            highlightFields: 要高亮的字段列表
            condition: 过滤条件字典
            matchExprs: 匹配表达式列表（如向量查询、全文查询）
            orderBy: 排序表达式
            offset: 分页偏移量
            limit: 结果数量限制
            indexNames: 索引名称（表名前缀）
            knowledgebaseIds: 知识库ID列表
            aggFields: 聚合字段
            rank_feature: PageRank等特征调整

        Returns:
            tuple: (结果列表, 总命中数)
        """
        if isinstance(indexNames, str):
            indexNames = indexNames.split(",")
        assert isinstance(indexNames, list) and len(indexNames) > 0

        # 准备输出字段
        output_fields = selectFields.copy()
        for essential_field in ["id"] + aggFields:
            if essential_field not in output_fields:
                output_fields.append(essential_field)
        # 移除特殊字段（不在数据库中）
        output_fields = [f for f in output_fields if f != "_score"]

        if limit <= 0:
            limit = 10000

        # 分类匹配表达式
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

        has_retrieval = bool(text_exprs or dense_expr)

        conn = None
        try:
            conn = self._get_connection()
            self._register_vector_extension(conn)
            cursor = conn.cursor()

            all_results = []
            total_hits_count = 0

            # 遍历所有表
            for indexName in indexNames:
                for kb_id in knowledgebaseIds:
                    table_name = f"{indexName}_{kb_id}"

                    # 检查表是否存在
                    if not self._table_exists(cursor, table_name):
                        logger.debug(f"表 {table_name} 不存在，跳过")
                        continue

                    # 获取表的实际列名
                    actual_columns = self._get_table_columns(cursor, table_name)
                    # 过滤输出字段，只保留实际存在的列
                    valid_output = [f for f in output_fields if f in actual_columns]

                    if has_retrieval:
                        # 混合检索
                        results, hits = self._search_with_retrieval(
                            cursor=cursor,
                            table_name=table_name,
                            select_fields=valid_output,
                            condition=condition,
                            text_exprs=text_exprs,
                            dense_expr=dense_expr,
                            fusion_expr=fusion_expr,
                            limit=limit + offset,
                            rank_feature=rank_feature,
                        )
                    else:
                        # 纯条件过滤
                        results, hits = self._search_with_filter_only(
                            cursor=cursor,
                            table_name=table_name,
                            select_fields=valid_output,
                            condition=condition,
                            order_by=orderBy,
                            limit=limit + offset,
                        )

                    all_results.extend(results)
                    total_hits_count += hits

            # 如果有检索，需要重新排序和融合
            if has_retrieval and all_results:
                # 按 _score 降序排序
                all_results.sort(key=lambda x: x.get("_score", 0.0), reverse=True)

            # 应用偏移和限制
            if offset > 0:
                all_results = all_results[offset:]
            if limit > 0:
                all_results = all_results[:limit]

            logger.debug(f"VastBase search 返回 {len(all_results)} 条结果, 总命中 {total_hits_count}")
            return all_results, total_hits_count

        except Exception as e:
            logger.error(f"VastBase search 失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return [], 0

        finally:
            if conn:
                self._release_connection(conn)

    def _get_table_columns(self, cursor, table_name: str) -> set[str]:
        """获取表的所有列名"""
        cursor.execute(f"""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
        """, (self.schema, table_name))
        return {row[0] for row in cursor.fetchall()}

    def _search_with_filter_only(
        self,
        cursor,
        table_name: str,
        select_fields: list[str],
        condition: dict,
        order_by: OrderByExpr,
        limit: int,
    ) -> tuple[list[dict], int]:
        """纯条件过滤搜索（无向量/全文检索）"""
        # 构建 WHERE 子句
        where_clauses, params = self._build_where_clause(condition)
        where_str = " AND ".join(where_clauses) if where_clauses else "1=1"

        # 构建 SELECT 字段
        select_str = ", ".join(select_fields) if select_fields else "*"

        # 构建 ORDER BY 子句
        order_str = ""
        if order_by and order_by.fields:
            order_parts = []
            for field, direction in order_by.fields:
                order_parts.append(f"{field} {'ASC' if direction == 0 else 'DESC'}")
            order_str = "ORDER BY " + ", ".join(order_parts)

        # 先统计总数
        count_sql = f"SELECT COUNT(*) FROM {self.schema}.{table_name} WHERE {where_str}"
        cursor.execute(count_sql, params)
        total_count = cursor.fetchone()[0]

        # 查询数据
        query_sql = f"""
            SELECT {select_str}
            FROM {self.schema}.{table_name}
            WHERE {where_str}
            {order_str}
            LIMIT {limit}
        """
        cursor.execute(query_sql, params)

        # 获取列名
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()

        results = []
        for row in rows:
            doc = dict(zip(columns, row))
            doc = self._deserialize_document(doc)
            results.append(doc)

        return results, total_count

    def _search_with_retrieval(
        self,
        cursor,
        table_name: str,
        select_fields: list[str],
        condition: dict,
        text_exprs: list[MatchTextExpr],
        dense_expr: MatchDenseExpr | None,
        fusion_expr: FusionExpr | None,
        limit: int,
        rank_feature: dict | None = None,
    ) -> tuple[list[dict], int]:
        """混合检索搜索（向量 + 全文）"""
        # 构建基础过滤条件
        where_clauses, filter_params = self._build_where_clause(condition)
        filter_str = " AND ".join(where_clauses) if where_clauses else ""

        text_results = {}
        text_scores = {}
        dense_results = {}
        dense_scores = {}

        # 执行全文检索
        if text_exprs:
            text_results, text_scores = self._execute_text_search(
                cursor=cursor,
                table_name=table_name,
                select_fields=select_fields,
                text_exprs=text_exprs,
                filter_str=filter_str,
                filter_params=filter_params,
                limit=limit,
            )

        # 执行向量检索
        if dense_expr:
            dense_results, dense_scores = self._execute_dense_search(
                cursor=cursor,
                table_name=table_name,
                select_fields=select_fields,
                dense_expr=dense_expr,
                filter_str=filter_str,
                filter_params=filter_params,
                limit=limit,
            )

        # 融合结果
        combined_results = self._fuse_results(
            text_results=text_results,
            text_scores=text_scores,
            dense_results=dense_results,
            dense_scores=dense_scores,
            dense_expr=dense_expr,
            fusion_expr=fusion_expr,
            rank_feature=rank_feature,
        )

        total_hits = len(combined_results)
        return combined_results, total_hits

    def _execute_text_search(
        self,
        cursor,
        table_name: str,
        select_fields: list[str],
        text_exprs: list[MatchTextExpr],
        filter_str: str,
        filter_params: list,
        limit: int,
    ) -> tuple[dict[str, dict], dict[str, float]]:
        """执行全文检索（BM25）"""
        results_map = {}
        scores_map = {}

        for expr in text_exprs:
            # 构建查询字符串（用于 @~@ 操作符）
            query_text = expr.matching_text

            # 处理 extra_options 中的参数
            param_parts = []
            if expr.extra_options:
                minimum_should_match = expr.extra_options.get("minimum_should_match")
                if minimum_should_match is not None:
                    # VastBase MINIMUM_SHOULD_MATCH 支持的格式：
                    # - 正整数: 3 (固定匹配3个词项)
                    # - 负整数: -1 (允许缺失1个词项)
                    # - 正百分比: "75%" (至少匹配75%)
                    # - 负百分比: "-25%" (允许缺失25%)
                    if isinstance(minimum_should_match, float):
                        # 浮点数转换为百分比格式: 0.3 -> "30%"
                        if 0 < minimum_should_match < 1:
                            minimum_should_match = f"{int(minimum_should_match * 100)}%"
                        elif minimum_should_match >= 1:
                            # 大于1的浮点数转为整数
                            minimum_should_match = int(minimum_should_match)
                        else:
                            # 小于等于0的浮点数无效，跳过
                            minimum_should_match = None
                    elif isinstance(minimum_should_match, int) and minimum_should_match == 0:
                        # 0 无效，跳过
                        minimum_should_match = None

                    if minimum_should_match is not None:
                        param_parts.append(f"PARAM:MINIMUM_SHOULD_MATCH={minimum_should_match}")

            # 构建带参数的查询字符串
            if param_parts:
                query_text = f"{query_text} @<{' '.join(param_parts)}>@"

            # 遍历字段进行查询
            for field_expr in expr.fields:
                field_name, boost = self._parse_field_weight(field_expr)

                # 检查字段是否存在
                table_columns = self._get_table_columns(cursor, table_name)
                if field_name not in table_columns:
                    logger.debug(f"字段 {field_name} 不在表 {table_name} 中，跳过")
                    continue

                select_str = ", ".join(select_fields) if select_fields else "*"

                # 构建完整的查询 SQL
                # 添加 BOOST 参数
                actual_query = query_text
                if boost != 1.0:
                    if "@<PARAM:" in actual_query:
                        # 已有参数，添加 BOOST
                        actual_query = actual_query.replace(">@", f" PARAM:BOOST={boost}>@")
                    else:
                        actual_query = f"{actual_query} @<PARAM:BOOST={boost}>@"

                sql = f"""
                    SELECT {select_str}, bm25_score() as _bm25_score
                    FROM {self.schema}.{table_name}
                    WHERE {field_name} @~@ %s
                """
                params = [actual_query]

                if filter_str:
                    sql += f" AND {filter_str}"
                    params.extend(filter_params)

                sql += f" ORDER BY _bm25_score DESC NULLS LAST LIMIT {limit}"

                try:
                    cursor.execute(sql, params)
                    columns = [desc[0] for desc in cursor.description]
                    rows = cursor.fetchall()

                    for row in rows:
                        doc = dict(zip(columns, row))
                        doc_id = doc.get("id")
                        if not doc_id:
                            continue

                        score = float(doc.pop("_bm25_score", 0.0))

                        # 累加分数（多字段查询时）
                        if doc_id in scores_map:
                            scores_map[doc_id] += score
                        else:
                            scores_map[doc_id] = score
                            results_map[doc_id] = self._deserialize_document(doc)

                except Exception as e:
                    logger.warning(f"VastBase 全文检索失败 field={field_name}, table={table_name}: {e}")
                    # 回滚当前事务以允许后续查询继续
                    try:
                        cursor.connection.rollback()
                    except Exception:
                        pass
                    continue

        return results_map, scores_map

    def _execute_dense_search(
        self,
        cursor,
        table_name: str,
        select_fields: list[str],
        dense_expr: MatchDenseExpr,
        filter_str: str,
        filter_params: list,
        limit: int,
    ) -> tuple[dict[str, dict], dict[str, float]]:
        """执行向量检索"""
        results_map = {}
        scores_map = {}

        vector_field = dense_expr.vector_column_name
        vector_data = dense_expr.embedding_data
        distance_type = dense_expr.distance_type.upper()

        # 确保向量数据是列表格式
        if hasattr(vector_data, 'tolist'):
            vector_data = vector_data.tolist()

        # 检查向量字段是否存在
        table_columns = self._get_table_columns(cursor, table_name)
        if vector_field not in table_columns:
            logger.debug(f"向量字段 {vector_field} 不在表 {table_name} 中，跳过")
            return results_map, scores_map

        # 选择距离操作符
        if distance_type in ("COSINE", "COS"):
            distance_op = "<=>"
        elif distance_type in ("L2", "EUCLIDEAN"):
            distance_op = "<->"
        elif distance_type in ("IP", "INNER_PRODUCT"):
            distance_op = "<#>"
        else:
            distance_op = "<=>"  # 默认余弦距离

        select_str = ", ".join(select_fields) if select_fields else "*"

        sql = f"""
            SELECT {select_str}, {vector_field} {distance_op} %s as _vector_distance
            FROM {self.schema}.{table_name}
        """
        params = [vector_data]

        if filter_str:
            sql += f" WHERE {filter_str}"
            params.extend(filter_params)

        sql += f" ORDER BY _vector_distance LIMIT {limit}"

        try:
            cursor.execute(sql, params)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()

            for row in rows:
                doc = dict(zip(columns, row))
                doc_id = doc.get("id")
                if not doc_id:
                    continue

                distance = float(doc.pop("_vector_distance", 0.0))
                scores_map[doc_id] = distance
                results_map[doc_id] = self._deserialize_document(doc)

        except Exception as e:
            logger.warning(f"VastBase 向量检索失败 table={table_name}: {e}")
            # 回滚当前事务以允许后续查询继续
            try:
                cursor.connection.rollback()
            except Exception:
                pass

        return results_map, scores_map

    def _fuse_results(
        self,
        text_results: dict[str, dict],
        text_scores: dict[str, float],
        dense_results: dict[str, dict],
        dense_scores: dict[str, float],
        dense_expr: MatchDenseExpr | None,
        fusion_expr: FusionExpr | None,
        rank_feature: dict | None = None,
    ) -> list[dict]:
        """融合全文检索和向量检索结果"""
        combined = []

        # 确定融合权重
        text_weight, vector_weight = self._get_fusion_weights(fusion_expr)

        # 归一化分数
        norm_text = self._normalize_scores(text_scores, reverse=False)  # BM25 越大越好

        # 向量距离：根据距离类型决定是否反转
        if dense_expr:
            distance_type = dense_expr.distance_type.upper()
            # L2/Euclidean 距离越小越好，需要反转
            # Cosine/IP 在 VastBase 中使用的是距离（越小越好），也需要反转
            is_distance = True  # VastBase 返回的都是距离
            norm_vector = self._normalize_scores(dense_scores, reverse=is_distance)
        else:
            norm_vector = {}

        # 合并所有文档 ID
        all_doc_ids = set(text_scores.keys()) | set(dense_scores.keys())

        # 合并结果
        all_results = {**text_results, **dense_results}

        for doc_id in all_doc_ids:
            if doc_id not in all_results:
                continue

            doc = all_results[doc_id].copy()

            # 计算融合分数
            text_score = norm_text.get(doc_id, 0.0)
            vector_score = norm_vector.get(doc_id, 0.0)

            # 如果只有一种检索，直接使用该分数
            if text_scores and not dense_scores:
                final_score = text_score
            elif dense_scores and not text_scores:
                final_score = vector_score
            else:
                final_score = text_weight * text_score + vector_weight * vector_score

            # 应用 rank_feature 加权
            if rank_feature and PAGERANK_FLD in doc:
                pagerank = float(doc.get(PAGERANK_FLD, 0))
                pagerank_weight = rank_feature.get(PAGERANK_FLD, 1.0)
                final_score += pagerank * pagerank_weight

            doc["_score"] = final_score
            combined.append(doc)

        # 按分数降序排序
        combined.sort(key=lambda x: x.get("_score", 0.0), reverse=True)

        return combined

    def _get_fusion_weights(self, fusion_expr: FusionExpr | None) -> tuple[float, float]:
        """获取融合权重"""
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

    @staticmethod
    def _normalize_scores(score_map: dict[str, float], reverse: bool = False) -> dict[str, float]:
        """
        使用 Min-Max 归一化分数
        :param score_map: 文档ID到分数的映射
        :param reverse: 是否反转分数（例如距离越小越好，需要反转）
        """
        import numpy as np

        if not score_map:
            return {}

        keys = list(score_map.keys())
        values = np.array([float(score_map[k]) for k in keys], dtype=np.float64)

        if len(values) == 1:
            return {keys[0]: 1.0}

        min_v = np.min(values)
        max_v = np.max(values)

        # 如果分值差异过小，返回 1.0
        if max_v - min_v < 1e-9:
            return {k: 1.0 for k in keys}

        if reverse:
            # 距离越小分数越高
            normalized = (max_v - values) / (max_v - min_v)
        else:
            # 分数越大越好
            normalized = (values - min_v) / (max_v - min_v)

        return {k: float(normalized[i]) for i, k in enumerate(keys)}

    @staticmethod
    def _parse_field_weight(field_expr: str) -> tuple[str, float]:
        """解析字段权重表达式（如 'title^2.0'）"""
        if "^" in field_expr:
            fname, boost = field_expr.split("^", 1)
            try:
                return fname, float(boost)
            except ValueError:
                return fname, 1.0
        return field_expr, 1.0

    def get(self, chunkId: str, indexName: str | list[str], knowledgebaseIds: list[str]) -> dict | None:
        """
        Get single chunk with given id

        Args:
            chunkId: 文档块ID
            indexName: 索引名称（字符串或字符串列表）
            knowledgebaseIds: 知识库ID列表

        Returns:
            dict | None: 文档数据字典，如果不存在返回None
        """
        # 处理索引名称参数
        if isinstance(indexName, list):
            if not indexName:  # 如果是空列表
                logger.error("索引名称列表为空")
                return None
            base_index_name = indexName[0]  # 取第一个元素
            logger.debug(f"索引名称是列表，使用第一个元素: {indexName[0]}")
        elif isinstance(indexName, str):
            base_index_name = indexName
        else:
            logger.error(f"索引名称必须是字符串或字符串列表，收到: {type(indexName)}")
            return None

        if not isinstance(knowledgebaseIds, list):
            logger.error("knowledgebaseIds必须是列表")
            return None

        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # 遍历所有知识库ID，查找文档
            for kb_id in knowledgebaseIds:
                table_name = f"{base_index_name}_{kb_id}"

                # 检查表是否存在
                if not self._table_exists(cursor, table_name):
                    logger.warning(f"表 {table_name} 不存在，跳过")
                    continue

                # 查询文档
                query_sql = f"""
                    SELECT * FROM {self.schema}.{table_name}
                    WHERE id = %s
                    LIMIT 1
                """

                cursor.execute(query_sql, (chunkId,))
                result = cursor.fetchone()

                if result:
                    # 找到文档，构建字典并进行数据反序列化
                    # 获取列名
                    columns = [desc[0] for desc in cursor.description]
                    doc = dict(zip(columns, result))
                    doc = self._deserialize_document(doc)
                    logger.debug(f"从 {table_name} 找到文档 {chunkId}")
                    return doc

            logger.debug(f"在所有知识库中未找到文档 {chunkId}")
            return None

        except Exception as e:
            logger.error(f"获取文档 {chunkId} 失败: {e}")
            return None

        finally:
            if conn:
                self._release_connection(conn)

    def insert(self, documents: list[dict], indexName: str | list[str], knowledgebaseId: str = None) -> list[str]:
        """
        Insert or update a bulk of documents

        Args:
            documents: 要插入的数据列表 # 原本是 rows，为了和task_executor中的兼容，将参数名改为 documents
            indexName: 索引名称（字符串或字符串列表）
            knowledgebaseId: 知识库ID

        Returns:
            list[str]: 空列表表示成功，否则返回错误信息列表
        """
        # 处理索引名称参数
        if isinstance(indexName, list):
            if not indexName:  # 如果是空列表
                logger.error("索引名称列表为空")
                return ["索引名称列表为空"]
            table_name = f"{indexName[0]}_{knowledgebaseId}"
            logger.debug(f"索引名称是列表，使用第一个元素: {indexName[0]}")
        elif isinstance(indexName, str):
            table_name = f"{indexName}_{knowledgebaseId}"
        else:
            logger.error(f"索引名称必须是字符串或字符串列表，收到: {type(indexName)}")
            return [f"索引名称类型错误: {type(indexName)}"]

        if not documents:
            logger.warning("没有数据需要插入")
            return []

        conn = None
        try:
            conn = self._get_connection()
            self._register_vector_extension(conn)
            cursor = conn.cursor()

            # 检查表是否存在，不存在则创建
            if not self._table_exists(cursor, table_name):
                # 从数据中推断向量维度
                vector_size = 0
                pattern = re.compile(r"q_(\d+)_vec")

                for row in documents:
                    for key in row.keys():
                        match = pattern.match(key)
                        if match:
                            vector_size = int(match.group(1))
                            break
                    if vector_size > 0:
                        break

                if vector_size == 0:
                    raise ValueError(f"无法从数据中确定向量维度，无法创建表 {table_name}")

                logger.info(f"表 {table_name} 不存在，自动创建（向量维度: {vector_size}）")
                # 释放当前连接，因为createIdx会获取新连接
                self._release_connection(conn)
                self.createIdx(indexName, knowledgebaseId, vector_size)
                # 重新获取连接
                conn = self._get_connection()
                self._register_vector_extension(conn)
                cursor = conn.cursor()

            # 获取表结构信息
            cursor.execute(f"""
                SELECT column_name, data_type, udt_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
            """, (self.schema, table_name))

            columns_info = {}
            for row in cursor.fetchall():
                # 由于没有使用RealDictCursor，需要手动构建字典
                columns_info[row[0]] = {
                    'column_name': row[0],
                    'data_type': row[1],
                    'udt_name': row[2]
                }

            # 预处理数据
            processed_rows = []
            for row in documents:
                new_row = copy.deepcopy(row)

                # 移除不需要存储的内部字段
                new_row.pop("pk", None)
                new_row.pop("_id", None)
                new_row.pop("vector", None)  # 移除通用vector字段

                # 验证必须有id字段
                if "id" not in new_row:
                    raise ValueError("每条记录必须包含'id'字段")

                # 处理特殊字段
                for k, v in list(new_row.items()):
                    # 处理关键词字段（以_kwd结尾的字段，列表转字符串）
                    if field_keyword(k):
                        if isinstance(v, list):
                            new_row[k] = "###".join(str(item) for item in v)
                        else:
                            new_row[k] = str(v) if v is not None else ""

                    # 处理特征字段（以_feas结尾，转JSON字符串）
                    elif re.search(r"_feas$", k):
                        new_row[k] = json.dumps(v) if v is not None else "{}"

                    # 处理kb_id字段
                    elif k == "kb_id":
                        if isinstance(v, list):
                            new_row[k] = v[0] if v else ""
                        else:
                            new_row[k] = str(v) if v is not None else ""

                    # 处理位置信息字段（嵌套数组展平为十六进制字符串）
                    elif k == "position_int":
                        if isinstance(v, list) and v:
                            # 展平嵌套结构（支持列表、元组等可迭代对象）
                            flat_array = []
                            for item in v:
                                if isinstance(item, (list, tuple)):
                                    flat_array.extend(item)
                                else:
                                    flat_array.append(item)
                            new_row[k] = "_".join(f"{num:08x}" for num in flat_array)
                        else:
                            new_row[k] = ""

                    # 处理页码和顶部位置字段
                    elif k in ["page_num_int", "top_int"]:
                        if isinstance(v, list) and v:
                            new_row[k] = "_".join(f"{num:08x}" for num in v)
                        else:
                            new_row[k] = ""

                    # 向量字段保持原样
                    elif k.endswith("_vec"):
                        # 确保向量是列表格式
                        if isinstance(v, list):
                            new_row[k] = v
                        else:
                            new_row[k] = list(v) if hasattr(v, '__iter__') else v

                    # 其他字段保持原样
                    else:
                        new_row[k] = v

                processed_rows.append(new_row)

            # 先删除已存在的记录（使用UPSERT模式）
            ids = [row["id"] for row in processed_rows]
            if ids:
                # 构建删除语句
                placeholders = ",".join(["%s"] * len(ids))
                delete_sql = f"DELETE FROM {self.schema}.{table_name} WHERE id IN ({placeholders})"

                try:
                    cursor.execute(delete_sql, ids)
                    deleted_count = cursor.rowcount
                    if deleted_count > 0:
                        logger.debug(f"删除了 {deleted_count} 条现有记录")
                except Exception as e:
                    logger.debug(f"删除现有记录时出错（可能不存在）: {e}")

            # 批量插入新记录
            # 获取所有字段名（从第一条记录）
            if processed_rows:
                fields = list(processed_rows[0].keys())

                # 构建INSERT语句
                columns_str = ", ".join(fields)
                placeholders = ", ".join(["%s"] * len(fields))
                insert_sql = f"""
                    INSERT INTO {self.schema}.{table_name} ({columns_str})
                    VALUES ({placeholders})
                """

                # 准备批量插入数据
                insert_data = []
                for row in processed_rows:
                    values = [row.get(field) for field in fields]
                    insert_data.append(values)

                # 执行批量插入
                from psycopg2.extras import execute_batch
                execute_batch(cursor, insert_sql, insert_data)

                conn.commit()
                logger.info(f"成功插入 {len(processed_rows)} 条记录到 {table_name}")
                logger.debug(f"VastBase inserted into {table_name}, ids: {ids[:5]}{'...' if len(ids) > 5 else ''}")

            return []  # 成功返回空列表

        except Exception as e:
            if conn:
                conn.rollback()
            error_msg = f"插入到 {table_name} 失败: {str(e)}"
            logger.error(error_msg)
            return [error_msg]  # 返回错误信息

        finally:
            if conn:
                self._release_connection(conn)

    def update(self, condition: dict, newValue: dict, indexName: str | list[str], knowledgebaseId: str) -> bool:
        """
        Update rows with given conjunctive equivalent filtering condition

        Args:
            condition: 更新条件字典
            newValue: 新值字典（支持remove和add特殊操作）
            indexName: 索引名称（字符串或字符串列表）
            knowledgebaseId: 知识库ID

        Returns:
            bool: 更新是否成功
        """
        # 处理索引名称参数
        if isinstance(indexName, list):
            if not indexName:  # 如果是空列表
                logger.error("索引名称列表为空")
                return False
            table_name = f"{indexName[0]}_{knowledgebaseId}"
            logger.debug(f"索引名称是列表，使用第一个元素: {indexName[0]}")
        elif isinstance(indexName, str):
            table_name = f"{indexName}_{knowledgebaseId}"
        else:
            logger.error(f"索引名称必须是字符串或字符串列表，收到: {type(indexName)}")
            return False

        conn = None
        try:
            conn = self._get_connection()
            self._register_vector_extension(conn)
            cursor = conn.cursor()

            # 检查表是否存在
            if not self._table_exists(cursor, table_name):
                logger.warning(f"表 {table_name} 不存在，无法更新")
                return False

            # 获取表结构信息
            cursor.execute(f"""
                SELECT column_name, data_type, column_default
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
            """, (self.schema, table_name))

            columns_info = {}
            for row in cursor.fetchall():
                # 由于没有使用RealDictCursor，需要手动构建字典
                columns_info[row[0]] = {
                    'column_name': row[0],
                    'data_type': row[1],
                    'column_default': row[2]
                }

            # 深拷贝newValue以避免修改原始数据
            update_values = copy.deepcopy(newValue)

            # 移除不需要存储的内部字段
            update_values.pop("pk", None)
            update_values.pop("_id", None)
            update_values.pop("vector", None)  # 移除通用vector字段

            # 处理特殊操作
            remove_operations = {}
            add_operations = {}

            # 提取remove操作
            if "remove" in update_values:
                remove_val = update_values.pop("remove")
                if isinstance(remove_val, str):
                    # 移除整个字段（设置为默认值）
                    if remove_val in columns_info:
                        col_info = columns_info[remove_val]
                        default_val = col_info.get('column_default')
                        if default_val:
                            update_values[remove_val] = default_val
                        elif col_info['data_type'] in ['character varying', 'text']:
                            update_values[remove_val] = ""
                        elif col_info['data_type'] in ['integer', 'bigint']:
                            update_values[remove_val] = 0
                        elif col_info['data_type'] in ['double precision', 'real']:
                            update_values[remove_val] = 0.0
                elif isinstance(remove_val, dict):
                    # 从列表字段中移除特定元素
                    remove_operations = remove_val

            # 提取add操作
            if "add" in update_values:
                add_val = update_values.pop("add")
                if isinstance(add_val, dict):
                    add_operations = add_val

            # 处理remove和add操作（需要先查询再更新）
            if remove_operations or add_operations:
                # 构建WHERE条件来查询需要更新的记录
                where_clauses, params = self._build_where_clause(condition)

                if where_clauses:
                    where_str = " AND ".join(where_clauses)
                    select_sql = f"SELECT * FROM {self.schema}.{table_name} WHERE {where_str}"

                    cursor.execute(select_sql, params)
                    rows_to_update = cursor.fetchall()

                    # 获取列名
                    columns = [desc[0] for desc in cursor.description]

                    for row in rows_to_update:
                        row_dict = dict(zip(columns, row))
                        row_id = row_dict['id']

                        # 处理remove操作
                        for field, value_to_remove in remove_operations.items():
                            if field in row_dict and row_dict[field]:
                                # 反序列化字段值
                                if field_keyword(field):
                                    current_values = row_dict[field].split("###") if row_dict[field] else []
                                    if value_to_remove in current_values:
                                        current_values.remove(value_to_remove)
                                    update_values[field] = "###".join(current_values)

                        # 处理add操作
                        for field, value_to_add in add_operations.items():
                            if field in row_dict:
                                # 反序列化字段值
                                if field_keyword(field):
                                    current_values = row_dict[field].split("###") if row_dict[field] else []
                                    if value_to_add not in current_values:
                                        current_values.append(value_to_add)
                                    update_values[field] = "###".join(current_values)
                                elif isinstance(row_dict[field], str):
                                    update_values[field] = row_dict[field] + str(value_to_add)
                            else:
                                update_values[field] = value_to_add

                        # 更新这一行（如果有remove/add操作产生的更新）
                        if remove_operations or add_operations:
                            self._update_single_row(cursor, table_name, row_id, update_values)

            # 预处理更新值
            processed_values = {}
            for k, v in update_values.items():
                # 处理关键词字段
                if field_keyword(k):
                    if isinstance(v, list):
                        processed_values[k] = "###".join(str(item) for item in v)
                    else:
                        processed_values[k] = str(v) if v is not None else ""

                # 处理特征字段
                elif re.search(r"_feas$", k):
                    processed_values[k] = json.dumps(v) if v is not None else "{}"

                # 处理kb_id字段
                elif k == "kb_id":
                    if isinstance(v, list):
                        processed_values[k] = v[0] if v else ""
                    else:
                        processed_values[k] = str(v) if v is not None else ""

                # 处理位置信息字段
                elif k == "position_int":
                    if isinstance(v, list) and v:
                        # 展平嵌套结构（支持列表、元组等可迭代对象）
                        flat_array = []
                        for item in v:
                            if isinstance(item, (list, tuple)):
                                flat_array.extend(item)
                            else:
                                flat_array.append(item)
                        processed_values[k] = "_".join(f"{num:08x}" for num in flat_array)
                    else:
                        processed_values[k] = ""

                # 处理页码和顶部位置字段
                elif k in ["page_num_int", "top_int"]:
                    if isinstance(v, list) and v:
                        processed_values[k] = "_".join(f"{num:08x}" for num in v)
                    else:
                        processed_values[k] = ""

                # 向量字段保持原样
                elif k.endswith("_vec"):
                    if isinstance(v, list):
                        processed_values[k] = v
                    else:
                        processed_values[k] = list(v) if hasattr(v, '__iter__') else v

                # 其他字段保持原样
                else:
                    processed_values[k] = v

            # 如果有常规更新值，执行批量更新
            if processed_values:
                # 构建WHERE条件
                where_clauses, params = self._build_where_clause(condition)

                if not where_clauses:
                    logger.warning("没有有效的更新条件")
                    return False

                where_str = " AND ".join(where_clauses)

                # 构建SET子句
                set_clauses = []
                update_params = []
                for k, v in processed_values.items():
                    set_clauses.append(f"{k} = %s")
                    update_params.append(v)

                # 合并参数
                all_params = update_params + params

                # 构建UPDATE语句
                set_str = ", ".join(set_clauses)
                update_sql = f"""
                    UPDATE {self.schema}.{table_name}
                    SET {set_str}
                    WHERE {where_str}
                """

                logger.debug(f"执行更新: {update_sql}")
                cursor.execute(update_sql, all_params)
                updated_count = cursor.rowcount

                logger.info(f"在 {table_name} 中更新了 {updated_count} 条记录")

            conn.commit()
            return True

        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"更新 {table_name} 失败: {e}")
            return False

        finally:
            if conn:
                self._release_connection(conn)

    def _build_where_clause(self, condition: dict) -> tuple[list[str], list]:
        """
        构建WHERE子句

        Returns:
            tuple: (where_clauses列表, 参数列表)
        """
        where_clauses = []
        params = []

        for k, v in condition.items():
            # 跳过内部字段
            if k in ("_id", "pk", "vector") or v is None:
                continue

            # 处理exists条件
            if k == "exists":
                where_clauses.append(f"{v} IS NOT NULL AND {v} != ''")
                continue

            # 处理must_not条件
            if k == "must_not":
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        if kk == "exists":
                            where_clauses.append(f"({vv} IS NULL OR {vv} = '')")
                continue

            # 处理列表值（IN条件）
            if isinstance(v, list):
                if not v:
                    continue
                placeholders = ",".join(["%s"] * len(v))
                where_clauses.append(f"{k} IN ({placeholders})")
                params.extend(v)

            # 处理字符串值
            elif isinstance(v, str):
                where_clauses.append(f"{k} = %s")
                params.append(v)

            # 处理数字值
            elif isinstance(v, (int, float)):
                where_clauses.append(f"{k} = %s")
                params.append(v)

            else:
                logger.warning(f"不支持的条件类型: {k}={v} (type={type(v)})")

        return where_clauses, params

    def _update_single_row(self, cursor, table_name: str, row_id: str, update_values: dict):
        """更新单行记录"""
        if not update_values:
            return

        set_clauses = []
        params = []

        for k, v in update_values.items():
            set_clauses.append(f"{k} = %s")
            params.append(v)

        params.append(row_id)

        set_str = ", ".join(set_clauses)
        update_sql = f"""
            UPDATE {self.schema}.{table_name}
            SET {set_str}
            WHERE id = %s
        """

        cursor.execute(update_sql, params)

    def _deserialize_document(self, doc: dict) -> dict:
        """
        反序列化文档数据（将存储格式转换为应用格式）

        Args:
            doc: 原始文档字典

        Returns:
            dict: 反序列化后的文档字典
        """
        result = {}

        for k, v in doc.items():
            # 跳过内部字段
            if k in ['created_at', 'updated_at']:
                continue

            # 处理关键词字段
            if field_keyword(k):
                if v:
                    result[k] = [item for item in v.split("###") if item]
                else:
                    result[k] = []

            # 处理特征字段
            elif re.search(r"_feas$", k):
                if v:
                    result[k] = json.loads(v)
                else:
                    result[k] = {}

            # 处理位置信息字段
            elif k == "position_int":
                if v:
                    arr = [int(hex_val, 16) for hex_val in v.split("_")]
                    # 转换回嵌套数组（每5个一组）
                    result[k] = [arr[i:i + 5] for i in range(0, len(arr), 5)]
                else:
                    result[k] = []

            # 处理页码和顶部位置字段
            elif k in ["page_num_int", "top_int"]:
                if v:
                    result[k] = [int(hex_val, 16) for hex_val in v.split("_")]
                else:
                    result[k] = []

            # 向量字段保持原样（已经是列表）
            elif k.endswith("_vec"):
                result[k] = list(v) if v is not None else []

            # 其他字段保持原样
            else:
                result[k] = v

        return result

    def delete(self, condition: dict, indexName: str | list[str], knowledgebaseId: str) -> int:
        """
        Delete rows with given conjunctive equivalent filtering condition

        Args:
            condition: 删除条件字典
            indexName: 索引名称（字符串或字符串列表）
            knowledgebaseId: 知识库ID

        Returns:
            int: 删除的记录数
        """
        # 处理索引名称参数
        if isinstance(indexName, list):
            if not indexName:  # 如果是空列表
                logger.error("索引名称列表为空")
                return 0
            table_name = f"{indexName[0]}_{knowledgebaseId}"
            logger.debug(f"索引名称是列表，使用第一个元素: {indexName[0]}")
        elif isinstance(indexName, str):
            table_name = f"{indexName}_{knowledgebaseId}"
        else:
            logger.error(f"索引名称必须是字符串或字符串列表，收到: {type(indexName)}")
            return 0

        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # 检查表是否存在
            if not self._table_exists(cursor, table_name):
                logger.warning(f"表 {table_name} 不存在，跳过删除")
                return 0

            # 构建WHERE条件
            where_clauses = []
            params = []

            for k, v in condition.items():
                # 跳过内部字段
                if k in ("_id", "pk", "vector") or v is None:
                    continue

                # 处理exists条件（特殊处理）
                if k == "exists":
                    # PostgreSQL: 字段不为NULL且不为空
                    where_clauses.append(f"{v} IS NOT NULL AND {v} != ''")
                    continue

                # 处理must_not条件
                if k == "must_not":
                    if isinstance(v, dict):
                        for kk, vv in v.items():
                            if kk == "exists":
                                # 字段为NULL或为空
                                where_clauses.append(f"({vv} IS NULL OR {vv} = '')")
                    continue

                # 处理列表值（IN条件）
                if isinstance(v, list):
                    if not v:
                        continue
                    placeholders = ",".join(["%s"] * len(v))
                    where_clauses.append(f"{k} IN ({placeholders})")
                    params.extend(v)

                # 处理字符串值
                elif isinstance(v, str):
                    where_clauses.append(f"{k} = %s")
                    params.append(v)

                # 处理数字值
                elif isinstance(v, (int, float)):
                    where_clauses.append(f"{k} = %s")
                    params.append(v)

                else:
                    logger.warning(f"不支持的条件类型: {k}={v} (type={type(v)})")

            # 构建DELETE语句
            if where_clauses:
                where_str = " AND ".join(where_clauses)
                delete_sql = f"DELETE FROM {self.schema}.{table_name} WHERE {where_str}"
            else:
                logger.warning("没有有效的删除条件")
                return 0

            logger.debug(f"执行删除: {delete_sql}, params: {params}")
            cursor.execute(delete_sql, params)
            deleted_count = cursor.rowcount

            conn.commit()
            logger.info(f"从 {table_name} 删除了 {deleted_count} 条记录")
            return deleted_count

        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"从 {table_name} 删除记录失败: {e}")
            return 0

        finally:
            if conn:
                self._release_connection(conn)

    """
    Helper functions for search result
    """

    def getTotal(self, res: tuple[list[dict], int] | list[dict]) -> int:
        """
        从搜索结果中获取总命中数

        Args:
            res: search 方法返回的结果，可能是 (results, total) 元组或直接是结果列表

        Returns:
            int: 总命中数
        """
        if isinstance(res, tuple):
            return res[1]
        return len(res)

    def getChunkIds(self, res: tuple[list[dict], int] | list[dict]) -> list[str]:
        """
        从搜索结果中提取所有文档块 ID

        Args:
            res: search 方法返回的结果

        Returns:
            list[str]: 文档块 ID 列表
        """
        if isinstance(res, tuple):
            results = res[0]
        else:
            results = res

        return [doc.get("id", "") for doc in results if doc.get("id")]

    def getFields(self, res: tuple[list[dict], int] | list[dict], fields: list[str]) -> dict[str, dict]:
        """
        从搜索结果中提取指定字段，按文档 ID 组织

        Args:
            res: search 方法返回的结果
            fields: 要提取的字段列表

        Returns:
            dict[str, dict]: {doc_id: {field: value, ...}, ...}
        """
        if isinstance(res, tuple):
            results = res[0]
        else:
            results = res

        if not fields:
            return {}

        result_dict = {}
        fields_set = set(fields)
        fields_set.add("id")  # 确保 id 字段被包含

        for doc in results:
            doc_id = doc.get("id")
            if not doc_id:
                continue

            # 提取指定字段
            field_values = {}
            for field in fields_set:
                if field in doc:
                    field_values[field] = doc[field]
                else:
                    field_values[field] = None

            result_dict[doc_id] = field_values

        return result_dict

    def getHighlight(
        self,
        res: tuple[list[dict], int] | list[dict],
        keywords: list[str],
        fieldnm: str
    ) -> dict[str, str]:
        """
        为搜索结果生成高亮文本

        VastBase 不支持原生高亮，这里手动实现关键词高亮

        Args:
            res: search 方法返回的结果
            keywords: 要高亮的关键词列表
            fieldnm: 要高亮的字段名

        Returns:
            dict[str, str]: {doc_id: highlighted_text, ...}
        """
        if isinstance(res, tuple):
            results = res[0]
        else:
            results = res

        highlight_dict = {}

        for doc in results:
            doc_id = doc.get("id")
            if not doc_id:
                continue

            text = doc.get(fieldnm, "")
            if not text or not isinstance(text, str):
                highlight_dict[doc_id] = str(text) if text else ""
                continue

            # 检查是否已经有高亮标记
            if re.search(r"<em>[^<>]+</em>", text, flags=re.IGNORECASE | re.MULTILINE):
                highlight_dict[doc_id] = text
                continue

            # 清理换行符
            txt = re.sub(r"[\r\n]", " ", text, flags=re.IGNORECASE | re.MULTILINE)

            # 按句子分割
            sentences = []
            for sentence in re.split(r"[.?!;。？！；\n]", txt):
                sentence = sentence.strip()
                if not sentence:
                    continue

                highlighted_sentence = sentence

                # 判断是否为英文
                if is_english([sentence]):
                    # 英文：单词边界匹配
                    for word in keywords:
                        pattern = r"(^|[ .?/'\"\\(\\)!,:;-])(%s)([ .?/'\"\\(\\)!,:;-])" % re.escape(word)
                        highlighted_sentence = re.sub(
                            pattern,
                            r"\1<em>\2</em>\3",
                            highlighted_sentence,
                            flags=re.IGNORECASE | re.MULTILINE,
                        )
                else:
                    # 中文：按关键词长度降序匹配
                    for word in sorted(keywords, key=len, reverse=True):
                        if word:
                            highlighted_sentence = re.sub(
                                re.escape(word),
                                f"<em>{word}</em>",
                                highlighted_sentence,
                                flags=re.IGNORECASE | re.MULTILINE,
                            )

                # 只保留有高亮的句子
                if re.search(r"<em>[^<>]+</em>", highlighted_sentence, flags=re.IGNORECASE | re.MULTILINE):
                    sentences.append(highlighted_sentence)

            if sentences:
                highlight_dict[doc_id] = "...".join(sentences)
            else:
                highlight_dict[doc_id] = text

        return highlight_dict

    def getAggregation(
        self,
        res: tuple[list[dict], int] | list[dict],
        fieldnm: str
    ) -> list[list]:
        """
        手动聚合指定字段的值（因为 VastBase 不提供原生聚合）

        Args:
            res: search 方法返回的结果
            fieldnm: 要聚合的字段名

        Returns:
            list[list]: [[value, count], ...] 按计数降序排列
        """
        from collections import Counter

        if isinstance(res, tuple):
            results = res[0]
        else:
            results = res

        if not results:
            return []

        tag_counter = Counter()

        for doc in results:
            value = doc.get(fieldnm)
            if value is None:
                continue

            # 处理不同的值类型
            if isinstance(value, str):
                # 处理 ### 分隔的标签字段
                if "###" in value:
                    tags = [tag.strip() for tag in value.split("###") if tag.strip()]
                else:
                    # 尝试逗号分隔
                    tags = [tag.strip() for tag in value.split(",") if tag.strip()]

                for tag in tags:
                    if tag:
                        tag_counter[tag] += 1

            elif isinstance(value, list):
                for item in value:
                    if item and isinstance(item, str):
                        tag_counter[item.strip()] += 1
                    elif item:
                        tag_counter[str(item)] += 1

            else:
                # 其他类型直接转字符串
                tag_counter[str(value)] += 1

        # 返回按计数降序排列的列表
        return [[tag, count] for tag, count in tag_counter.most_common()]

    """
    SQL
    """

    def sql(self, sql_str: str, fetch_size: int = 100, format: str = "json"):
        """
        执行自定义 SQL 查询（用于 text-to-sql 场景）

        Args:
            sql_str: SQL 查询语句
            fetch_size: 每次获取的行数
            format: 返回格式（"json" 或 "raw"）

        Returns:
            查询结果
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute(sql_str)

            if cursor.description is None:
                # 非查询语句（如 INSERT、UPDATE、DELETE）
                conn.commit()
                return {"affected_rows": cursor.rowcount}

            # 获取列名
            columns = [desc[0] for desc in cursor.description]

            # 获取结果
            rows = cursor.fetchmany(fetch_size)
            results = []

            while rows:
                for row in rows:
                    if format == "json":
                        results.append(dict(zip(columns, row)))
                    else:
                        results.append(row)
                rows = cursor.fetchmany(fetch_size)

            return results

        except Exception as e:
            logger.error(f"执行 SQL 失败: {e}")
            raise

        finally:
            if conn:
                self._release_connection(conn)