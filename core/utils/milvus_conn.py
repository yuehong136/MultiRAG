# coding=utf-8
"""
@project: multirag
@Author：龙
@file： milvus_conn.py
@date：2024/7/31 10:12
@desc:
"""
import logging
from uuid import uuid4
from datetime import datetime

import polars as pl
from pymilvus.client.constants import DEFAULT_CONSISTENCY_LEVEL
from pymilvus.client.types import ExceptionsMessage, LoadState
from pymilvus.exceptions import (
    DataTypeNotMatchException,
    MilvusException,
    ParamError,
    PrimaryKeyException,
)
from pymilvus.milvus_client.index import IndexParams
from pymilvus.orm import utility
from pymilvus.orm.collection import CollectionSchema, FieldSchema
from pymilvus.orm.connections import connections
from pymilvus.orm.types import DataType
from pymilvus import __version__

from core import settings
from core.utils.doc_store_conn import DocStoreConnection, MatchExpr, OrderByExpr, MatchDenseExpr

logging.info("Milvus version: " + str(__version__))


class MilvusConnection(DocStoreConnection):
    def __init__(
            self,
            uri: str = settings.MILVUS.get("hosts", ""),
            user: str = settings.MILVUS.get("username", ""),
            password: str = settings.MILVUS.get("password", ""),
            db_name: str = "",
            token: str = "",
            timeout: float | None = None,
            **kwargs,
    ) -> None:
        self._using = self._create_connection(
            uri, user, password, db_name, token, timeout=timeout, **kwargs
        )
        self.is_self_hosted = bool(utility.get_server_type(using=self._using) == "milvus")

    """
        Implementing DocStoreConnection abstract methods
        """

    def dbType(self) -> str:
        return "milvus"

    def health(self) -> dict:
        try:
            version = utility.get_server_version(using=self._using)
            return {"type": "milvus", "status": "green", "version": version}
        except MilvusException as e:
            return {"type": "milvus", "status": "red", "error": str(e)}

    def createIdx(self, indexName: str, knowledgebaseId: str, vectorSize: int):
        return self.create_collection(indexName, dimension=vectorSize)

    def deleteIdx(self, indexName: str, knowledgebaseId: str):
        return self.drop_collection(indexName)

    def indexExist(self, indexName: str, knowledgebaseId: str) -> bool:
        return self.has_collection(indexName)

    # def insert(self, rows: list[dict], indexName: str, knowledgebaseId: str) -> list[str]:
    #     res = self.insert(indexName, rows)
    #     return res.get("ids", [])

    def get(self, chunkId: str, indexName: str, knowledgebaseIds: list[str]) -> dict | None:
        result = self.get_collection_data(indexName, chunkId)
        return result[0] if result else None

    def search(
            self, selectFields: list[str], highlightFields: list[str], condition: dict,
            matchExprs: list[MatchExpr], orderBy: OrderByExpr, offset: int, limit: int,
            indexNames: str | list[str], knowledgebaseIds: list[str]
    ) -> list[dict] | pl.DataFrame:
        data = [expr.query_data for expr in matchExprs if isinstance(expr, MatchDenseExpr)]
        if not data:
            raise ValueError("No valid dense vector query data found in matchExprs")
        search_res = self.search(indexNames[0], data, limit=limit)
        return search_res

    def delete(self, condition: dict, indexName: str, knowledgebaseId: str) -> int:
        ids = condition.get("id", [])
        if not ids:
            return 0
        delete_res = self.delete(indexName, ids=ids)
        return delete_res.get("delete_count", 0)

    def getTotal(self, res):
        return len(res)

    def getChunkIds(self, res):
        return [hit["id"] for hit in res]

    def getFields(self, res, fields: list[str]) -> dict[str, dict]:
        return {d["id"]: {f: d.get(f) for f in fields} for d in res}

    def getAggregation(self, res, fieldnm: str) -> list[tuple]:
        """
        Milvus 不支持直接的聚合操作，因此这个方法需要自定义实现。
        """
        logging.warning("Aggregation is not natively supported in Milvus.")
        return []  # 返回空列表或您可以添加自定义聚合逻辑

    def getHighlight(self, res, keywords: list[str], fieldnm: str) -> dict[str, str]:
        """
        Milvus 不支持高亮操作，因此此方法需要自定义实现。
        """
        logging.warning("Highlight is not natively supported in Milvus.")
        return {}  # 返回空字典或自定义实现

    def sql(self, sql: str, fetch_size: int, format: str):
        """
        执行一个伪SQL操作，通过将SQL语句转换为Milvus兼容的查询来实现。

        :param sql: SQL样式的查询字符串
        :param fetch_size: 要检索的记录数
        :param format: 输出结果的格式
        :return: 结果集或适当的错误消息
        """
        try:
            # 简化的翻译逻辑（可以替换为实际解析逻辑）
            # 这里只是一个简单示例，通过检查SQL中的基本关键词来模拟功能
            if "SELECT" in sql.upper():
                # 提取查询的基本部分（例如字段、条件、限制条件）
                # 此处为示例，实际使用中需要完整的SQL解析并转换为Milvus查询
                collection_name = "target_collection"  # 从SQL解析或提供默认值
                limit = fetch_size  # 使用fetch_size作为查询的限制

                # 在此处根据解析的SQL结构使用`query`或`search`执行查询
                # 此示例中data只是一个示例数据，实际情况应替换为解析后的向量数据
                data = [[0.1, 0.2, 0.3, 0.4]]  # 示例数据，用于搜索，可替换为实际数据
                results = self.search(collection_name=collection_name, data=data, limit=limit)
                return results
            else:
                raise ValueError("此简化SQL功能仅支持SELECT操作。")

        except Exception as e:
            logging.error(f"执行SQL失败: {sql}. 错误: {str(e)}")
            return {"error": str(e)}

    def update(self, condition: dict, newValue: dict, indexName: str, knowledgebaseId: str) -> bool:
        """
        使用“查询原始数据 + 删除 + 重新插入”来模拟更新操作。
        自动更新时间字段 create_time 和 create_timestamp_flt。

        :param condition: 查询条件，指定要更新的记录
        :param newValue: 更新后的新数据
        :param indexName: 集合名称
        :param knowledgebaseId: 知识库ID
        :return: 操作是否成功
        """
        if "pk" not in condition:
            raise ValueError("Update operation requires 'pk' in condition")

        # Step 1: 获取集合连接
        conn = self._get_connection()
        try:
            collection_info = conn.describe_collection(indexName)
        except Exception as e:
            logging.error(f"Failed to get collection: {indexName}. Error: {e}")
            return False

        # Step 2: 查询原始记录
        try:
            expr = f"pk == '{condition['pk']}'"
            results = conn.query(indexName, expr=expr, output_fields=["*"])
            if not results:
                logging.error(f"No record found with pk: {condition['pk']}")
                return False
            original_data = results[0]  # 获取查到的第一条记录
            logging.info(f"Original data fetched for update: {original_data}")
        except Exception as e:
            logging.error(f"Failed to fetch original record for update: {e}")
            return False

        # Step 3: 合并用户提供的数据与原始数据
        merged_data = {**original_data, **newValue}  # 用新数据覆盖原始数据

        # Step 4: 自动更新时间字段
        current_time = datetime.now()
        merged_data["create_time"] = current_time.strftime("%Y-%m-%d %H:%M:%S")
        merged_data["create_timestamp_flt"] = current_time.timestamp()
        logging.info(
            f"Updated time fields: create_time={merged_data['create_time']}, create_timestamp_flt={merged_data['create_timestamp_flt']}")

        # Step 5: 删除原始记录
        try:
            delete_res = conn.delete(indexName, expression=expr)
            logging.info(f"Deleted record with condition: {expr}")
        except Exception as e:
            logging.error(f"Failed to delete record(s) for update: {e}")
            return False

        # Step 6: 插入更新后的记录
        try:
            insert_res = self.insert(indexName, data=[merged_data])
            logging.info(f"Inserted updated record: {merged_data}")
            return True
        except Exception as e:
            logging.error(f"Failed to insert updated record(s): {e}")
            return False

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

    def upsert(
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
            return {"upsert_count": 0}

        conn = self._get_connection()
        try:
            res = conn.upsert_rows(
                collection_name, data, partition_name=partition_name, timeout=timeout, **kwargs
            )
        except Exception as ex:
            raise ex from ex

        return {"upsert_count": res.upsert_count}

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

    def search(
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
                timeout=timeout,
                **kwargs,
                )
        except Exception as ex:
            logging.error("Failed to search collection: %s", collection_name)
            raise ex from ex

        ret = []
        for hits in res:
            query_result = []
            for hit in hits:
                query_result.append(hit.to_dict())
            ret.append(query_result)

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
            filter: str | None = "",
            partition_name: str | None = None,
            **kwargs,
    ) -> dict:
        """
        删除指定集合中的数据。

        :param collection_name: 集合名称。
        :param ids: 要删除的行的主键ID，可以是单个ID或ID列表。
        :param timeout: 操作的超时时间。
        :param filter: 查询过滤条件。
        :param partition_name: 分区名称。
        :param kwargs: 其他参数，包括pks（主键）。
        :return: 删除操作的结果，包括删除的主键列表或删除计数。
        """
        # 从kwargs中获取pks，如果不存在则默认为空列表
        pks = kwargs.get("pks", [])
        # 如果pks是单个int或str类型，将其转换为列表
        if isinstance(pks, (int, str)):
            pks = [pks]

        # 检查pks列表中的元素是否都是int或str类型
        for pk in pks:
            if not isinstance(pk, (int, str)):
                raise TypeError(f"wrong type of argument pks, expect list, int or str, got '{type(pk).__name__}'")

        # 如果提供了ids参数
        if ids is not None:
            # 如果ids是单个int或str类型，将其添加到pks列表
            if isinstance(ids, (int, str)):
                pks.append(ids)
            # 如果ids是列表，遍历并检查每个元素是否都是int或str类型
            elif isinstance(ids, list):
                for id in ids:
                    if not isinstance(id, (int, str)):
                        raise TypeError(
                            f"wrong type of argument ids, expect list, int or str, got '{type(id).__name__}'")
                pks.extend(ids)
            # 如果ids的类型不是预期的，抛出TypeError
            else:
                raise TypeError(f"wrong type of argument ids, expect list, int or str, got '{type(ids).__name__}'")

        # 初始化查询表达式
        expr = ""
        # 获取连接
        conn = self._get_connection()
        # 如果有pks，构造查询表达式
        if pks:
            try:
                # 获取集合的schema以用于构造查询表达式
                schema_dict = conn.describe_collection(collection_name, timeout=timeout, **kwargs)
            except Exception as ex:
                logging.error("Failed to describe collection: %s", collection_name)
                raise ex from ex

            expr = self._pack_pks_expr(schema_dict, pks)

        # 如果提供了filter参数
        if filter:
            # 如果已经存在查询表达式，则抛出异常，因为不能同时使用filter和pks/ids
            if expr:
                raise ParamError(message=ExceptionsMessage.AmbiguousDeleteFilterParam)
            # 检查filter参数是否为字符串类型
            if not isinstance(filter, str):
                raise DataTypeNotMatchException(message=ExceptionsMessage.ExprType % type(filter))

            expr = filter

        # 初始化删除的主键列表
        ret_pks = []
        try:
            # 执行删除操作
            res = conn.delete(
                collection_name,
                expr,
                partition_name,
                timeout=timeout,
                param_name="filter or ids",
                **kwargs,
            )
            # 如果有删除的主键，将其添加到结果列表
            if res.primary_keys:
                ret_pks.extend(res.primary_keys)
        except Exception as ex:
            logging.error("Failed to delete primary keys in collection: %s", collection_name)
            raise ex from ex

        # 如果有删除的主键，返回主键列表；否则，返回删除计数
        if ret_pks:
            return ret_pks

        return {"delete_count": res.delete_count}

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

    def create_collection_with_mapping(self, collection_name, mapping):
        dynamic_templates = mapping.get("mappings", {}).get("dynamic_templates", [])
        fields = []

        # 解析动态模板中的字段信息
        for template in dynamic_templates:
            for key, value in template.items():
                match_pattern = value.get("match", "")
                mapping_type = value.get("mapping", {}).get("type", "")
                dims = value.get("mapping", {}).get("dims", 128)

                if match_pattern == "vector":
                    fields.append(FieldSchema(name=match_pattern, dtype=DataType.FLOAT_VECTOR, dim=dims))
                elif mapping_type == "VarChar" and match_pattern != "pk":
                    max_length = value.get("mapping", {}).get("max_length", 256)  # 默认设置 max_length 为 256
                    fields.append(FieldSchema(name=match_pattern, dtype=DataType.VARCHAR, max_length=max_length, is_nullable=True))
                elif match_pattern == "pk":
                    max_length = value.get("mapping", {}).get("max_length", 256)  # 默认设置 max_length 为 256
                    fields.append(FieldSchema(name=match_pattern, dtype=DataType.VARCHAR, max_length=max_length,
                                              is_primary=True))
                elif mapping_type == "FLOAT":
                    fields.append(FieldSchema(name=match_pattern, dtype=DataType.FLOAT, is_nullable=True))
                elif mapping_type == "JSON":
                    fields.append(FieldSchema(name=match_pattern, dtype=DataType.JSON, is_nullable=True))
                elif mapping_type == "Array":
                    fields.append(FieldSchema(name=match_pattern, dtype=DataType.ARRAY, element_type=DataType.VARCHAR,
                                              max_length=256, max_capacity=4096, is_nullable=True))
        schema = CollectionSchema(fields=fields, description="Created from mapping file", enable_dynamic_field=True)
        self.create_collection(collection_name, schema=schema)
        # 处理索引相关的逻辑
        for template in dynamic_templates:
            for key, value in template.items():
                if value.get("match", "") == "vector":
                    index_params = {
                        "field_name": "vector",  # 确保 field_name 正确
                        "index_type": "IVF_FLAT",
                        "params": {"nlist": 1024},
                        "index_name": "vector",
                        "metric_type": value.get("mapping", {}).get("similarity", "COSINE")
                    }
                    self.create_index(collection_name, index_params)

        # 加载集合
        self.load_collection(collection_name)

    def close(self):
        connections.disconnect(self._using)

    def _get_connection(self):
        return connections._fetch_handler(self._using)

    def _create_connection(
            self,
            uri: str,
            user: str = "",
            password: str = "",
            db_name: str = "",
            token: str = "",
            **kwargs,
    ) -> str:
        using = uuid4().hex
        try:
            connections.connect(using, user, password, db_name, token, uri=uri, **kwargs)
        except Exception as ex:
            logging.error("Failed to create new connection using: %s", using)
            raise ex from ex
        else:
            logging.debug("Created new connection using: %s", using)
            return using

    def _extract_primary_field(self, schema_dict: dict) -> dict:
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
MILVUS_CONNECTION = MilvusConnection()
