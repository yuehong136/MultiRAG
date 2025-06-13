from typing import List, Dict, Any, Optional, Union
import re
from enum import Enum


class FilterOperator(Enum):
    """过滤操作符枚举"""
    EQUALS = "="
    NOT_EQUALS = "!="
    GREATER_THAN = ">"
    LESS_THAN = "<"
    GREATER_EQUAL = ">="
    LESS_EQUAL = "<="
    LIKE = "LIKE"
    NOT_LIKE = "NOT LIKE"
    IN = "IN"
    NOT_IN = "NOT IN"
    IS_NULL = "IS NULL"
    IS_NOT_NULL = "IS NOT NULL"
    BETWEEN = "BETWEEN"


class OrderDirection(Enum):
    """排序方向枚举"""
    ASC = "ASC"
    DESC = "DESC"


class FilterCondition:
    """过滤条件类"""

    def __init__(self, field: str, operator: FilterOperator, value: Any = None, value2: Any = None):
        self.field = field
        self.operator = operator
        self.value = value
        self.value2 = value2  # 用于BETWEEN操作符的第二个值

    def to_sql(self) -> tuple:
        """转换为SQL条件和参数"""
        if self.operator in [FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL]:
            return f"{self._escape_identifier(self.field)} {self.operator.value}", []

        elif self.operator == FilterOperator.BETWEEN:
            return f"{self._escape_identifier(self.field)} BETWEEN %s AND %s", [self.value, self.value2]

        elif self.operator in [FilterOperator.IN, FilterOperator.NOT_IN]:
            if isinstance(self.value, (list, tuple)):
                placeholders = ','.join(['%s'] * len(self.value))
                return f"{self._escape_identifier(self.field)} {self.operator.value} ({placeholders})", list(self.value)
            else:
                placeholders = '%s'
                return f"{self._escape_identifier(self.field)} {self.operator.value} ({placeholders})", [self.value]

        else:
            return f"{self._escape_identifier(self.field)} {self.operator.value} %s", [self.value]

    def _escape_identifier(self, identifier: str) -> str:
        """转义标识符"""
        # 简单的标识符转义，可以根据具体数据库调整
        if '.' in identifier:
            parts = identifier.split('.')
            return '.'.join([f'`{part}`' for part in parts])
        return f'`{identifier}`'


class OrderByClause:
    """排序子句类"""

    def __init__(self, field: str, direction: OrderDirection = OrderDirection.ASC):
        self.field = field
        self.direction = direction

    def to_sql(self) -> str:
        return f"`{self.field}` {self.direction.value}"


