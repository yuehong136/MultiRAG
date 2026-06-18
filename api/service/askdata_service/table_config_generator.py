import asyncio
import re
import uuid
from typing import Any, List, Dict, Tuple, Optional

from api.service.askdata_service.sql_components_parser import SQLComponentsParser
from api.service.askdata_service.util.are_expressions_equal_ignore_quotes import are_expressions_equal_ignore_quotes
from api.service.askdata_service.util.find_aggregate_columns import find_aggregate_columns
from api.service.askdata_service.util.parse_sql_extract import parse_sql_extract
from api.service.nl2sql_service.semantic_api_client import SemanticApiClient
from api.service.askdata_service.util.askdata_logger import get_askdata_logger

logger = get_askdata_logger()


class TableConfigGenerator:
    """
    负责从SQL查询和语义模型生成表格配置。
    """

    def __init__(self, semantic_api_client: SemanticApiClient):
        self.semantic_api_client = semantic_api_client

    @staticmethod
    def _is_sql_expression_value(value: Any) -> bool:
        """判断 WHERE/HAVING 右侧值是否为 SQL 表达式（非简单字面量）。
        命中特征即视为表达式：函数调用/括号、PostgreSQL 类型转换、日期时间常量/关键字。
        这类值不能被当作字面量参数绑定到 `?`，否则会让 `col >= func()` 变成 `col >= 'func()'`
        的字符串比较（当前 bug 就是这样 count 变 0 的）。
        """
        if not isinstance(value, str):
            return False
        v = value.strip()
        if not v:
            return False
        if "(" in v or ")" in v or "::" in v:
            return True
        v_upper = v.upper()
        keywords = ("CURRENT_DATE", "CURRENT_TIMESTAMP", "CURRENT_TIME",
                    "NOW", "INTERVAL", "CAST", "EXTRACT")
        return any(kw in v_upper for kw in keywords)

    async def generate(self, used_table_detail_dict: Dict[str, Dict], model_list: List[Dict],
                       sql_components: Dict[str, Any], recommended_chart: str,
                       cached_model_relations: list | None = None,
                       cached_dimension_values: dict | None = None):
        """
        生成表配置信息，将SQL解析结果映射到语义层。

        Args:
            used_table_detail_dict: 使用的表的详情字典
            model_list: 所有相关的模型信息列表
            sql_components: SQL句子成分
            recommended_chart: 推荐的图表类型
            cached_model_relations: Phase 1 预拉的模型关系，缓存优先
            cached_dimension_values: Phase 1 预拉的维度值，缓存优先

        Returns:
            包含列、过滤器和排序信息的配置字典
        """
        # 1. 解析SQL
        parts = SQLComponentsParser(sql_components).parse_all()
        main_table = parts["main_table"]

        # 查询与主表有关的模型
        # 首先要根据main_table的名字找到模型详情中的模型ID,然后调用查询模型关系的方法,把相关模型都查出来
        # 因为后续返回给前端的可以调整的内容不仅仅是当前涉及到的表，完整的应该是与主表相关联的表都可以通过维度等进行调整。
        main_table_model_id = None
        if main_table and main_table in used_table_detail_dict:
            main_table_model_id = used_table_detail_dict[main_table].get("modelId")
        
        # 如果找到了主表的模型ID,则查询相关的模型关系
        related_model_relationships = []
        if main_table_model_id:
            try:
                if cached_model_relations is not None:
                    # 缓存优先：从 Phase 1 缓存的全量关系中过滤出与主表相关的
                    related_model_relationships = [
                        r for r in cached_model_relations
                        if r.get('sourceModelId') == main_table_model_id
                        or r.get('targetModelId') == main_table_model_id
                    ]
                    logger.info(f"从缓存中过滤到 {len(related_model_relationships)} 条与主表相关的模型关系")
                else:
                    related_model_relationships = await self.semantic_api_client.get_model_relationships_async(
                        model_ids=main_table_model_id
                    )
                    logger.info(f"从 API 获取到 {len(related_model_relationships)} 条与主表相关的模型关系")
            except Exception as e:
                logger.warning(f"获取主表模型关系失败: {str(e)}")
        
        # 从关系中提取所有涉及的模型ID
        related_model_ids = set()
        for relation in related_model_relationships:
            source_model_id = relation.get('sourceModelId')
            target_model_id = relation.get('targetModelId')
            if source_model_id:
                related_model_ids.add(source_model_id)
            if target_model_id:
                related_model_ids.add(target_model_id)
        
        # 获取model_list中已存在的模型ID
        existing_model_ids = set(model.get('modelId') for model in model_list if model.get('modelId'))
        
        # 找出需要新增的模型ID
        missing_model_ids = related_model_ids - existing_model_ids
        
        # 如果有缺失的模型,则获取它们的详情并添加到model_list
        if missing_model_ids:
            logger.info(f"发现 {len(missing_model_ids)} 个关联模型不在model_list中,准备获取详情")
            try:
                missing_models_details = await self.semantic_api_client.get_model_detail_async(
                    model_ids=list(missing_model_ids)
                )

                # 并行获取缺少 dimsAndMetrics 的模型（gather 替代串行 await）
                models_needing_dims = [m for m in missing_models_details if 'dimsAndMetrics' not in m]
                if models_needing_dims:
                    dims_tasks = [
                        self.semantic_api_client.get_model_inds_and_dims_by_model_id_async(
                            model_id=m["modelId"]
                        )
                        for m in models_needing_dims
                    ]
                    dims_results = await asyncio.gather(*dims_tasks)
                    for model_detail, dims_metrics in zip(models_needing_dims, dims_results):
                        model_detail['dimsAndMetrics'] = dims_metrics

                for model_detail in missing_models_details:
                    model_list.append(model_detail)
                    logger.info(f"成功添加关联模型: {model_detail.get('modelName')} (ID: {model_detail.get('modelId')})")

                logger.info(f"成功获取并添加 {len(missing_models_details)} 个关联模型的完整详情到model_list")
            except Exception as e:
                logger.warning(f"获取关联模型详情失败: {str(e)}")

        # 重新构建 used_table_detail_dict，确保包含所有模型（包括新增的关联模型）
        for model in model_list:
            table_name = model.get("tableName")
            if table_name:
                used_table_detail_dict[table_name] = model
        logger.info(f"更新后的 used_table_detail_dict 包含 {len(used_table_detail_dict)} 个表/模型")

        # 收集所有已存在的别名，避免冲突
        existing_aliases = set()
        max_alias_num = 0
        has_number_pattern = False  # 是否识别到数字规律（如 t1, t2）

        for alias_and_table in parts["from_tables"]:
            alias = alias_and_table["alias"]
            existing_aliases.add(alias)

            # 尝试识别 t1, t2, t3 这类数字规律
            match = re.match(r'^([a-zA-Z]+)(\d+)$', alias)
            if match:
                prefix = match.group(1)
                num = int(match.group(2))
                # 只记录 t1, t2 这种单字母+数字的模式
                if prefix == 't':
                    has_number_pattern = True
                    max_alias_num = max(max_alias_num, num)

        model_table_alias_mapping_list = []
        for model in model_list:
            alias = ""
            # 首先尝试从已有的from_tables中找到别名
            for alias_and_table in parts["from_tables"]:
                if alias_and_table["table"] == model["tableName"]:
                    alias = alias_and_table["alias"]
                    break

            # 如果没有找到别名（说明是新增的关联模型），生成一个新别名
            if not alias:
                if has_number_pattern:
                    # 有数字规律，继续 t+数字
                    max_alias_num += 1
                    alias = f"t{max_alias_num}"
                else:
                    # 没有规律，使用表名_auto作为别名
                    table_name = model["tableName"]
                    alias = f"{table_name}_auto"
                    # 如果冲突，添加序号
                    counter = 1
                    while alias in existing_aliases:
                        alias = f"{table_name}_auto{counter}"
                        counter += 1

                existing_aliases.add(alias)
                logger.info(f"为新增模型 {model['modelName']} (表: {model['tableName']}) 生成别名: {alias}")

            model_table_alias_mapping_list.append(
                {"modelId": model["modelId"], "table": model["tableName"], "alias": alias,
                 "modelName": model["modelName"]})

        if recommended_chart == "明细表":
            # 4. 一次性构建所有语义字段信息
            semantic_fields_info = await self._build_semantic_fields_info(model_list, cached_dimension_values)

            # 5. 处理SELECT列
            selected_columns = self._process_select_columns(parts, used_table_detail_dict)

            # 6. 处理WHERE条件
            filter_conditions = self._process_where_conditions(parts, used_table_detail_dict)

            # 7. 处理ORDER BY字段
            order_by_fields = self._process_order_by_fields(parts, used_table_detail_dict)

            limit = self._process_limit(parts)

            return model_table_alias_mapping_list, {
                "chart_type": "table-row",
                "columns": {
                    "selected_columns": selected_columns,
                    "available_fields": semantic_fields_info["available_fields"]
                },
                "filters": {
                    "filter_conditions": filter_conditions,
                    "available_fields": semantic_fields_info["filterable_fields"]
                },
                "order_by": {
                    "order_by_fields": order_by_fields,
                    "available_fields": semantic_fields_info["sortable_fields"]
                },
                "limit": limit,
                "all_semantic_fields": semantic_fields_info["all_fields"]
            }
        elif recommended_chart == "聚合表":
            # 4. 一次性构建所有语义字段信息
            semantic_fields_info = await self._build_semantic_fields_info_for_aggr(model_list, cached_dimension_values)

            # 5. 处理维度，维度是group by中的内容
            selected_dimensions = self._build_selected_dimensions(parts, used_table_detail_dict)

            # 6. 处理指标，使用方法提取指标字段
            selected_metrics = self._build_selected_metrics(parts, used_table_detail_dict)

            # 7. 处理where
            where_conditions = self._process_where_conditions(parts, used_table_detail_dict)

            # 8. 处理having
            having_conditions = self._process_having_conditions(parts, used_table_detail_dict)

            order_by_fields = self._process_order_by_fields(parts, used_table_detail_dict)

            limit = self._process_limit(parts)

            return model_table_alias_mapping_list, {
                "chart_type": "table-aggr",
                "dimensions": {
                    "selected_dimensions": selected_dimensions,
                    "available_dimensions": semantic_fields_info["available_dimensions"]
                },
                "metrics": {
                    "selected_metrics": selected_metrics,
                    "available_metrics": semantic_fields_info["available_metrics"]
                },
                "where_conditions": {
                    "where_conditions": where_conditions,
                    "available_fields": semantic_fields_info["whereable_fields"]
                },
                "having_conditions": {
                    "having_conditions": having_conditions,
                    "available_fields": semantic_fields_info["havingable_fields"]
                },
                "order_by": {
                    "order_by_fields": order_by_fields,
                    "available_fields": semantic_fields_info["sortable_fields"]
                },
                "limit": limit,
                "all_semantic_fields": semantic_fields_info["all_fields"]
            }

    def _process_having_conditions(self, parts: Dict[str, Any], used_table_detail_dict: Dict[str, Any]) -> List[Dict]:
        """处理having条件"""
        table_alias_mapping = parts["table_alias_mapping"]
        having_conditions = parts['having_conditions']
        if having_conditions.get('has_or'):
            raw = having_conditions['raw_condition']
            return [{"is_semantic_field": False, "is_complex_condition": True, "raw_condition": raw, "field": raw,
                     "operator": "", "value": "", "from_model": None, "id": str(uuid.uuid4()),
                     "wid": str(uuid.uuid4())}]

        filter_columns = []
        for cond in having_conditions.get('parsed_conditions', []):
            is_matched_semantic_field = False
            table_alias, column_name = self._get_table_alias_and_field_by_split_column(cond['field'])
            operator = cond['operator']
            value = cond['value']
            table_name, table_detail = self._resolve_table(table_alias, column_name, table_alias_mapping, used_table_detail_dict)
            if table_detail is None:
                filter_columns.append(
                    {"is_semantic_field": False, "sql_column": cond["field"], "operator": operator, "value": value,
                     "id": str(uuid.uuid4()), "wid": str(uuid.uuid4())})
                continue
            for metric in table_detail['dimsAndMetrics']['metrics']:
                if metric['expression'].lower() == table_name+'.'+column_name.lower():
                    filter_columns.append(
                        {"is_semantic_field": True, "semantic_type": "metric", "id": metric["metricId"],
                         "metric_name": metric["metricName"], "operator": operator, "value": value,
                         "wid": str(uuid.uuid4()), "original_sql_component": cond}
                    )
                    is_matched_semantic_field = True
                    break
            if is_matched_semantic_field:
                continue
            filter_columns.append(
                {"is_semantic_field": False, "sql_column": cond["field"], "operator": operator, "value": value,
                 "id": str(uuid.uuid4()), "wid": str(uuid.uuid4())})

        return filter_columns

    def _build_selected_dimensions(self, parts: Dict[str, Any], used_table_detail_dict: Dict[str, Any]) -> List[Dict]:
        """构建group by维度字典"""
        try:
            group_by_dimensions = []
            table_alias_mapping = parts["table_alias_mapping"]
            for group_by in parts["group_by"]:
                is_matched_semantic_field = False
                split_col = self._get_table_alias_and_field_by_split_column(group_by)
                if len(split_col) == 2:
                    table_alias, column_name = self._get_table_alias_and_field_by_split_column(group_by)
                    table_name, table_detail = self._resolve_table(table_alias, column_name, table_alias_mapping, used_table_detail_dict)
                    if table_detail is None:
                        group_by_dimensions.append({
                            "is_semantic_field": False,
                            "sql_column": group_by,
                            "id": str(uuid.uuid4()),
                            "wid": str(uuid.uuid4()),
                            "nanoId": str(uuid.uuid4())
                        })
                        continue
                    is_timeseries = False
                    time_unit = None
                    time_source = None
                    if column_name.lower().startswith("extract("):
                        parsed_result = parse_sql_extract(column_name)
                        if parsed_result['unit'] is not None and parsed_result['source'] is not None:
                            time_unit = parsed_result['unit']
                            time_source = parsed_result['source']
                        if time_unit and time_source:
                            is_timeseries = True
                    for dim in table_detail['dimsAndMetrics']['dimensions']:
                        if is_timeseries and dim['dimensionEnName'].lower() == time_source.lower():
                            group_by_dimensions.append(
                                {"is_semantic_field": True, "semantic_type": "dimension",
                                 "id": dim["dimensionId"],
                                 "dimension_name": dim["dimensionName"], "wid": str(uuid.uuid4()),
                                 "nanoId": str(uuid.uuid4()), "original_sql_component": group_by, "unit": time_unit}
                            )
                            is_matched_semantic_field = True
                            break
                        if dim['dimensionEnName'].lower() == column_name.lower():
                            group_by_dimensions.append(
                                {"is_semantic_field": True, "semantic_type": "dimension", "id": dim["dimensionId"],
                                 "dimension_name": dim["dimensionName"], "wid": str(uuid.uuid4()),
                                 "nanoId": str(uuid.uuid4()), "original_sql_component": group_by}
                            )
                            is_matched_semantic_field = True
                            break
                    if is_matched_semantic_field:
                        continue

                if not is_matched_semantic_field:
                    group_by_dimensions.append({
                        "is_semantic_field": False,
                        "sql_column": group_by,
                        "id": str(uuid.uuid4()),
                        "wid": str(uuid.uuid4()),
                        "nanoId": str(uuid.uuid4())
                    })
            return group_by_dimensions
        except Exception as e:
            logger.exception(f"parts: {parts}")
            logger.exception(f"used_table_detail_dict: {used_table_detail_dict}")
            return []

    def _build_selected_metrics(self, parts: Dict[str, Any], used_table_detail_dict: Dict[str, Any]) -> List[Dict]:
        selected_metrics = []
        selected_columns = parts["select_columns"]
        table_alias_mapping = parts["table_alias_mapping"]
        metrics = find_aggregate_columns(selected_columns)
        for metric in metrics:
            is_matched_semantic_field = False
            table_alias, column_name = self._get_table_alias_and_field_by_split_column(metric)
            table_name, table_detail = self._resolve_table(table_alias, column_name, table_alias_mapping, used_table_detail_dict)
            if table_detail is None:
                # 无法确定来源表（多表+裸列，或模型表详情缺失），按原始列名作为非语义字段处理
                selected_metrics.append(
                    {"is_semantic_field": False, "sql_column": metric, "id": str(uuid.uuid4()),
                     "wid": str(uuid.uuid4()), "nanoId": str(uuid.uuid4()), "original_sql_component": metric})
                continue
            else:
                for model_metric in table_detail['dimsAndMetrics']['metrics']:
                    if (model_metric['expression'].lower() == column_name.lower()) or are_expressions_equal_ignore_quotes(model_metric['expression'], column_name):
                        selected_metrics.append(
                            {"is_semantic_field": True, "semantic_type": "metric", "id": model_metric["metricId"],
                             "metric_name": model_metric["metricName"], "wid": str(uuid.uuid4()),
                             "nanoId": str(uuid.uuid4()), "original_sql_component": metric}
                        )
                        is_matched_semantic_field = True
                        break
                if is_matched_semantic_field:
                    continue
            if not is_matched_semantic_field:
                selected_metrics.append({
                    "is_semantic_field": False,
                    "sql_column": metric,
                    "id": str(uuid.uuid4()),
                    "wid": str(uuid.uuid4()),
                    "nanoId": str(uuid.uuid4())
                })
        return selected_metrics

    async def _build_semantic_fields_info(self, model_list: List[Dict],
                                          cached_dimension_values: dict | None = None) -> Dict[str, List]:
        """一次性构建所有语义字段信息，避免重复遍历"""
        available_fields, filterable_fields, sortable_fields, all_fields = [], [], [], []
        all_dimension_ids = []
        for model in model_list:
            dimensions = model["dimsAndMetrics"]["dimensions"]
            for dim in dimensions:
                # 如果是时间字段或者是高基数维度的字段，就不查询他们的维度值。因为时间类型的值是通过前端组件选择得到的，高基数维度的值是前端动态调用接口得到的
                if dim["dimtype"].lower() == "time" or dim["dimtype"].lower() == "hc":
                    continue
                all_dimension_ids.append(dim['dimensionId'])

        # 缓存优先获取维度值
        if cached_dimension_values is not None:
            dimensions_value_dict = cached_dimension_values
            logger.info(f"使用缓存的维度值 ({len(dimensions_value_dict)} 个)")
        elif all_dimension_ids:
            dimensions_value_dict = await self.semantic_api_client.get_dimension_values_async(
                dimension_ids=all_dimension_ids
            )
        else:
            dimensions_value_dict = {}

        for model in model_list:
            model_id = model["modelId"]
            model_name = model["modelName"]

            for metric in model["dimsAndMetrics"]["metrics"]:
                # 如果表达式中包含括号，则认为是聚合表达式，由于当前是为明细表服务，不可用于值展示、过滤、排序
                is_agg = "(" in metric["expression"].lower()
                available_fields.append(
                    {"is_allow_use": not is_agg, "semantic_type": "metric", "id": metric["metricId"],
                     "metric_name": metric["metricName"]})
                filterable_fields.append(
                    {"is_allow_use": not is_agg, "semantic_type": "metric", "id": metric["metricId"],
                     "from_model": model_name,
                     "from_model_id": model_id, "metric_name": metric["metricName"]})
                sortable_fields.append({"is_allow_use": not is_agg, "semantic_type": "metric", "id": metric["metricId"],
                                        "from_model": model_id, "metric_name": metric["metricName"]})
                all_fields.append({"semantic_type": "metric", "semantic_field": metric, "id": metric["metricId"],
                                   "from_model": model_name, "from_model_id": model_id})

            for dim in model["dimsAndMetrics"]["dimensions"]:
                dim_id = dim["dimensionId"]
                # 如果是时间类型或者高基数维度，则不不要将值传给前端，
                if dim["dimtype"].lower() == "time" or dim["dimtype"].lower() == "hc":
                    pass
                else:
                    dim['possibleValues'] = dimensions_value_dict.get(dim_id, [])
                available_fields.append({"is_allow_use": True, "semantic_type": "dimension", "id": dim_id,
                                         "dimension_name": dim["dimensionName"]})
                filterable_fields.append(
                    {"is_allow_use": True, "semantic_type": "dimension", "id": dim_id, "from_model": model_name,
                     "from_model_id": model_id,
                     "dimension_name": dim["dimensionName"]})
                sortable_fields.append(
                    {"is_allow_use": True, "semantic_type": "dimension", "id": dim_id, "from_model": model_id,
                     "dimension_name": dim["dimensionName"]})
                all_fields.append(
                    {"semantic_type": "dimension", "semantic_field": dim, "id": dim_id, "from_model": model_name,
                     "from_model_id": model_id})

        return {
            "available_fields": available_fields, "filterable_fields": filterable_fields,
            "sortable_fields": sortable_fields, "all_fields": all_fields
        }

    async def _build_semantic_fields_info_for_aggr(self, model_list: List[Dict],
                                                    cached_dimension_values: dict | None = None) -> Dict[str, List]:
        """一次性构建所有语义字段信息，避免重复遍历"""
        available_dimensions, available_metrics, sortable_fields, whereable_fields, havingable_fields, all_fields = [], [], [], [], [], []
        all_dimension_ids = []
        for model in model_list:
            dimensions = model["dimsAndMetrics"]["dimensions"]
            for dim in dimensions:
                # 如果是时间字段或者是高基数维度的字段，就不查询他们的维度值。因为时间类型的值是通过前端组件选择得到的，高基数维度的值是前端动态调用接口得到的
                if dim["dimtype"].lower() == "time" or dim["dimtype"].lower() == "hc":
                    continue
                all_dimension_ids.append(dim['dimensionId'])

        # 缓存优先获取维度值
        if cached_dimension_values is not None:
            dimensions_value_dict = cached_dimension_values
            logger.info(f"使用缓存的维度值 ({len(dimensions_value_dict)} 个)")
        elif all_dimension_ids:
            dimensions_value_dict = await self.semantic_api_client.get_dimension_values_async(
                dimension_ids=all_dimension_ids
            )
        else:
            dimensions_value_dict = {}

        for model in model_list:
            model_id = model["modelId"]
            model_name = model["modelName"]

            for metric in model["dimsAndMetrics"]["metrics"]:
                available_metrics.append(
                    {"is_allow_use": True, "semantic_type": "metric", "id": metric["metricId"],
                     "from_model": model_name,
                     "from_model_id": model_id, "metric_name": metric["metricName"], "wid": str(uuid.uuid4())})
                whereable_fields.append(
                    {"is_allow_use": True, "semantic_type": "metric", "id": metric["metricId"], "from_model": model_name,
                     "from_model_id": model_id,
                     "metric_name": metric["metricName"], "wid": str(uuid.uuid4())})
                havingable_fields.append(
                    {"is_allow_use": True, "semantic_type": "metric", "id": metric["metricId"],
                     "from_model": model_name,
                     "from_model_id": model_id, "metric_name": metric["metricName"], "wid": str(uuid.uuid4())})
                sortable_fields.append({"is_allow_use": True, "semantic_type": "metric", "id": metric["metricId"],
                                        "from_model": model_id, "metric_name": metric["metricName"]})
                all_fields.append({"semantic_type": "metric", "semantic_field": metric, "id": metric["metricId"],
                                   "from_model": model_name, "from_model_id": model_id})

            for dim in model["dimsAndMetrics"]["dimensions"]:
                dim_id = dim["dimensionId"]
                if dim["dimtype"].lower() == "time" or dim["dimtype"].lower() == "hc":
                    pass
                else:
                    dim['possibleValues'] = dimensions_value_dict.get(dim_id, [])
                available_dimensions.append({"is_allow_use": True, "semantic_type": "dimension", "id": dim_id,
                                             "dimension_name": dim["dimensionName"]})
                available_metrics.append(
                    {"is_allow_use": True, "semantic_type": "dimension", "id": dim_id, "from_model": model_name,
                     "from_model_id": model_id,
                     "dimension_name": dim["dimensionName"], "wid": str(uuid.uuid4())})
                whereable_fields.append(
                    {"is_allow_use": True, "semantic_type": "dimension", "id": dim_id, "from_model": model_name,
                     "from_model_id": model_id,
                     "dimension_name": dim["dimensionName"], "wid": str(uuid.uuid4())})
                sortable_fields.append(
                    {"is_allow_use": True, "semantic_type": "dimension", "id": dim_id, "from_model": model_id,
                     "dimension_name": dim["dimensionName"]})
                all_fields.append(
                    {"semantic_type": "dimension", "semantic_field": dim, "id": dim_id, "from_model": model_name,
                     "from_model_id": model_id})

        return {
            "available_dimensions": available_dimensions, "available_metrics": available_metrics,
            "sortable_fields": sortable_fields, "whereable_fields": whereable_fields,
            "havingable_fields": havingable_fields, "all_fields": all_fields
        }

    def _process_select_columns(self, parts: Dict[str, Any], used_table_detail_dict: Dict[str, Any]) -> List[Dict]:
        """处理SELECT列"""
        selected_columns = []
        table_alias_mapping = parts["table_alias_mapping"]
        for col in parts["select_columns"]:
            is_matched_semantic_field = False
            split_col = self._get_table_alias_and_field_by_split_column(col)
            if len(split_col) == 2:
                alias, column_name = split_col
                table_name, table_detail = self._resolve_table(alias, column_name, table_alias_mapping, used_table_detail_dict)
                if table_detail is None:
                    selected_columns.append(
                        {"is_semantic_field": False, "sql_column": col, "id": str(uuid.uuid4()), "wid": str(uuid.uuid4())})
                    continue
                for dim in table_detail['dimsAndMetrics']['dimensions']:
                    if dim['dimensionEnName'].lower() == column_name.lower():
                        selected_columns.append(
                            {"is_semantic_field": True, "semantic_type": "dimension", "id": dim["dimensionId"],
                             "dimension_name": dim["dimensionName"], "wid": str(uuid.uuid4())}
                        )
                        is_matched_semantic_field = True
                        break
                if is_matched_semantic_field:
                    continue
                for metric in table_detail['dimsAndMetrics']['metrics']:
                    if metric['expression'].lower() == table_name+'.'+column_name.lower():
                        selected_columns.append(
                            {"is_semantic_field": True, "semantic_type": "metric", "id": metric["metricId"],
                             "metric_name": metric["metricName"], "wid": str(uuid.uuid4())}
                        )
                        is_matched_semantic_field = True
                        break
                if is_matched_semantic_field:
                    continue
                selected_columns.append(
                    {"is_semantic_field": False, "sql_column": col, "id": str(uuid.uuid4()), "wid": str(uuid.uuid4())})
        return selected_columns

    def _get_table_detail_with_fallback(self, used_table_detail_dict: Dict[str, Any], table_name: str) -> Dict[
        str, Any]:
        """
        获取表详情，支持带引号/不带引号的表名容错处理
        """
        # 1. 先尝试原始表名
        if table_name in used_table_detail_dict:
            return used_table_detail_dict[table_name]

        # 2. 如果原始表名失败，尝试去掉引号
        if table_name.startswith('"') and table_name.endswith('"'):
            table_name_no_quotes = table_name[1:-1]
            if table_name_no_quotes in used_table_detail_dict:
                return used_table_detail_dict[table_name_no_quotes]

        # 3. 如果不带引号，尝试加上引号
        elif not (table_name.startswith('"') and table_name.endswith('"')):
            table_name_with_quotes = f'"{table_name}"'
            if table_name_with_quotes in used_table_detail_dict:
                return used_table_detail_dict[table_name_with_quotes]

        # 4. 都找不到，抛出更详细的异常
        available_keys = list(used_table_detail_dict.keys())
        raise KeyError(f"Table '{table_name}' not found. Available tables: {available_keys}")

    def _resolve_table(self, alias, column_name, table_alias_mapping, used_table_detail_dict):
        """
        把列解析为 (真实表名, 表详情)；无法安全确定来源表时返回 (None, None)，
        调用方据此把该字段降级为非语义字段——本方法绝不抛异常。

        解析阶梯（从最确定到最不确定，命中即止）：
          1) 别名命中映射 → 直接用（多表带前缀的正常路径，行为同改造前）；
          2) 别名为空/未命中，但本次查询只涉及唯一一张表 → 用那张表
             （覆盖「单表 + LLM 省略表前缀」高频场景，确定性恢复，无需猜测）；
          3) 别名为空/未命中且为多表 → 按列名在「本次真实 FROM 表」里反查唯一拥有者
             （详见 _resolve_multi_table_owner 的意图说明）。

        校验 dimsAndMetrics：getModelIndsAndDims 空返回会被直接赋成 None
        （见 model_dataset_resolver._ensure_dims_and_metrics），裸取其 dimensions/metrics
        会 TypeError，这里一并拦下并降级。
        """
        table_name = None
        if alias and alias in table_alias_mapping:
            table_name = table_alias_mapping[alias]
        else:
            # set(values()) 同时完成「去重」与「self-join 多别名折叠回同一张表」
            candidate_tables = set(table_alias_mapping.values())
            if len(candidate_tables) == 1:
                table_name = next(iter(candidate_tables))
            else:
                table_name = self._resolve_multi_table_owner(
                    column_name, candidate_tables, used_table_detail_dict)
        if not table_name:
            return None, None
        try:
            detail = self._get_table_detail_with_fallback(used_table_detail_dict, table_name)
        except KeyError:
            return None, None
        dims_and_metrics = detail.get("dimsAndMetrics") if isinstance(detail, dict) else None
        if (not isinstance(dims_and_metrics, dict)
                or "dimensions" not in dims_and_metrics
                or "metrics" not in dims_and_metrics):
            return None, None
        return table_name, detail

    def _resolve_multi_table_owner(self, column_name, candidate_tables, used_table_detail_dict):
        """
        多表 + 裸列（无表前缀）时，按列名反查它到底属于哪张表。返回唯一拥有者表名；
        无法唯一确定（0 张或 ≥2 张）则返回 None，让调用方降级——绝不猜。

        【为什么这样做是安全的，而不是在赌】
        这条 SQL 已经在真实库里执行过（data.result 就是它跑出来的）。一个无前缀的裸列
        还能在多表 JOIN 里成功执行，意味着它在 FROM scope 内不歧义——数据库已经替我们
        证明了「scope 内恰好一张表拥有它」。我们只是用同一份语义 schema 把这个既成事实
        查出来，不引入任何不确定性。

        【关键：候选集必须是「本次真实 FROM 的表」，不能是整个 used_table_detail_dict】
        generate() 会把主表的关联模型也并进 used_table_detail_dict（供前端面板列“可调整
        字段”，并非本次 SQL 参与的表）。若拿全量字典反查，会把没参与这条 SQL 的表算进候选，
        凭空造出本不存在的歧义、把本可命中的列误判成多拥有者而降级。所以候选集只取
        set(table_alias_mapping.values())（由 _resolve_table 收窄后传入），
        上面那条「能跑即唯一」的保证才成立。

        【为什么只认维度，不认指标】
        指标按 expression（t.col / count(t.col) 形态）匹配，裸列拿不到表前缀、无法干净反查；
        而裸列出现在 WHERE/GROUP BY/SELECT 平铺位置时几乎都是维度，聚合指标另走
        _build_selected_metrics 的表达式匹配。指标的统一限定名解析留给后续 sqlglot qualify。

        【0 张 / ≥2 张为何都降级】
        0 张：该列是表达式、计算列或未建模列，本就该按原样展示；
        ≥2 张：scope 收窄后仍多拥有者，只能是两表 dimensionEnName 命名巧合（物理上 DB 已
              证明不歧义），没有唯一正解——降级展示表达式永远不会错，强行选一个才会错。
        """
        owners = [
            table_name for table_name in candidate_tables
            if self._table_has_dimension(table_name, column_name, used_table_detail_dict)
        ]
        return owners[0] if len(owners) == 1 else None

    def _table_has_dimension(self, table_name, column_name, used_table_detail_dict):
        """该表的语义模型里是否建模了名为 column_name 的维度（按 dimensionEnName 比，忽略大小写）。
        仅供多表裸列反查定位来源表用；查不到表 / dimsAndMetrics 为 None 一律视为「没有」，绝不抛异常。
        """
        try:
            detail = self._get_table_detail_with_fallback(used_table_detail_dict, table_name)
        except KeyError:
            return False
        dims_and_metrics = detail.get("dimsAndMetrics") if isinstance(detail, dict) else None
        if not isinstance(dims_and_metrics, dict):
            return False
        target = column_name.lower()
        return any(
            (dim.get("dimensionEnName") or "").lower() == target
            for dim in (dims_and_metrics.get("dimensions") or [])
        )

    def _process_where_conditions(self, parts: Dict[str, Any], used_table_detail_dict: Dict[str, Any]) -> List[
        Dict[str, Any]]:
        """处理WHERE条件"""
        table_alias_mapping = parts["table_alias_mapping"]
        where_conditions = parts['where_conditions']
        if where_conditions.get('has_or'):
            raw = where_conditions['raw_condition']
            return [{"is_semantic_field": False, "is_complex_condition": True, "raw_condition": raw, "field": raw,
                     "operator": "", "value": "", "from_model": None, "id": str(uuid.uuid4()),
                     "wid": str(uuid.uuid4())}]

        filter_columns = []
        for cond in where_conditions.get('parsed_conditions', []):
            is_matched_semantic_field = False
            field = cond['field']
            operator = cond['operator']
            value = cond['value']
            # 复杂条件：field 含括号（表达式字段）或 value 是 SQL 表达式（函数/类型转换/时间常量）。
            # 原先只判断 field 导致 LLM 生成 `jysj >= (CURRENT_DATE - INTERVAL '7 days')::text`
            # 被当成普通语义字段，value 被作为字面量参数化绑定，re-query 时退化为字符串比较。
            if "(" in field or self._is_sql_expression_value(value):
                val_str = "" if value is None else str(value)
                raw_condition = (field + " " + operator + (" " + val_str if val_str else "")).strip()
                filter_columns.append(
                    {"is_semantic_field": False, "is_complex_condition": True,
                     "raw_condition": raw_condition, "from_model": None,
                     "field": field, "operator": operator, "value": val_str,
                     "id": str(uuid.uuid4()),
                     "wid": str(uuid.uuid4()), "original_sql_component": cond})
                continue
            table_alias, column_name = self._get_table_alias_and_field_by_split_column(field)
            table_name, table_detail = self._resolve_table(table_alias, column_name, table_alias_mapping, used_table_detail_dict)
            if table_detail is None:
                filter_columns.append(
                    {"is_semantic_field": False, "sql_column": cond["field"], "operator": operator, "value": value,
                     "id": str(uuid.uuid4()), "wid": str(uuid.uuid4())})
                continue
            for dim in table_detail['dimsAndMetrics']['dimensions']:
                if dim['dimensionEnName'].lower() == column_name.lower():
                    filter_columns.append(
                        {"is_semantic_field": True, "semantic_type": "dimension", "id": dim["dimensionId"],
                         "dimension_name": dim["dimensionName"], "operator": operator, "value": value,
                         "wid": str(uuid.uuid4()), "original_sql_component": cond}
                    )
                    is_matched_semantic_field = True
                    break
            if is_matched_semantic_field:
                continue
            for metric in table_detail['dimsAndMetrics']['metrics']:
                if metric['expression'].lower() == table_name+'.'+column_name.lower():
                    filter_columns.append(
                        {"is_semantic_field": True, "semantic_type": "metric", "id": metric["metricId"],
                         "metric_name": metric["metricName"], "operator": operator, "value": value,
                         "wid": str(uuid.uuid4()), "original_sql_component": cond}
                    )
                    is_matched_semantic_field = True
                    break
            if is_matched_semantic_field:
                continue
            filter_columns.append(
                {"is_semantic_field": False, "sql_column": cond["field"], "operator": operator, "value": value,
                 "id": str(uuid.uuid4()), "wid": str(uuid.uuid4())})

        return filter_columns

    def _process_order_by_fields(self, parts: Dict[str, Any], used_table_detail_dict: Dict[str, Any]) -> List[
        Dict[str, Any]]:
        """处理ORDER BY字段"""
        order_by_columns = []
        table_alias_mapping = parts["table_alias_mapping"]
        order_by_fields = parts['order_by']
        for order_info in order_by_fields:
            is_matched_semantic_field = False
            table_alias, column_name = self._get_table_alias_and_field_by_split_column(order_info['field'])
            direction = order_info['direction']
            table_name, table_detail = self._resolve_table(table_alias, column_name, table_alias_mapping, used_table_detail_dict)
            if table_detail is None:
                order_by_columns.append(
                    {"is_semantic_field": False, "sql_column": order_info['field'], "id": str(uuid.uuid4()),
                     "direction": direction, "wid": str(uuid.uuid4())})
                continue
            for metric in table_detail['dimsAndMetrics']['metrics']:
                if metric['expression'].lower() == table_name+'.'+column_name.lower():
                    order_by_columns.append(
                        {"is_semantic_field": True, "semantic_type": "metric", "id": metric["metricId"],
                         "metric_name": metric["metricName"], "direction": direction, "wid": str(uuid.uuid4()),
                         "original_sql_component": order_info}
                    )
                    is_matched_semantic_field = True
                    break
            if is_matched_semantic_field:
                continue
            for dim in table_detail['dimsAndMetrics']['dimensions']:
                if dim['dimensionEnName'].lower() == column_name.lower():
                    order_by_columns.append(
                        {"is_semantic_field": True, "semantic_type": "dimension", "id": dim["dimensionId"],
                         "dimension_name": dim["dimensionName"], "direction": direction, "wid": str(uuid.uuid4()),
                         "original_sql_component": order_info}
                    )
                    is_matched_semantic_field = True
                    break
            if is_matched_semantic_field:
                continue
            order_by_columns.append(
                {"is_semantic_field": False, "sql_column": order_info['field'], "id": str(uuid.uuid4()),
                 "direction": direction, "wid": str(uuid.uuid4())})

        return order_by_columns

    def _process_limit(self, parts: Dict[str, Any]) -> Optional[int]:
        """处理LIMIT字段"""
        limit = parts['limit']
        if not limit:
            return None
        return int(limit)

    def _get_table_alias_and_field_by_split_column(self, column: str) -> Tuple[str, str]:
        """
        将列名分割为 (table, field)
        支持以下格式：
        - 'table.field' -> ('table', 'field')
        - 'COUNT(table.field)' -> ('table', 'COUNT(field)')
        - 'COUNT(table.field) AS alias' -> ('table', 'COUNT(field)')
        - 'EXTRACT(YEAR FROM t1.hire_date)' -> ('t1', 'EXTRACT(YEAR FROM hire_date)')
        - 'SUBSTRING(t1.name FROM 1 FOR 10)' -> ('t1', 'SUBSTRING(name FROM 1 FOR 10)')
        - 'CASE WHEN t1.status = 1 THEN t1.name END' -> ('t1', 'CASE WHEN status = 1 THEN name END')
        - 'field' -> ('', 'field')
        - 'COUNT(field)' -> ('', 'COUNT(field)')
        """
        # 去除前后空格
        column = column.strip()

        # 处理 AS 别名，取 AS 前面的部分
        if ' AS ' in column.upper():
            column = column.split(' AS ')[0].strip()

        # 查找所有表别名引用 (格式: alias.field)
        table_refs = self._find_table_references(column)

        if not table_refs:
            # 没有找到表引用，检查是否是简单的聚合函数
            func_pattern = r'^([A-Z_]+)\((.*)\)$'
            match = re.match(func_pattern, column, re.IGNORECASE)
            if match:
                func_name = match.group(1)
                func_arg = match.group(2).strip()

                # 处理 DISTINCT 关键字
                distinct_prefix = ""
                if func_arg.upper().startswith('DISTINCT '):
                    distinct_prefix = "DISTINCT "
                    func_arg = func_arg[9:].strip()

                # 检查函数参数中是否有表前缀
                if '.' in func_arg and not ' ' in func_arg:
                    # 简单的 table.field 格式
                    parts = func_arg.split('.')
                    table = parts[0].strip()
                    field = parts[1].strip()
                    new_column = f"{func_name}({distinct_prefix}{field})"
                    return (table, new_column)
                else:
                    # 函数参数中没有表前缀或者是复杂表达式
                    if distinct_prefix:
                        new_column = f"{func_name}({distinct_prefix}{func_arg})"
                    else:
                        new_column = column
                    return ("", new_column)
            else:
                # 不是函数，按原逻辑处理
                parts = column.split('.')
                return (parts[0], parts[1]) if len(parts) == 2 else ("", column)

        # 有表引用，选择主要的表别名（出现次数最多的）
        main_table = max(table_refs, key=table_refs.count) if table_refs else ""

        # 移除主要表别名，生成新的列表达式
        new_column = self._remove_table_prefix(column, main_table)

        return (main_table, new_column)

    def _find_table_references(self, expression: str) -> List[str]:
        """
        查找表达式中的所有表别名引用
        返回表别名列表
        """
        # 匹配 alias.field 格式，但排除数字开头的（如 1.5）
        pattern = r'\b([a-zA-Z_][a-zA-Z0-9_]*)\.'
        matches = re.findall(pattern, expression)

        # 过滤掉SQL关键字和数字
        sql_keywords = {
            'SELECT', 'FROM', 'WHERE', 'JOIN', 'INNER', 'LEFT', 'RIGHT', 'OUTER',
            'ON', 'AND', 'OR', 'NOT', 'IN', 'EXISTS', 'CASE', 'WHEN', 'THEN',
            'ELSE', 'END', 'AS', 'DISTINCT', 'GROUP', 'BY', 'ORDER', 'HAVING',
            'LIMIT', 'OFFSET', 'UNION', 'ALL', 'EXTRACT', 'SUBSTRING', 'CAST',
            'CONVERT', 'DATEPART', 'DATEDIFF', 'YEAR', 'MONTH', 'DAY'
        }

        table_refs = []
        for match in matches:
            if match.upper() not in sql_keywords and not match.isdigit():
                table_refs.append(match)

        return table_refs

    def _remove_table_prefix(self, expression: str, table_alias: str) -> str:
        """
        从表达式中移除指定的表别名前缀
        例如: 'EXTRACT(YEAR FROM t1.hire_date)' -> 'EXTRACT(YEAR FROM hire_date)'
        """
        if not table_alias:
            return expression

        # 使用正则表达式匹配 table_alias. 但不匹配在字符串或标识符中间的
        # 确保匹配的是完整的表别名，不是标识符的一部分
        pattern = r'\b' + re.escape(table_alias) + r'\.'

        # 替换所有匹配的表前缀
        result = re.sub(pattern, '', expression)

        return result
