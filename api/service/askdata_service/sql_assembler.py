from typing import List, Dict, Any, Optional, Union, Tuple
import re
from enum import Enum
from abc import ABC, abstractmethod


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


class SQLFragment(ABC):
    """SQL片段抽象基类"""

    @abstractmethod
    def to_sql(self) -> Tuple[str, List[Any]]:
        """转换为SQL字符串和参数列表"""
        pass


class FilterCondition(SQLFragment):
    """标准过滤条件类"""

    def __init__(self, field: str, operator: FilterOperator, value: Any = None, value2: Any = None):
        self.field = field
        self.operator = operator
        self.value = value
        self.value2 = value2

    def to_sql(self) -> Tuple[str, List[Any]]:
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
        if '.' in identifier:
            parts = identifier.split('.')
            return '.'.join([f'`{part}`' for part in parts])
        return f'`{identifier}`'


class RawSQLFragment(SQLFragment):
    """原始SQL片段类 - 支持纯自定义SQL"""

    def __init__(self, sql_content: str, parameters: List[Any] = None):
        """
        初始化原始SQL片段

        Args:
            sql_content: 原始SQL内容，可以是完整的SQL或带参数占位符的模板
            parameters: 可选的参数列表，如果SQL中有%s占位符
        """
        self.sql_content = sql_content.strip()
        self.parameters = parameters or []

        # 验证参数占位符数量
        placeholder_count = self.sql_content.count('%s')
        if placeholder_count != len(self.parameters):
            if placeholder_count > 0 and len(self.parameters) == 0:
                # 如果有占位符但没有参数，可能用户想要纯SQL，给出提示
                raise ValueError(f"SQL中包含{placeholder_count}个参数占位符，但未提供参数。如果要使用纯SQL，请避免使用%s")
            elif placeholder_count != len(self.parameters):
                raise ValueError(f"参数占位符数量({placeholder_count})与参数列表长度({len(self.parameters)})不匹配")

        # 基本安全检查
        self._validate_sql_safety()

    def _validate_sql_safety(self):
        """基本的SQL安全检查"""
        # 检查危险关键字（仅针对明显危险的操作）
        dangerous_patterns = [
            r'\bDROP\s+TABLE\b',
            r'\bDELETE\s+FROM\b',
            r'\bTRUNCATE\b',
            r'\bINSERT\s+INTO\b',
            r'\bUPDATE\s+\w+\s+SET\b',
            r'\bCREATE\s+TABLE\b',
            r'\bALTER\s+TABLE\b',
            r'--',  # SQL注释
            r'/\*.*?\*/',  # 多行注释
        ]

        sql_upper = self.sql_content.upper()
        for pattern in dangerous_patterns:
            if re.search(pattern, sql_upper):
                raise ValueError(f"检测到潜在危险的SQL操作: {pattern}")

    def to_sql(self) -> Tuple[str, List[Any]]:
        """转换为SQL字符串和参数列表"""
        return self.sql_content, self.parameters


class DynamicSQLFragment(SQLFragment):
    """动态SQL片段类 - 支持运行时替换变量"""

    def __init__(self, sql_template: str, dynamic_values: Dict[str, Any] = None):
        """
        初始化动态SQL片段

        Args:
            sql_template: SQL模板，使用{变量名}进行占位
            dynamic_values: 动态值字典
        """
        self.sql_template = sql_template
        self.dynamic_values = dynamic_values or {}

    def to_sql(self) -> Tuple[str, List[Any]]:
        """转换为SQL字符串和参数列表"""
        try:
            # 使用format进行变量替换
            formatted_sql = self.sql_template.format(**self.dynamic_values)
            return formatted_sql, []
        except KeyError as e:
            raise ValueError(f"模板变量 {e} 未在dynamic_values中定义")


class OrderByClause(SQLFragment):
    """排序子句类"""

    def __init__(self, field: str, direction: OrderDirection = OrderDirection.ASC):
        self.field = field
        self.direction = direction

    def to_sql(self) -> Tuple[str, List[Any]]:
        return f"`{self.field}` {self.direction.value}", []


