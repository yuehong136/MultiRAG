# coding=utf-8
"""
@project: multirag
@Author：龙
@file： milvus_conn.py
@date：2024/7/31 10:12
@desc:
"""
from typing import Dict, List, Optional, Union
from uuid import uuid4

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
from core.settings import milvus_logger

milvus_logger.info("Milvus version: " + str(__version__))


class MilvusConnection:
    def __init__(
            self,
            uri: str = settings.MILVUS.get("hosts", ""),
            user: str = settings.MILVUS.get("username", ""),
            password: str = settings.MILVUS.get("password", ""),
            db_name: str = "",
            token: str = "",
            timeout: Optional[float] = None,
            **kwargs,
    ) -> None:
        self._using = self._create_connection(
            uri, user, password, db_name, token, timeout=timeout, **kwargs
        )
        self.is_self_hosted = bool(utility.get_server_type(using=self._using) == "milvus")

    def create_collection(
            self,
            collection_name: str,
            dimension: Optional[int] = None,
            primary_field_name: str = "id",
            id_type: str = "int",
            vector_field_name: str = "vector",
            metric_type: str = "COSINE",
            auto_id: bool = False,
            timeout: Optional[float] = None,
            schema: Optional[CollectionSchema] = None,
            index_params: Optional[IndexParams] = None,
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
            id_type: Union[DataType, str] = DataType.INT64,
            vector_field_name: str = "vector",
            metric_type: str = "COSINE",
            auto_id: bool = False,
            timeout: Optional[float] = None,
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
            milvus_logger.debug("Successfully created collection: %s", collection_name)
        except Exception as ex:
            milvus_logger.error("Failed to create collection: %s", collection_name)
            raise ex from ex

        index_params = IndexParams()
        index_params.add_index(vector_field_name, "", "", metric_type=metric_type)
        self.create_index(collection_name, index_params, timeout=timeout)
        self.load_collection(collection_name, timeout=timeout)

    def create_index(
            self,
            collection_name: str,
            index_params: Union[IndexParams, Dict],
            timeout: Optional[float] = None,
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
            self, collection_name: str, index_param: Dict, timeout: Optional[float] = None, **kwargs
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
            milvus_logger.debug("Successfully created an index on collection: %s", collection_name)
        except Exception as ex:
            milvus_logger.error("Failed to create an index on collection: %s", collection_name)
            raise ex from ex

    # def create_index(
    #         self,
    #         collection_name: str,
    #         index_params: IndexParams,
    #         timeout: Optional[float] = None,
    #         **kwargs,
    # ):
    #     for index_param in index_params:
    #         self._create_index(collection_name, index_param, timeout=timeout, **kwargs)
    #
    # def _create_index(
    #         self, collection_name: str, index_param: Dict, timeout: Optional[float] = None, **kwargs
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
    #         milvus_logger.debug("Successfully created an index on collection: %s", collection_name)
    #     except Exception as ex:
    #         milvus_logger.error("Failed to create an index on collection: %s", collection_name)
    #         raise ex from ex

    def insert(
            self,
            collection_name: str,
            data: Union[Dict, List[Dict]],
            timeout: Optional[float] = None,
            partition_name: Optional[str] = "",
            **kwargs,
    ) -> Dict:
        if isinstance(data, Dict):
            data = [data]

        msg = "wrong type of argument 'data',"
        msg += f"expected 'Dict' or list of 'Dict', got '{type(data).__name__}'"

        if not isinstance(data, List):
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

    def health(self) -> dict:
        try:
            # 尝试获取 Milvus 服务器的版本来检查是否连接正常
            version = utility.get_server_version(using=self._using)
            return {
                "status": "healthy",
                "version": version
            }
        except MilvusException as e:
            # 捕获异常并返回不健康状态
            return {
                "status": "unhealthy",
                "error": str(e)
            }

    def upsert(
            self,
            collection_name: str,
            data: Union[Dict, List[Dict]],
            timeout: Optional[float] = None,
            partition_name: Optional[str] = "",
            **kwargs,
    ) -> Dict:
        if isinstance(data, Dict):
            data = [data]

        msg = "wrong type of argument 'data',"
        msg += f"expected 'Dict' or list of 'Dict', got '{type(data).__name__}'"

        if not isinstance(data, List):
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
                milvus_logger.info("Successfully upserted records to Milvus")
            except Exception as e:
                milvus_logger.error("Failed to upsert records to Milvus: " + str(e))
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
            data: Union[List[list], list],
            filter: str = "",
            limit: int = 10,
            output_fields: Optional[List[str]] = None,
            search_params: Optional[dict] = None,
            timeout: Optional[float] = None,
            partition_names: Optional[List[str]] = None,
            anns_field: Optional[str] = None,
            **kwargs,
    ) -> List[List[dict]]:
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
            milvus_logger.error("Failed to search collection: %s", collection_name)
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
            output_fields: Optional[List[str]] = None,
            timeout: Optional[float] = None,
            ids: Optional[Union[List, str, int]] = None,
            partition_names: Optional[List[str]] = None,
            **kwargs,
    ) -> List[dict]:
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
            milvus_logger.error("Failed to describe collection: %s", collection_name)
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
            milvus_logger.error("Failed to query collection: %s", collection_name)
            raise ex from ex

        return res

    def get(
            self,
            collection_name: str,
            ids: Union[list, str, int],
            output_fields: Optional[List[str]] = None,
            timeout: Optional[float] = None,
            partition_names: Optional[List[str]] = None,
            **kwargs,
    ) -> List[dict]:
        if not isinstance(ids, list):
            ids = [ids]

        if len(ids) == 0:
            return []

        conn = self._get_connection()
        try:
            schema_dict = conn.describe_collection(collection_name, timeout=timeout, **kwargs)
        except Exception as ex:
            milvus_logger.error("Failed to describe collection: %s", collection_name)
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
            milvus_logger.error("Failed to get collection: %s", collection_name)
            raise ex from ex

        return res

    def delete(
            self,
            collection_name: str,
            ids: Optional[Union[list, str, int]] = None,
            timeout: Optional[float] = None,
            filter: Optional[str] = "",
            partition_name: Optional[str] = "",
            **kwargs,
    ) -> Dict:
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
                milvus_logger.error("Failed to describe collection: %s", collection_name)
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
            milvus_logger.error("Failed to delete primary keys in collection: %s", collection_name)
            raise ex from ex

        # 如果有删除的主键，返回主键列表；否则，返回删除计数
        if ret_pks:
            return ret_pks

        return {"delete_count": res.delete_count}

    def get_collection_stats(self, collection_name: str, timeout: Optional[float] = None) -> Dict:
        conn = self._get_connection()
        stats = conn.get_collection_stats(collection_name, timeout=timeout)
        result = {stat.key: stat.value for stat in stats}
        if "row_count" in result:
            result["row_count"] = int(result["row_count"])
        return result

    def describe_collection(self, collection_name: str, timeout: Optional[float] = None, **kwargs):
        conn = self._get_connection()
        return conn.describe_collection(collection_name, timeout=timeout, **kwargs)

    def has_collection(self, collection_name: str, timeout: Optional[float] = None, **kwargs):
        conn = self._get_connection()
        return conn.has_collection(collection_name, timeout=timeout, **kwargs)

    def list_collections(self, **kwargs):
        conn = self._get_connection()
        return conn.list_collections(**kwargs)

    def drop_collection(self, collection_name: str, timeout: Optional[float] = None, **kwargs):
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
            index_params: IndexParams,
            timeout: Optional[float] = None,
            **kwargs,
    ):
        schema.verify()

        conn = self._get_connection()
        if "consistency_level" not in kwargs:
            kwargs["consistency_level"] = DEFAULT_CONSISTENCY_LEVEL
        try:
            conn.create_collection(collection_name, schema, timeout=timeout, **kwargs)
            milvus_logger.debug("Successfully created collection: %s", collection_name)
        except Exception as ex:
            milvus_logger.error("Failed to create collection: %s", collection_name)
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
                    fields.append(FieldSchema(name=match_pattern, dtype=DataType.VARCHAR, max_length=max_length))
                elif match_pattern == "pk":
                    max_length = value.get("mapping", {}).get("max_length", 256)  # 默认设置 max_length 为 256
                    fields.append(FieldSchema(name=match_pattern, dtype=DataType.VARCHAR, max_length=max_length,
                                              is_primary=True))
                elif mapping_type == "FLOAT":
                    fields.append(FieldSchema(name=match_pattern, dtype=DataType.FLOAT))
                elif mapping_type == "JSON":
                    fields.append(FieldSchema(name=match_pattern, dtype=DataType.JSON))
                elif mapping_type == "Array":
                    fields.append(FieldSchema(name=match_pattern, dtype=DataType.ARRAY, element_type=DataType.VARCHAR,
                                              max_length=256, max_capacity=4096))
        # todo 测试一下能否在下面使用动态字段
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
            milvus_logger.error("Failed to create new connection using: %s", using)
            raise ex from ex
        else:
            milvus_logger.debug("Created new connection using: %s", using)
            return using

    def _extract_primary_field(self, schema_dict: Dict) -> dict:
        fields = schema_dict.get("fields", [])
        if not fields:
            return {}

        for field_dict in fields:
            if field_dict.get("is_primary", None) is not None:
                return field_dict

        return {}

    def _get_vector_field_name(self, schema_dict: Dict):
        fields = schema_dict.get("fields", [])
        if not fields:
            return {}

        for field_dict in fields:
            if field_dict.get("type", None) == DataType.FLOAT_VECTOR:
                return field_dict.get("name", "")
        return ""

    def _pack_pks_expr(self, schema_dict: Dict, pks: List) -> str:
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

    def load_collection(self, collection_name: str, timeout: Optional[float] = None, **kwargs):
        conn = self._get_connection()
        try:
            conn.load_collection(collection_name, timeout=timeout, **kwargs)
        except MilvusException as ex:
            milvus_logger.error("Failed to load collection: %s", collection_name)
            raise ex from ex

    def release_collection(self, collection_name: str, timeout: Optional[float] = None, **kwargs):
        conn = self._get_connection()
        try:
            conn.release_collection(collection_name, timeout=timeout, **kwargs)
        except MilvusException as ex:
            milvus_logger.error("Failed to load collection: %s", collection_name)
            raise ex from ex

    def get_load_state(
            self,
            collection_name: str,
            partition_name: Optional[str] = "",
            timeout: Optional[float] = None,
            **kwargs,
    ) -> Dict:
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

    def refresh_load(self, collection_name: str, timeout: Optional[float] = None, **kwargs):
        kwargs.pop("_refresh", None)
        conn = self._get_connection()
        conn.load_collection(collection_name, timeout=timeout, _refresh=True, **kwargs)

    def list_indexes(self, collection_name: str, field_name: Optional[str] = "", **kwargs):
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
            self, collection_name: str, index_name: str, timeout: Optional[float] = None, **kwargs
    ):
        conn = self._get_connection()
        conn.drop_index(collection_name, "", index_name, timeout=timeout, **kwargs)

    def describe_index(
            self, collection_name: str, index_name: str, timeout: Optional[float] = None, **kwargs
    ) -> Dict:
        conn = self._get_connection()
        return conn.describe_index(collection_name, index_name, timeout=timeout, **kwargs)

    def create_partition(
            self, collection_name: str, partition_name: str, timeout: Optional[float] = None, **kwargs
    ):
        conn = self._get_connection()
        conn.create_partition(collection_name, partition_name, timeout=timeout, **kwargs)

    def drop_partition(
            self, collection_name: str, partition_name: str, timeout: Optional[float] = None, **kwargs
    ):
        conn = self._get_connection()
        conn.drop_partition(collection_name, partition_name, timeout=timeout, **kwargs)

    def has_partition(
            self, collection_name: str, partition_name: str, timeout: Optional[float] = None, **kwargs
    ) -> bool:
        conn = self._get_connection()
        return conn.has_partition(collection_name, partition_name, timeout=timeout, **kwargs)

    def list_partitions(
            self, collection_name: str, timeout: Optional[float] = None, **kwargs
    ) -> List[str]:
        conn = self._get_connection()
        return conn.list_partitions(collection_name, timeout=timeout, **kwargs)

    def load_partitions(
            self,
            collection_name: str,
            partition_names: Union[str, List[str]],
            timeout: Optional[float] = None,
            **kwargs,
    ):
        if isinstance(partition_names, str):
            partition_names = [partition_names]

        conn = self._get_connection()
        conn.load_partitions(collection_name, partition_names, timeout=timeout, **kwargs)

    def release_partitions(
            self,
            collection_name: str,
            partition_names: Union[str, List[str]],
            timeout: Optional[float] = None,
            **kwargs,
    ):
        if isinstance(partition_names, str):
            partition_names = [partition_names]
        conn = self._get_connection()
        conn.release_partitions(collection_name, partition_names, timeout=timeout, **kwargs)

    def get_partition_stats(
            self, collection_name: str, partition_name: str, timeout: Optional[float] = None, **kwargs
    ) -> Dict:
        conn = self._get_connection()
        if not isinstance(partition_name, str):
            msg = f"wrong type of argument 'partition_name', str expected, got '{type(partition_name).__name__}'"
            raise TypeError(msg)
        ret = conn.get_partition_stats(collection_name, partition_name, timeout=timeout, **kwargs)
        result = {stat.key: stat.value for stat in ret}
        if "row_count" in result:
            result["row_count"] = int(result["row_count"])
        return result

    def create_user(self, user_name: str, password: str, timeout: Optional[float] = None, **kwargs):
        conn = self._get_connection()
        return conn.create_user(user_name, password, timeout=timeout, **kwargs)

    def drop_user(self, user_name: str, timeout: Optional[float] = None, **kwargs):
        conn = self._get_connection()
        return conn.delete_user(user_name, timeout=timeout, **kwargs)

    def update_password(
            self,
            user_name: str,
            old_password: str,
            new_password: str,
            reset_connection: Optional[bool] = False,
            timeout: Optional[float] = None,
            **kwargs,
    ):
        conn = self._get_connection()
        conn.update_password(user_name, old_password, new_password, timeout=timeout, **kwargs)
        if reset_connection:
            conn._setup_authorization_interceptor(user_name, new_password, None)
            conn._setup_grpc_channel()

    def list_users(self, timeout: Optional[float] = None, **kwargs):
        conn = self._get_connection()
        return conn.list_usernames(timeout=timeout, **kwargs)

    def describe_user(self, user_name: str, timeout: Optional[float] = None, **kwargs):
        conn = self._get_connection()
        try:
            res = conn.select_one_user(user_name, True, timeout=timeout, **kwargs)
        except Exception as ex:
            raise ex from ex
        if res.groups:
            item = res.groups[0]
            return {"user_name": user_name, "roles": item.roles}
        return {}

    def grant_role(self, user_name: str, role_name: str, timeout: Optional[float] = None, **kwargs):
        conn = self._get_connection()
        conn.add_user_to_role(user_name, role_name, timeout=timeout, **kwargs)

    def revoke_role(
            self, user_name: str, role_name: str, timeout: Optional[float] = None, **kwargs
    ):
        conn = self._get_connection()
        conn.remove_user_from_role(user_name, role_name, timeout=timeout, **kwargs)

    def create_role(self, role_name: str, timeout: Optional[float] = None, **kwargs):
        conn = self._get_connection()
        conn.create_role(role_name, timeout=timeout, **kwargs)

    def drop_role(self, role_name: str, timeout: Optional[float] = None, **kwargs):
        conn = self._get_connection()
        conn.drop_role(role_name, timeout=timeout, **kwargs)

    def describe_role(
            self, role_name: str, timeout: Optional[float] = None, **kwargs
    ) -> List[Dict]:
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

    def list_roles(self, timeout: Optional[float] = None, **kwargs):
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
            db_name: Optional[str] = "",
            timeout: Optional[float] = None,
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
            db_name: Optional[str] = "",
            timeout: Optional[float] = None,
            **kwargs,
    ):
        conn = self._get_connection()
        conn.revoke_privilege(
            role_name, object_type, object_name, privilege, db_name, timeout=timeout, **kwargs
        )

    def create_alias(
            self, collection_name: str, alias: str, timeout: Optional[float] = None, **kwargs
    ):
        conn = self._get_connection()
        conn.create_alias(collection_name, alias, timeout=timeout, **kwargs)

    def drop_alias(self, alias: str, timeout: Optional[float] = None, **kwargs):
        conn = self._get_connection()
        conn.drop_alias(alias, timeout=timeout, **kwargs)

    def alter_alias(
            self, collection_name: str, alias: str, timeout: Optional[float] = None, **kwargs
    ):
        conn = self._get_connection()
        conn.alter_alias(collection_name, alias, timeout=timeout, **kwargs)

    def describe_alias(self, alias: str, timeout: Optional[float] = None, **kwargs) -> Dict:
        conn = self._get_connection()
        return conn.describe_alias(alias, timeout=timeout, **kwargs)

    def list_aliases(
            self, collection_name: str = "", timeout: Optional[float] = None, **kwargs
    ) -> List[str]:
        conn = self._get_connection()
        return conn.list_aliases(collection_name, timeout=timeout, **kwargs)

    def using_database(self, db_name: str, **kwargs):
        conn = self._get_connection()
        conn.reset_db_name(db_name)


# Create a singleton instance of MilvusConnection
MILVUS_CONNECTION = MilvusConnection()
