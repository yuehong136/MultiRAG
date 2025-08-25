"""
宽表SQL生成器
根据数据集信息、关联关系和权限配置，自动生成宽表查询SQL
"""
import logging
from typing import List, Dict
from .main_table_determiner import MainTableDeterminer

logger = logging.getLogger(__name__)


class WideTableSQLGenerator:
    """宽表SQL生成器"""

    def __init__(self):
        self.main_table_determiner = MainTableDeterminer()

    def generate_sql(
            self,
            dataset_detail: Dict,
            model_relationships: List[Dict],
            user_permissions: Dict,
            user_id: str
    ) -> str:
        """
        生成宽表查询SQL

        Args:
            dataset_detail: 数据集详情
            model_relationships: 模型关系
            user_permissions: 用户权限
            user_id: 用户ID

        Returns:
            生成的SQL语句
        """
        try:
            # 1. 确定主表
            main_model_id, score_detail = self.main_table_determiner.determine_main_table(
                dataset_detail,
                model_relationships,
                user_permissions
            )

            # 2. 构建表关联关系图
            join_graph = self._build_join_graph(
                main_model_id,
                dataset_detail.get("models", []),
                model_relationships
            )

            # 3. 应用权限过滤
            filtered_fields = self._apply_permissions(
                dataset_detail,
                user_permissions,
                user_id
            )

            # 4. 生成SQL
            sql = self._build_sql(
                main_model_id,
                dataset_detail.get("models", []),
                join_graph,
                filtered_fields,
                user_permissions
            )

            return sql

        except Exception as e:
            logger.error(f"生成宽表SQL失败: {str(e)}", exc_info=True)
            raise

    def _build_join_graph(
            self,
            main_model_id: str,
            models: List[Dict],
            relationships: List[Dict]
    ) -> Dict:
        """构建JOIN关系图"""
        join_graph = {
            "main_table": main_model_id,
            "joins": []
        }

        # 建立模型ID到模型信息的映射
        model_map = {m["modelId"]: m for m in models}

        # 找出所有需要关联的表
        visited = {main_model_id}
        to_visit = [main_model_id]

        while to_visit:
            current_model_id = to_visit.pop(0)

            # 查找当前表的所有关联
            for rel in relationships:
                source_id = rel.get("sourceModelId")
                target_id = rel.get("targetModelId")

                # 从当前表出发的关联
                if source_id == current_model_id and target_id not in visited:
                    if target_id in model_map:  # 确保目标表在数据集中
                        join_info = {
                            "left_table": model_map[source_id]["tableName"],
                            "left_field": rel.get("sourceField"),
                            "right_table": model_map[target_id]["tableName"],
                            "right_field": rel.get("targetField"),
                            "join_type": rel.get("joinType", "LEFT"),
                            "left_model_id": source_id,
                            "right_model_id": target_id
                        }
                        join_graph["joins"].append(join_info)
                        visited.add(target_id)
                        to_visit.append(target_id)

                # 到当前表的关联（反向）
                elif target_id == current_model_id and source_id not in visited:
                    if source_id in model_map:
                        join_info = {
                            "left_table": model_map[target_id]["tableName"],
                            "left_field": rel.get("targetField"),
                            "right_table": model_map[source_id]["tableName"],
                            "right_field": rel.get("sourceField"),
                            "join_type": rel.get("joinType", "LEFT"),
                            "left_model_id": target_id,
                            "right_model_id": source_id
                        }
                        join_graph["joins"].append(join_info)
                        visited.add(source_id)
                        to_visit.append(source_id)

        return join_graph

    def _apply_permissions(
            self,
            dataset_detail: Dict,
            user_permissions: Dict,
            user_id: str
    ) -> Dict:
        """应用权限过滤，返回允许访问的字段"""
        filtered_fields = {
            "dimensions": [],
            "metrics": []
        }

        if not user_permissions:
            # 没有权限信息，返回所有字段
            return {
                "dimensions": dataset_detail.get("dimensions", []),
                "metrics": dataset_detail.get("metrics", [])
            }

        # 获取权限配置
        data_permissions = user_permissions.get("dataPermissions", {})
        permission_models = data_permissions.get("models", [])

        # 建立模型ID到权限的映射
        permission_map = {pm["modelId"]: pm for pm in permission_models}

        # 过滤维度
        for dim in dataset_detail.get("dimensions", []):
            model_id = dim.get("modelId")
            if model_id in permission_map:
                allowed_columns = permission_map[model_id].get("allowedColumns", [])
                # 检查维度是否在允许列表中
                dim_id = dim.get("dimensionid")
                dim_name_en = dim.get("dimname_en")

                for allowed in allowed_columns:
                    if (allowed.get("semanticId") == dim_id or
                            allowed.get("semantic") == dim_name_en):
                        filtered_fields["dimensions"].append(dim)
                        break
            else:
                # 模型没有权限配置，默认允许
                filtered_fields["dimensions"].append(dim)

        # 过滤指标
        for metric in dataset_detail.get("metrics", []):
            model_id = metric.get("modelId")
            if model_id in permission_map:
                allowed_columns = permission_map[model_id].get("allowedColumns", [])
                # 检查指标是否在允许列表中
                metric_id = metric.get("metricid")
                metric_name_en = metric.get("indname_en")

                for allowed in allowed_columns:
                    if (allowed.get("semanticId") == metric_id or
                            allowed.get("semantic") == metric_name_en):
                        filtered_fields["metrics"].append(metric)
                        break
            else:
                # 模型没有权限配置，默认允许
                filtered_fields["metrics"].append(metric)

        return filtered_fields

    def _build_sql(
            self,
            main_model_id: str,
            models: List[Dict],
            join_graph: Dict,
            filtered_fields: Dict,
            user_permissions: Dict
    ) -> str:
        """构建最终的SQL语句"""
        # 建立模型映射
        model_map = {m["modelId"]: m for m in models}
        main_table = model_map[main_model_id]["tableName"]

        # 生成表别名
        table_aliases = self._generate_table_aliases(models)

        # 构建SELECT子句
        select_fields = []

        # 添加维度字段
        for dim in filtered_fields["dimensions"]:
            table_name = model_map[dim["modelId"]]["tableName"]
            alias = table_aliases[table_name]
            field_name = dim["dimname_en"]
            field_label = dim["dimensionname"]
            select_fields.append(f"{alias}.{field_name} AS \"{field_label}\"")

        # 添加指标字段
        for metric in filtered_fields["metrics"]:
            table_name = model_map[metric["modelId"]]["tableName"]
            alias = table_aliases[table_name]
            field_name = metric["indname_en"]
            field_label = metric["metricname"]
            select_fields.append(f"{alias}.{field_name} AS \"{field_label}\"")

        # 如果没有字段，至少选择一个
        if not select_fields:
            select_fields = ["*"]

        # 构建FROM子句
        main_alias = table_aliases[main_table]
        from_clause = f"{main_table} {main_alias}"

        # 构建JOIN子句
        join_clauses = []
        for join in join_graph["joins"]:
            left_alias = table_aliases[join["left_table"]]
            right_alias = table_aliases[join["right_table"]]
            join_type = join["join_type"]

            join_clause = (
                f"{join_type} JOIN {join['right_table']} {right_alias} "
                f"ON {left_alias}.{join['left_field']} = {right_alias}.{join['right_field']}"
            )
            join_clauses.append(join_clause)

        # 构建WHERE子句（行权限）
        where_clauses = self._build_where_clauses(
            models,
            table_aliases,
            user_permissions
        )

        # 组装SQL
        sql_parts = [
            "SELECT",
            "    " + ",\n    ".join(select_fields),
            f"FROM {from_clause}"
        ]

        if join_clauses:
            sql_parts.extend(join_clauses)

        if where_clauses:
            sql_parts.append(f"WHERE {where_clauses}")

        sql = "\n".join(sql_parts)

        # 记录生成的SQL
        logger.info(f"生成的宽表SQL:\n{sql}")

        return sql

    def _generate_table_aliases(self, models: List[Dict]) -> Dict[str, str]:
        """生成表别名"""
        aliases = {}
        used_aliases = set()

        for model in models:
            table_name = model["tableName"]
            # 使用表名首字母作为别名
            parts = table_name.split("_")
            alias = "".join([p[0] for p in parts if p])

            # 确保别名唯一
            if alias in used_aliases:
                counter = 1
                while f"{alias}{counter}" in used_aliases:
                    counter += 1
                alias = f"{alias}{counter}"

            aliases[table_name] = alias
            used_aliases.add(alias)

        return aliases

    def _build_where_clauses(
            self,
            models: List[Dict],
            table_aliases: Dict[str, str],
            user_permissions: Dict
    ) -> str:
        """构建WHERE子句（行权限）"""
        if not user_permissions:
            return ""

        where_conditions = []
        data_permissions = user_permissions.get("dataPermissions", {})
        permission_models = data_permissions.get("models", [])

        # 建立模型ID到模型的映射
        model_map = {m["modelId"]: m for m in models}

        for perm_model in permission_models:
            model_id = perm_model["modelId"]
            row_filter = perm_model.get("rowFilter", {})

            if not row_filter or model_id not in model_map:
                continue

            rules = row_filter.get("rules", [])
            logical_operator = row_filter.get("logicalOperator", "AND")

            if rules:
                table_name = model_map[model_id]["tableName"]
                alias = table_aliases[table_name]

                # 处理每个规则
                rule_conditions = []
                for rule in rules:
                    expression = rule.get("expression", "")
                    if expression:
                        # 替换WHERE关键字，添加表别名
                        expression = expression.replace("WHERE ", "")
                        # 简单处理：为字段添加表别名
                        # 注意：这里需要更复杂的SQL解析来正确处理
                        # 目前仅作示例
                        expression = f"({expression})"
                        rule_conditions.append(expression)

                if rule_conditions:
                    combined = f" {logical_operator} ".join(rule_conditions)
                    where_conditions.append(combined)

        return " AND ".join(where_conditions) if where_conditions else ""