class FlexibleSQLAssembler:
    """灵活的SQL组装器 - 支持多种自定义方式"""

    def __init__(self, base_from_clause: str):
        """
        初始化SQL组装器

        Args:
            base_from_clause: 基础的FROM子句
        """
        self.base_from_clause = base_from_clause.strip()
        self.select_parts: List[SQLFragment] = []  # 统一处理SELECT的各个部分
        self.where_conditions: List[SQLFragment] = []  # 统一处理WHERE条件
        self.order_by_parts: List[SQLFragment] = []  # 统一处理ORDER BY
        self.limit_count: Optional[int] = None
        self.offset_count: Optional[int] = None
        self.additional_clauses: Dict[str, str] = {}  # 其他自定义子句

    # ============ SELECT相关方法 ============
    def add_column(self, column: str) -> 'FlexibleSQLAssembler':
        """添加普通列"""
        raw_fragment = RawSQLFragment(f"`{column}`")
        self.select_parts.append(raw_fragment)
        return self

    def add_raw_column(self, sql_expression: str, alias: str = None) -> 'FlexibleSQLAssembler':
        """
        添加原始SQL表达式作为列

        Args:
            sql_expression: SQL表达式，如 "COUNT(CASE WHEN status = 'completed' THEN 1 END)"
            alias: 列别名
        """
        if alias:
            sql_expression = f"({sql_expression}) AS `{alias}`"

        raw_fragment = RawSQLFragment(sql_expression)
        self.select_parts.append(raw_fragment)
        return self

    def add_parameterized_column(self, sql_template: str, parameters: List[Any],
                                 alias: str = None) -> 'FlexibleSQLAssembler':
        """
        添加带参数的列表达式（用于动态值）

        Args:
            sql_template: 带参数占位符的SQL模板
            parameters: 参数列表
            alias: 列别名
        """
        if alias:
            sql_template = f"({sql_template}) AS `{alias}`"

        raw_fragment = RawSQLFragment(sql_template, parameters)
        self.select_parts.append(raw_fragment)
        return self

    def add_dynamic_column(self, sql_template: str, dynamic_values: Dict[str, Any],
                           alias: str = None) -> 'FlexibleSQLAssembler':
        """
        添加动态列表达式（用于配置驱动的场景）

        Args:
            sql_template: 带{变量}占位符的SQL模板
            dynamic_values: 变量值字典
            alias: 列别名
        """
        if alias:
            sql_template = f"({sql_template}) AS `{alias}`"

        dynamic_fragment = DynamicSQLFragment(sql_template, dynamic_values)
        self.select_parts.append(dynamic_fragment)
        return self

    # ============ WHERE相关方法 ============
    def add_filter(self, field: str, operator: FilterOperator, value: Any = None,
                   value2: Any = None) -> 'FlexibleSQLAssembler':
        """添加标准过滤条件"""
        condition = FilterCondition(field, operator, value, value2)
        self.where_conditions.append(condition)
        return self

    def add_raw_where(self, sql_condition: str) -> 'FlexibleSQLAssembler':
        """
        添加原始WHERE条件

        Args:
            sql_condition: 完整的WHERE条件，如 "u.created_at >= '2023-01-01'"
        """
        raw_fragment = RawSQLFragment(sql_condition)
        self.where_conditions.append(raw_fragment)
        return self

    def add_parameterized_where(self, sql_template: str, parameters: List[Any]) -> 'FlexibleSQLAssembler':
        """
        添加带参数的WHERE条件

        Args:
            sql_template: 带参数占位符的条件模板
            parameters: 参数列表
        """
        raw_fragment = RawSQLFragment(sql_template, parameters)
        self.where_conditions.append(raw_fragment)
        return self

    # ============ ORDER BY相关方法 ============
    def add_order_by(self, field: str, direction: OrderDirection = OrderDirection.ASC) -> 'FlexibleSQLAssembler':
        """添加标准排序"""
        order_clause = OrderByClause(field, direction)
        self.order_by_parts.append(order_clause)
        return self

    def add_raw_order_by(self, sql_expression: str) -> 'FlexibleSQLAssembler':
        """
        添加原始ORDER BY表达式

        Args:
            sql_expression: 排序表达式，如 "FIELD(status, 'active', 'pending', 'inactive')"
        """
        raw_fragment = RawSQLFragment(sql_expression)
        self.order_by_parts.append(raw_fragment)
        return self

    # ============ 其他方法 ============
    def set_limit(self, limit: int, offset: int = 0) -> 'FlexibleSQLAssembler':
        """设置LIMIT和OFFSET"""
        self.limit_count = limit
        self.offset_count = offset if offset > 0 else None
        return self

    def add_clause(self, clause_name: str, sql_content: str) -> 'FlexibleSQLAssembler':
        """
        添加其他自定义子句

        Args:
            clause_name: 子句名称，如 'additional_joins', 'having'
            sql_content: SQL内容
        """
        self.additional_clauses[clause_name] = sql_content
        return self

    def build_sql(self) -> Tuple[str, List[Any]]:
        """构建最终的SQL语句"""
        all_params = []

        # 构建SELECT子句
        if self.select_parts:
            select_expressions = []
            for part in self.select_parts:
                part_sql, part_params = part.to_sql()
                select_expressions.append(part_sql)
                all_params.extend(part_params)
            select_clause = "SELECT " + ", ".join(select_expressions)
        else:
            select_clause = "SELECT *"

        # 构建FROM子句
        from_clause = f"FROM {self.base_from_clause}"

        # 添加额外的JOIN
        if 'additional_joins' in self.additional_clauses:
            from_clause += f" {self.additional_clauses['additional_joins']}"

        # 构建WHERE子句
        where_clause = ""
        if self.where_conditions:
            where_expressions = []
            for condition in self.where_conditions:
                condition_sql, condition_params = condition.to_sql()
                where_expressions.append(condition_sql)
                all_params.extend(condition_params)
            where_clause = "WHERE " + " AND ".join(where_expressions)

        # 构建ORDER BY子句
        order_by_clause = ""
        if self.order_by_parts:
            order_expressions = []
            for part in self.order_by_parts:
                part_sql, part_params = part.to_sql()
                order_expressions.append(part_sql)
                all_params.extend(part_params)
            order_by_clause = "ORDER BY " + ", ".join(order_expressions)

        # 构建HAVING子句（如果有）
        having_clause = ""
        if 'having' in self.additional_clauses:
            having_clause = f"HAVING {self.additional_clauses['having']}"

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
        if having_clause:
            sql_parts.append(having_clause)
        if order_by_clause:
            sql_parts.append(order_by_clause)
        if limit_clause:
            sql_parts.append(limit_clause)

        final_sql = " ".join(sql_parts)
        return final_sql, all_params

    def clear_all(self) -> 'FlexibleSQLAssembler':
        """清空所有设置"""
        self.select_parts.clear()
        self.where_conditions.clear()
        self.order_by_parts.clear()
        self.additional_clauses.clear()
        self.limit_count = None
        self.offset_count = None
        return self


