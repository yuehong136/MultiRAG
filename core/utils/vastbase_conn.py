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
    Table operations - 待实现
    """

    def createIdx(self, indexName: str, knowledgebaseId: str, vectorSize: int):
        """
        Create an index with given name
        """
        raise NotImplementedError("Not implemented")

    def deleteIdx(self, indexName: str, knowledgebaseId: str):
        """
        Delete an index with given name
        """
        raise NotImplementedError("Not implemented")

    def indexExist(self, indexName: str, knowledgebaseId: str) -> bool:
        """
        Check if an index with given name exists
        """
        raise NotImplementedError("Not implemented")

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
    Milvus compatibility methods - 不需要实现
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