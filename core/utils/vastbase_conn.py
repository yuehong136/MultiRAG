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
        self.host = settings.VASTBASE.get("host", "127.0.0.1")
        self.port = settings.VASTBASE.get("port", 5433)
        self.database = settings.VASTBASE.get("database", "datav")
        self.user = settings.VASTBASE.get("user", "datav")
        self.password = settings.VASTBASE.get("password", "")
        self.max_connections = settings.VASTBASE.get("max_connections", 20)
        self.schema = settings.VASTBASE.get("schema", "public")

        self.connection_pool = None
        logger.info(f"使用 VastBase {self.host}:{self.port}/{self.database} 作为文档存储引擎")

        # 测试连接并等待健康状态
        for attempt in range(24):
            try:
                # 创建连接池
                self._create_connection_pool()

                # 测试连接
                conn = self._get_connection()
                cursor = conn.cursor()

                # 注册向量扩展
                self._register_vector_extension(conn)

                # 检查数据库健康状态
                cursor.execute("SELECT version()")
                version = cursor.fetchone()[0]
                logger.info(f"VastBase连接成功，版本: {version}")

                # 检查向量功能支持
                try:
                    cursor.execute("SELECT vb_version()")
                    vb_version = cursor.fetchone()[0]
                    if "VECTOR" not in vb_version:
                        raise Exception("VastBase不支持向量功能，请检查安装和许可证")
                    logger.info(f"VastBase向量功能可用: {vb_version}")
                except Exception as e:
                    logger.warning(f"无法检查向量功能支持: {e}")

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
            self.connection_pool = psycopg2.pool.ThreadedConnectionPool(
                1,  # minconn
                self.max_connections,  # maxconn
                host=self.host,
                port=self.port,
                database=self.database,
                user=self.user,
                password=self.password,
                cursor_factory=RealDictCursor,
                options=f'-c search_path={self.schema}'
            )
            logger.debug(f"VastBase连接池创建成功: 最大连接数 {self.max_connections}")
        except Exception as e:
            logger.error(f"创建VastBase连接池失败: {e}")
            raise

    def _get_connection(self):
        """从连接池获取连接"""
        if not self.connection_pool:
            raise Exception("连接池未初始化")
        return self.connection_pool.getconn()

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
            logger.error(f"注册VastBase向量扩展失败: {e}")
            raise

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

    def createIdx(self, indexName: str, knowledgebaseId: str, vectorSize: int):
        """
        Create an index with given name
        """
        table_name = f"{indexName}_{knowledgebaseId}"
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

    def deleteIdx(self, indexName: str, knowledgebaseId: str):
        """
        Delete an index with given name
        """
        table_name = f"{indexName}_{knowledgebaseId}"
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

    def indexExist(self, indexName: str, knowledgebaseId: str) -> bool:
        """
        Check if an index with given name exists
        """
        table_name = f"{indexName}_{knowledgebaseId}"
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
    CRUD operations - 待实现
    """

    def search(self, *args, **kwargs):
        """Search with given parameters"""
        raise NotImplementedError("Not implemented")

    def get(self, chunkId: str, indexName: str, knowledgebaseIds: list[str]) -> dict | None:
        """Get single chunk with given id"""
        raise NotImplementedError("Not implemented")

    def insert(self, rows: list[dict], indexName: str, knowledgebaseId: str) -> list[str]:
        """Update or insert a bulk of rows"""
        raise NotImplementedError("Not implemented")

    def update(self, condition: dict, newValue: dict, indexName: str, knowledgebaseId: str) -> bool:
        """Update rows with given conjunctive equivalent filtering condition"""
        raise NotImplementedError("Not implemented")

    def delete(self, condition: dict, indexName: str, knowledgebaseId: str) -> int:
        """Delete rows with given conjunctive equivalent filtering condition"""
        raise NotImplementedError("Not implemented")

    """
    Helper functions for search result - 待实现
    """

    def getTotal(self, res):
        raise NotImplementedError("Not implemented")

    def getChunkIds(self, res):
        raise NotImplementedError("Not implemented")

    def getFields(self, res, fields: list[str]) -> dict[str, dict]:
        raise NotImplementedError("Not implemented")

    def getHighlight(self, res, keywords: list[str], fieldnm: str):
        raise NotImplementedError("Not implemented")

    def getAggregation(self, res, fieldnm: str):
        raise NotImplementedError("Not implemented")

    """
    SQL - 待实现
    """

    def sql(self, sql: str, fetch_size: int, format: str):
        """Run the sql generated by text-to-sql"""
        raise NotImplementedError("Not implemented")

    """
    Milvus methods - 不需要实现
    """

    def search_by_milvus(self, *args, **kwargs):
        """Not applicable for VastBase"""
        raise NotImplementedError("VastBase不支持Milvus兼容接口")

    def describe_collection(self, *args, **kwargs):
        """Not applicable for VastBase"""
        raise NotImplementedError("VastBase不支持Milvus兼容接口")

    def has_collection(self, *args, **kwargs):
        """Not applicable for VastBase"""
        raise NotImplementedError("VastBase不支持Milvus兼容接口")

    def query(self, *args, **kwargs):
        """Not applicable for VastBase"""
        raise NotImplementedError("VastBase不支持Milvus兼容接口")