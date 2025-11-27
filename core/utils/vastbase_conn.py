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
    CRUD operations - 待实现
    """

    def search(self, *args, **kwargs):
        """Search with given parameters"""
        raise NotImplementedError("Not implemented")

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