# 使用示例
if __name__ == "__main__":
    # 创建灵活的SQL组装器
    assembler = FlexibleSQLAssembler("users u LEFT JOIN departments d ON u.dept_id = d.id")

    print("=== 示例1：用户直接写完整SQL表达式 ===")
    sql1, params1 = (assembler
                     .add_column('u.name')
                     .add_column('u.email')
                     # 用户直接写完整的SQL表达式
                     .add_raw_column("COUNT(CASE WHEN o.status = 'completed' THEN 1 END)", 'completed_orders')
                     .add_raw_column("TIMESTAMPDIFF(YEAR, u.birth_date, CURDATE())", 'age')
                     .add_clause('additional_joins', 'LEFT JOIN orders o ON u.id = o.user_id')
                     .build_sql())

    print(f"SQL: {sql1}")
    print(f"参数: {params1}")
    print()

    print("=== 示例2：IN操作符的各种处理方式 ===")
    assembler.clear_all()

    # 方式1：标准IN操作（推荐用于动态值）
    dept_ids = [1, 2, 3, 4]  # 来自用户选择或配置
    user_roles = ['admin', 'manager', 'lead']  # 来自权限配置

    sql2, params2 = (assembler
                     .add_column('u.name')
                     .add_column('d.dept_name')
                     .add_filter('u.dept_id', FilterOperator.IN, dept_ids)  # 自动生成多个占位符
                     .add_filter('u.role', FilterOperator.IN, user_roles)  # 字符串列表
                     .add_filter('u.status', FilterOperator.NOT_IN, ['deleted', 'banned'])  # NOT IN
                     .build_sql())

    print(f"SQL: {sql2}")
    print(f"参数: {params2}")
    print()

    print("=== 示例3：用户自定义IN条件 ===")
    assembler.clear_all()

    # 方式2：用户直接写IN语句（适合固定值）
    sql3, params3 = (assembler
                     .add_column('u.name')
                     .add_raw_where("u.dept_id IN (1, 2, 3)")  # 用户直接写
                     .add_raw_where("u.role IN ('admin', 'manager')")  # 固定角色
                     .add_raw_where("u.id NOT IN (SELECT blocked_user_id FROM blocked_users)")  # 子查询
                     .build_sql())

    print(f"SQL: {sql3}")
    print(f"参数: {params3}")
    print()

    print("=== 示例4：混合IN操作和其他条件 ===")
    assembler.clear_all()

    # 场景：部分条件来自用户选择，部分来自系统规则
    selected_depts = [10, 20, 30]  # 用户在界面上选择的部门

    sql4, params4 = (assembler
                     .add_column('u.name')
                     .add_column('u.salary')
    .add_parameterized_where()
                     .add_filter('u.dept_id', FilterOperator.IN, selected_depts)  # 动态部门
                     .add_filter('u.status', FilterOperator.EQUALS, 'active')  # 固定状态
                     .add_raw_where("u.role IN ('admin', 'manager')")  # 固定角色限制
                     .add_raw_where("u.hire_date >= '2020-01-01'")  # 固定日期限制
                     .build_sql())

    print(f"SQL: {sql4}")
    print(f"参数: {params4}")
    print()

    print("=== 示例5：复杂IN场景 - 参数化子查询 ===")
    assembler.clear_all()

    # 高级场景：IN中包含参数化子查询
    current_year = 2024
    min_order_count = 5

    sql5, params5 = (assembler
                     .add_column('u.name')
                     .add_column('u.email')
                     .add_parameterized_where(
        """u.id IN (
            SELECT user_id 
            FROM orders 
            WHERE YEAR(created_at) = %s 
            GROUP BY user_id 
            HAVING COUNT(*) >= %s
        )""",
        [current_year, min_order_count]
    )
                     .build_sql())

    print(f"SQL: {sql5}")
    print(f"参数: {params5}")
    print()

    print("=== 示例6：单值IN的处理 ===")
    assembler.clear_all()

    # 测试单个值的IN操作
    single_dept = 5

    sql6, params6 = (assembler
                     .add_column('u.name')
                     .add_filter('u.dept_id', FilterOperator.IN, single_dept)  # 单个值
                     .add_filter('u.role', FilterOperator.IN, ['admin'])  # 单元素列表
                     .build_sql())

    print(f"SQL: {sql6}")
    print(f"参数: {params6}")
    print()

    print("=== 示例7：空列表IN的边界情况处理 ===")
    assembler.clear_all()

    try:
        # 演示空列表的处理（实际使用中应该在业务层面处理）
        empty_list = []
        if empty_list:  # 业务层检查
            assembler.add_filter('u.dept_id', FilterOperator.IN, empty_list)
        else:
            # 空列表时的替代处理
            assembler.add_raw_where("1 = 0")  # 或者其他业务逻辑

        sql7, params7 = assembler.add_column('u.name').build_sql()
        print(f"SQL: {sql7}")
        print(f"参数: {params7}")

    except Exception as e:
        print(f"处理空列表时的错误: {e}")

    print()

    print("=== 总结：IN操作的最佳实践 ===")
    print("1. 动态值列表 -> 使用 add_filter() + FilterOperator.IN")
    print("2. 固定值列表 -> 使用 add_raw_where() 直接写SQL")
    print("3. 复杂子查询 -> 使用 add_parameterized_where()")
    print("4. 空列表检查 -> 在业务层面提前处理")