class SQLAssembler:
    """SQL组装器"""

    def __init__(self, base_from_clause: str):
        """
        初始化SQL组装器

        Args:
            base_from_clause: 基础的FROM子句，例如 "table1 t1 LEFT JOIN table2 t2 ON t1.id = t2.id"
        """
        self.base_from_clause = base_from_clause.strip()
        self.columns: List[str] = []
        self.filters: List[FilterCondition] = []
        self.order_by_clauses: List[OrderByClause] = []
        self.limit_count: Optional[int] = None
        self.offset_count: Optional[int] = None

    def add_column(self, column: str) -> 'SQLAssembler':
        """添加查询列"""
        if column not in self.columns:
            self.columns.append(column)
        return self

    def add_columns(self, columns: List[str]) -> 'SQLAssembler':
        """批量添加查询列"""
        for column in columns:
            self.add_column(column)
        return self

    def clear_columns(self) -> 'SQLAssembler':
        """清空所有列"""
        self.columns.clear()
        return self

    def add_filter(self, field: str, operator: FilterOperator, value: Any = None, value2: Any = None) -> 'SQLAssembler':
        """添加过滤条件"""
        condition = FilterCondition(field, operator, value, value2)
        self.filters.append(condition)
        return self

    def add_filters(self, filters: List[Dict[str, Any]]) -> 'SQLAssembler':
        """
        批量添加过滤条件

        Args:
            filters: 过滤条件列表，每个元素是字典，包含field, operator, value等键
        """
        for filter_dict in filters:
            field = filter_dict['field']
            operator = FilterOperator(filter_dict['operator'])
            value = filter_dict.get('value')
            value2 = filter_dict.get('value2')
            self.add_filter(field, operator, value, value2)
        return self

    def clear_filters(self) -> 'SQLAssembler':
        """清空所有过滤条件"""
        self.filters.clear()
        return self

    def add_order_by(self, field: str, direction: OrderDirection = OrderDirection.ASC) -> 'SQLAssembler':
        """添加排序条件"""
        order_clause = OrderByClause(field, direction)
        self.order_by_clauses.append(order_clause)
        return self

    def clear_order_by(self) -> 'SQLAssembler':
        """清空排序条件"""
        self.order_by_clauses.clear()
        return self

    def set_limit(self, limit: int, offset: int = 0) -> 'SQLAssembler':
        """设置LIMIT和OFFSET"""
        self.limit_count = limit
        self.offset_count = offset if offset > 0 else None
        return self

    def clear_limit(self) -> 'SQLAssembler':
        """清空LIMIT设置"""
        self.limit_count = None
        self.offset_count = None
        return self

    def build_sql(self) -> tuple:
        """
        构建最终的SQL语句

        Returns:
            tuple: (sql_string, parameters_list)
        """
        # 构建SELECT子句
        if self.columns:
            select_clause = "SELECT " + ", ".join([f"`{col}`" for col in self.columns])
        else:
            select_clause = "SELECT *"

        # 构建FROM子句
        from_clause = f"FROM {self.base_from_clause}"

        # 构建WHERE子句
        where_clause = ""
        where_params = []
        if self.filters:
            where_conditions = []
            for filter_condition in self.filters:
                condition_sql, condition_params = filter_condition.to_sql()
                where_conditions.append(condition_sql)
                where_params.extend(condition_params)
            where_clause = "WHERE " + " AND ".join(where_conditions)

        # 构建ORDER BY子句
        order_by_clause = ""
        if self.order_by_clauses:
            order_by_parts = [clause.to_sql() for clause in self.order_by_clauses]
            order_by_clause = "ORDER BY " + ", ".join(order_by_parts)

        # 构建LIMIT子句
        limit_clause = ""
        if self.limit_count is not None:
            if self.offset_count is not None:
                limit_clause = f"LIMIT {self.offset_count}, {self.limit_count}"
            else:
                limit_clause = f"LIMIT {self.limit_count}"

        # 组装完整SQL
        sql_parts = [select_clause, from_clause]
        if where_clause:
            sql_parts.append(where_clause)
        if order_by_clause:
            sql_parts.append(order_by_clause)
        if limit_clause:
            sql_parts.append(limit_clause)

        final_sql = " ".join(sql_parts)

        return final_sql, where_params

    def get_sql_string(self) -> str:
        """获取SQL字符串（不包含参数）"""
        sql, params = self.build_sql()
        return sql

    def reset(self) -> 'SQLAssembler':
        """重置所有设置，但保留base_from_clause"""
        self.columns.clear()
        self.filters.clear()
        self.order_by_clauses.clear()
        self.limit_count = None
        self.offset_count = None
        return self


# 使用示例
if __name__ == "__main__":
    # 创建SQL组装器实例
    assembler = SQLAssembler("users u LEFT JOIN departments d ON u.dept_id = d.id")

    # 示例1：基本查询
    print("=== 示例1：基本查询 ===")
    sql1, params1 = (assembler
                     .add_columns(['u.name', 'u.email', 'd.dept_name'])
                     .add_filter('u.status', FilterOperator.EQUALS, 'active')
                     .add_filter('u.age', FilterOperator.GREATER_THAN, 18)
                     .add_order_by('u.name', OrderDirection.ASC)
                     .set_limit(20)
                     .build_sql())

    print(f"SQL: {sql1}")
    print(f"参数: {params1}")
    print()

    # 示例2：复杂查询条件
    print("=== 示例2：复杂查询条件 ===")
    assembler.reset()
    sql2, params2 = (assembler
                     .add_columns(['u.id', 'u.name', 'u.salary'])
                     .add_filter('u.dept_id', FilterOperator.IN, [1, 2, 3])
                     .add_filter('u.salary', FilterOperator.BETWEEN, 5000, 10000)
                     .add_filter('u.name', FilterOperator.LIKE, '%张%')
                     .add_order_by('u.salary', OrderDirection.DESC)
                     .add_order_by('u.name', OrderDirection.ASC)
                     .set_limit(10, 20)  # LIMIT 20, 10
                     .build_sql())

    print(f"SQL: {sql2}")
    print(f"参数: {params2}")
    print()

    # 示例3：使用字典批量添加过滤条件
    print("=== 示例3：批量添加过滤条件 ===")
    assembler.reset()

    filters = [
        {'field': 'u.status', 'operator': '=', 'value': 'active'},
        {'field': 'u.created_at', 'operator': '>=', 'value': '2023-01-01'},
        {'field': 'u.email', 'operator': 'IS NOT NULL'}
    ]

    sql3, params3 = (assembler
                     .add_filter('u.deleted_at', FilterOperator.IS_NULL)
                     .add_order_by('u.created_at', OrderDirection.DESC)
                     .build_sql())

    print(f"SQL: {sql3}")
    print(f"参数: {params3}")
