from typing import List, Dict, Any, Optional, Union, Tuple
import re
from enum import Enum
from abc import ABC, abstractmethod


class DatabaseType(Enum):
    """数据库类型枚举"""
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    SQLITE = "sqlite"
    SQL_SERVER = "sql_server"
    ORACLE = "oracle"


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

    @classmethod
    def from_value(cls, value: str):
        for op in cls:
            if op.value == value:
                return op
        raise ValueError(f"'{value}' 不是有效的 FilterOperator 值。")


class OrderDirection(Enum):
    """排序方向枚举"""
    ASC = "ASC"
    DESC = "DESC"

    @classmethod
    def from_value(cls, value: str):
        for direction in cls:
            if direction.value == value:
                return direction
        raise ValueError(f"'{value}' 不是有效的 OrderDirection 值。")


class DatabaseDialect:
    """数据库方言类 - 处理不同数据库的语法差异"""

    @staticmethod
    def get_identifier_quote(db_type: DatabaseType) -> Tuple[str, str]:
        """获取标识符引用符号"""
        quote_map = {
            DatabaseType.MYSQL: ('`', '`'),
            DatabaseType.POSTGRESQL: ('"', '"'),
            DatabaseType.SQLITE: ('"', '"'),
            DatabaseType.SQL_SERVER: ('[', ']'),
            DatabaseType.ORACLE: ('"', '"')
        }
        return quote_map.get(db_type, ('`', '`'))

    @staticmethod
    def escape_identifier(identifier: str, db_type: DatabaseType) -> str:
        """根据数据库类型转义标识符"""
        if not identifier or identifier.strip() == '':
            return identifier

        # 清理标识符，移除多余空格
        identifier = identifier.strip()

        left_quote, right_quote = DatabaseDialect.get_identifier_quote(db_type)

        # 处理复合标识符（如 table.column）
        if '.' in identifier:
            parts = identifier.split('.')
            escaped_parts = []
            for part in parts:
                part = part.strip()
                if part:  # 确保不是空字符串
                    escaped_parts.append(f'{left_quote}{part}{right_quote}')
            return '.'.join(escaped_parts)
        else:
            return f'{left_quote}{identifier}{right_quote}'


class SQLFragment(ABC):
    """SQL片段抽象基类"""

    @abstractmethod
    def to_sql(self, db_type: DatabaseType = DatabaseType.POSTGRESQL) -> Tuple[str, List[Any]]:
        """转换为SQL字符串和参数列表"""
        pass


class FilterCondition(SQLFragment):
    """标准过滤条件类"""

    def __init__(self, field: str, operator: FilterOperator, value: Any = None, value2: Any = None):
        self.field = field
        self.operator = operator
        self.value = value
        self.value2 = value2

    def to_sql(self, db_type: DatabaseType = DatabaseType.POSTGRESQL) -> Tuple[str, List[Any]]:
        """转换为SQL条件和参数"""
        escaped_field = DatabaseDialect.escape_identifier(self.field, db_type)

        if self.operator in [FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL]:
            return f"{escaped_field} {self.operator.value}", []

        elif self.operator == FilterOperator.BETWEEN:
            return f"{escaped_field} BETWEEN %s AND %s", [self.value, self.value2]

        elif self.operator in [FilterOperator.IN, FilterOperator.NOT_IN]:
            if isinstance(self.value, (list, tuple)):
                placeholders = ','.join(['%s'] * len(self.value))
                return f"{escaped_field} {self.operator.value} ({placeholders})", list(self.value)
            else:
                placeholders = '%s'
                return f"{escaped_field} {self.operator.value} ({placeholders})", [self.value]

        else:
            return f"{escaped_field} {self.operator.value} %s", [self.value]


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

    def to_sql(self, db_type: DatabaseType = DatabaseType.POSTGRESQL) -> Tuple[str, List[Any]]:
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

    def to_sql(self, db_type: DatabaseType = DatabaseType.POSTGRESQL) -> Tuple[str, List[Any]]:
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

    def to_sql(self, db_type: DatabaseType = DatabaseType.POSTGRESQL) -> Tuple[str, List[Any]]:
        escaped_field = DatabaseDialect.escape_identifier(self.field, db_type)
        return f"{escaped_field} {self.direction.value}", []


class GroupByClause(SQLFragment):
    """分组子句类"""

    def __init__(self, field: str):
        self.field = field

    def to_sql(self, db_type: DatabaseType = DatabaseType.POSTGRESQL) -> Tuple[str, List[Any]]:
        escaped_field = DatabaseDialect.escape_identifier(self.field, db_type)
        return escaped_field, []


class HavingCondition(SQLFragment):
    """HAVING条件类 - 支持聚合函数的条件"""

    def __init__(self, aggregate_expression: str, operator: FilterOperator, value: Any = None, value2: Any = None):
        """
        初始化HAVING条件

        Args:
            aggregate_expression: 聚合表达式，如 "COUNT(*)", "SUM(amount)"
            operator: 操作符
            value: 值
            value2: 第二个值（用于BETWEEN）
        """
        self.aggregate_expression = aggregate_expression
        self.operator = operator
        self.value = value
        self.value2 = value2

    def to_sql(self, db_type: DatabaseType = DatabaseType.POSTGRESQL) -> Tuple[str, List[Any]]:
        """转换为SQL条件和参数"""
        if self.operator in [FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL]:
            return f"{self.aggregate_expression} {self.operator.value}", []

        elif self.operator == FilterOperator.BETWEEN:
            return f"{self.aggregate_expression} BETWEEN %s AND %s", [self.value, self.value2]

        elif self.operator in [FilterOperator.IN, FilterOperator.NOT_IN]:
            if isinstance(self.value, (list, tuple)):
                placeholders = ','.join(['%s'] * len(self.value))
                return f"{self.aggregate_expression} {self.operator.value} ({placeholders})", list(self.value)
            else:
                placeholders = '%s'
                return f"{self.aggregate_expression} {self.operator.value} ({placeholders})", [self.value]

        else:
            return f"{self.aggregate_expression} {self.operator.value} %s", [self.value]


class FlexibleSQLAssembler:
    """灵活的SQL组装器 - 支持多种自定义方式"""

    def __init__(self, base_from_clause: str, db_type: DatabaseType = DatabaseType.POSTGRESQL):
        """
        初始化SQL组装器

        Args:
            base_from_clause: 基础的FROM子句
            db_type: 数据库类型
        """
        self.base_from_clause = base_from_clause.strip()
        self.db_type = db_type
        self.select_parts: List[SQLFragment] = []
        self.where_conditions: List[SQLFragment] = []
        self.group_by_parts: List[SQLFragment] = []
        self.having_conditions: List[SQLFragment] = []
        self.order_by_parts: List[SQLFragment] = []
        self.limit_count: Optional[int] = None
        self.offset_count: Optional[int] = None
        self.additional_clauses: Dict[str, str] = {}

    @staticmethod
    def _clean_sql_clause(sql_input: str, clause_keywords: List[str],
                          clause_name: str = "条件") -> Tuple[str, bool]:
        """
        清理SQL子句，移除不必要的关键字

        Args:
            sql_input: 用户输入的SQL
            clause_keywords: 要检查和移除的关键字列表
            clause_name: 子句名称，用于日志

        Returns:
            (清理后的SQL, 是否进行了清理)
        """
        if not sql_input or not sql_input.strip():
            return sql_input, False

        cleaned_sql = sql_input.strip()
        was_cleaned = False

        # 检查是否以任何关键字开头（忽略大小写）
        for keyword in clause_keywords:
            pattern = rf'^\s*{re.escape(keyword)}\s+'
            if re.match(pattern, cleaned_sql, re.IGNORECASE):
                # 移除关键字
                cleaned_sql = re.sub(pattern, '', cleaned_sql, flags=re.IGNORECASE).strip()
                was_cleaned = True
                print(f"警告: 已自动移除{clause_name}中的 '{keyword}' 关键字")
                break

        return cleaned_sql, was_cleaned

    @staticmethod
    def _validate_condition_content(sql_content: str, clause_name: str = "条件") -> str:
        """
        验证条件内容的有效性

        Args:
            sql_content: SQL条件内容
            clause_name: 子句名称

        Returns:
            验证后的SQL内容

        Raises:
            ValueError: 如果内容无效
        """
        if not sql_content or not sql_content.strip():
            raise ValueError(f"{clause_name}内容不能为空")

        cleaned_content = sql_content.strip()

        # 检查是否只包含关键字（没有实际条件）
        if not cleaned_content:
            raise ValueError(f"{clause_name}必须包含实际的条件内容")

        # 基本的语法检查
        if cleaned_content.count('(') != cleaned_content.count(')'):
            raise ValueError(f"{clause_name}中的括号不匹配")

        return cleaned_content

    def set_database_type(self, db_type: DatabaseType) -> 'FlexibleSQLAssembler':
        """设置数据库类型"""
        self.db_type = db_type
        return self

    # ============ SELECT相关方法 ============
    def add_column(self, column: str) -> 'FlexibleSQLAssembler':
        """添加普通列"""
        # 使用DatabaseDialect来正确转义标识符
        escaped_column = DatabaseDialect.escape_identifier(column, self.db_type)
        raw_fragment = RawSQLFragment(escaped_column)
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
            escaped_alias = DatabaseDialect.escape_identifier(alias, self.db_type)
            sql_expression = f"({sql_expression}) AS {escaped_alias}"

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
            escaped_alias = DatabaseDialect.escape_identifier(alias, self.db_type)
            sql_template = f"({sql_template}) AS {escaped_alias}"

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
            escaped_alias = DatabaseDialect.escape_identifier(alias, self.db_type)
            sql_template = f"({sql_template}) AS {escaped_alias}"

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

    def add_raw_where(self, sql_condition: str, validate: bool = True) -> 'FlexibleSQLAssembler':
        """
        添加原始WHERE条件（增强版）

        Args:
            sql_condition: WHERE条件，支持以下格式：
                         - "u.created_at >= '2023-01-01'"  (推荐)
                         - "WHERE u.created_at >= '2023-01-01'"  (会自动清理)
            validate: 是否进行内容验证
        """
        try:
            # 清理可能的WHERE关键字
            cleaned_condition, was_cleaned = self._clean_sql_clause(
                sql_condition,
                ['WHERE'],
                'WHERE条件'
            )

            # 验证条件内容
            if validate:
                cleaned_condition = self._validate_condition_content(
                    cleaned_condition,
                    'WHERE条件'
                )

            if was_cleaned:
                print(f"处理后的WHERE条件: {cleaned_condition}")

            raw_fragment = RawSQLFragment(cleaned_condition)
            self.where_conditions.append(raw_fragment)

        except ValueError as e:
            raise ValueError(f"WHERE条件添加失败: {e}")

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

    # ============ GROUP BY相关方法 ============
    def add_group_by(self, field: str) -> 'FlexibleSQLAssembler':
        """添加标准分组字段"""
        group_clause = GroupByClause(field)
        self.group_by_parts.append(group_clause)
        return self

    def add_raw_group_by(self, sql_expression: str, validate: bool = True) -> 'FlexibleSQLAssembler':
        """
        添加原始GROUP BY表达式（增强版）

        Args:
            sql_expression: 分组表达式，支持以下格式：
                           - "DATE(created_at)"  (推荐)
                           - "GROUP BY DATE(created_at)"  (会自动清理)
            validate: 是否进行内容验证
        """
        try:
            # 清理可能的GROUP BY关键字
            cleaned_expression, was_cleaned = self._clean_sql_clause(
                sql_expression,
                ['GROUP BY'],
                'GROUP BY表达式'
            )

            # 验证表达式内容
            if validate:
                cleaned_expression = self._validate_condition_content(
                    cleaned_expression,
                    'GROUP BY表达式'
                )

            if was_cleaned:
                print(f"处理后的GROUP BY表达式: {cleaned_expression}")

            raw_fragment = RawSQLFragment(cleaned_expression)
            self.group_by_parts.append(raw_fragment)

        except ValueError as e:
            raise ValueError(f"GROUP BY表达式添加失败: {e}")

        return self

    # 添加便捷方法，支持多种输入方式
    def add_where_condition(self, condition: Union[str, FilterCondition]) -> 'FlexibleSQLAssembler':
        """
        智能添加WHERE条件，支持字符串和FilterCondition对象

        Args:
            condition: 可以是字符串条件或FilterCondition对象
        """
        if isinstance(condition, str):
            return self.add_raw_where(condition)
        elif isinstance(condition, FilterCondition):
            self.where_conditions.append(condition)
            return self
        else:
            raise ValueError("条件必须是字符串或FilterCondition对象")

    def add_having_condition(self, condition: Union[str, HavingCondition]) -> 'FlexibleSQLAssembler':
        """
        智能添加HAVING条件，支持字符串和HavingCondition对象

        Args:
            condition: 可以是字符串条件或HavingCondition对象
        """
        if isinstance(condition, str):
            return self.add_raw_having(condition)
        elif isinstance(condition, HavingCondition):
            self.having_conditions.append(condition)
            return self
        else:
            raise ValueError("条件必须是字符串或HavingCondition对象")

    def add_multiple_group_by(self, fields: List[str]) -> 'FlexibleSQLAssembler':
        """批量添加分组字段"""
        for field in fields:
            self.add_group_by(field)
        return self

    # ============ HAVING相关方法 ============
    def add_having(self, aggregate_expression: str, operator: FilterOperator, value: Any = None,
                   value2: Any = None) -> 'FlexibleSQLAssembler':
        """
        添加标准HAVING条件

        Args:
            aggregate_expression: 聚合表达式，如 "COUNT(*)", "SUM(amount)"
            operator: 操作符
            value: 值
            value2: 第二个值（用于BETWEEN）
        """
        having_condition = HavingCondition(aggregate_expression, operator, value, value2)
        self.having_conditions.append(having_condition)
        return self

    def add_raw_having(self, sql_condition: str, validate: bool = True) -> 'FlexibleSQLAssembler':
        """
        添加原始HAVING条件（增强版）

        Args:
            sql_condition: HAVING条件，支持以下格式：
                          - "COUNT(*) > 5"  (推荐)
                          - "HAVING COUNT(*) > 5"  (会自动清理)
            validate: 是否进行内容验证
        """
        try:
            # 清理可能的HAVING关键字
            cleaned_condition, was_cleaned = self._clean_sql_clause(
                sql_condition,
                ['HAVING'],
                'HAVING条件'
            )

            # 验证条件内容
            if validate:
                cleaned_condition = self._validate_condition_content(
                    cleaned_condition,
                    'HAVING条件'
                )

            if was_cleaned:
                print(f"处理后的HAVING条件: {cleaned_condition}")

            raw_fragment = RawSQLFragment(cleaned_condition)
            self.having_conditions.append(raw_fragment)

        except ValueError as e:
            raise ValueError(f"HAVING条件添加失败: {e}")

        return self

    def add_parameterized_having(self, sql_template: str, parameters: List[Any]) -> 'FlexibleSQLAssembler':
        """
        添加带参数的HAVING条件

        Args:
            sql_template: 带参数占位符的条件模板
            parameters: 参数列表
        """
        raw_fragment = RawSQLFragment(sql_template, parameters)
        self.having_conditions.append(raw_fragment)
        return self

    # ============ ORDER BY相关方法 ============
    def add_order_by(self, field: str, direction: OrderDirection = OrderDirection.ASC) -> 'FlexibleSQLAssembler':
        """添加标准排序"""
        order_clause = OrderByClause(field, direction)
        self.order_by_parts.append(order_clause)
        return self

    def add_raw_order_by(self, sql_expression: str, validate: bool = True) -> 'FlexibleSQLAssembler':
        """
        添加原始ORDER BY表达式（增强版）

        Args:
            sql_expression: 排序表达式，支持以下格式：
                           - "FIELD(status, 'active', 'pending', 'inactive')"  (推荐)
                           - "ORDER BY FIELD(status, 'active', 'pending', 'inactive')"  (会自动清理)
            validate: 是否进行内容验证
        """
        try:
            # 清理可能的ORDER BY关键字
            cleaned_expression, was_cleaned = self._clean_sql_clause(
                sql_expression,
                ['ORDER BY'],
                'ORDER BY表达式'
            )

            # 验证表达式内容
            if validate:
                cleaned_expression = self._validate_condition_content(
                    cleaned_expression,
                    'ORDER BY表达式'
                )

            if was_cleaned:
                print(f"处理后的ORDER BY表达式: {cleaned_expression}")

            raw_fragment = RawSQLFragment(cleaned_expression)
            self.order_by_parts.append(raw_fragment)

        except ValueError as e:
            raise ValueError(f"ORDER BY表达式添加失败: {e}")

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
            clause_name: 子句名称，如 'additional_joins'
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
                part_sql, part_params = part.to_sql(self.db_type)
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
                condition_sql, condition_params = condition.to_sql(self.db_type)
                where_expressions.append(condition_sql)
                all_params.extend(condition_params)
            where_clause = "WHERE " + " AND ".join(where_expressions)

        # 构建GROUP BY子句
        group_by_clause = ""
        if self.group_by_parts:
            group_expressions = []
            for part in self.group_by_parts:
                part_sql, part_params = part.to_sql(self.db_type)
                group_expressions.append(part_sql)
                all_params.extend(part_params)
            group_by_clause = "GROUP BY " + ", ".join(group_expressions)

        # 构建HAVING子句
        having_clause = ""
        if self.having_conditions:
            having_expressions = []
            for condition in self.having_conditions:
                condition_sql, condition_params = condition.to_sql(self.db_type)
                having_expressions.append(condition_sql)
                all_params.extend(condition_params)
            having_clause = "HAVING " + " AND ".join(having_expressions)

        # 构建ORDER BY子句
        order_by_clause = ""
        if self.order_by_parts:
            order_expressions = []
            for part in self.order_by_parts:
                part_sql, part_params = part.to_sql(self.db_type)
                order_expressions.append(part_sql)
                all_params.extend(part_params)
            order_by_clause = "ORDER BY " + ", ".join(order_expressions)

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
        if group_by_clause:
            sql_parts.append(group_by_clause)
        if having_clause:
            sql_parts.append(having_clause)
        if order_by_clause:
            sql_parts.append(order_by_clause)
        if limit_clause:
            sql_parts.append(limit_clause)

        final_sql = " ".join(sql_parts)
        return final_sql, all_params

    def build_sql_for_jdbc(self) -> Tuple[str, List[Any]]:
        """构建JDBC格式的SQL语句（使用?作为占位符）"""
        sql, params = self.build_sql()
        jdbc_sql = sql.replace('%s', '?')
        return jdbc_sql, params

    def clear_all(self) -> 'FlexibleSQLAssembler':
        """清空所有设置"""
        self.select_parts.clear()
        self.where_conditions.clear()
        self.group_by_parts.clear()
        self.having_conditions.clear()
        self.order_by_parts.clear()
        self.additional_clauses.clear()
        self.limit_count = None
        self.offset_count = None
        return self


# 使用示例
if __name__ == "__main__":
    # 示例1：基本的GROUP BY和HAVING使用
    print("=== 示例1：基本的GROUP BY和HAVING ===")
    assembler = FlexibleSQLAssembler(
        "orders o INNER JOIN customers c ON o.customer_id = c.id",
        DatabaseType.MYSQL
    )

    sql1, params1 = (assembler
                     .add_column("c.country")
                     .add_raw_column("COUNT(*)", "order_count")
                     .add_raw_column("SUM(o.total_amount)", "total_revenue")
                     .add_filter("o.status", FilterOperator.EQUALS, "completed")
                     .add_group_by("c.country")
                     .add_having("COUNT(*)", FilterOperator.GREATER_THAN, 10)
                     .add_having("SUM(o.total_amount)", FilterOperator.GREATER_THAN, 50000)
                     .add_order_by("total_revenue", OrderDirection.DESC)
                     .build_sql())

    print(f"SQL: {sql1}")
    print(f"参数: {params1}")
    print()

    # 示例2：复杂的GROUP BY（按表达式分组）
    print("=== 示例2：按表达式分组 ===")
    assembler.clear_all()

    sql2, params2 = (assembler
                     .add_raw_column("YEAR(o.created_at)", "order_year")
                     .add_raw_column("MONTH(o.created_at)", "order_month")
                     .add_raw_column("COUNT(*)", "monthly_orders")
                     .add_raw_column("AVG(o.total_amount)", "avg_order_value")
                     .add_filter("o.created_at", FilterOperator.GREATER_EQUAL, "2023-01-01")
                     .add_raw_group_by("YEAR(o.created_at), MONTH(o.created_at)")
                     .add_having("COUNT(*)", FilterOperator.GREATER_THAN, 100)
                     .add_order_by("order_year", OrderDirection.DESC)
                     .add_order_by("order_month", OrderDirection.DESC)
                     .build_sql())

    print(f"SQL: {sql2}")
    print(f"参数: {params2}")
    print()

    # 示例3：多字段分组
    print("=== 示例3：多字段分组 ===")
    assembler.clear_all()

    sql3, params3 = (assembler
                     .add_column("c.country")
                     .add_column("c.city")
                     .add_column("o.product_category")
                     .add_raw_column("COUNT(DISTINCT o.customer_id)", "unique_customers")
                     .add_raw_column("SUM(o.quantity)", "total_quantity")
                     .add_multiple_group_by(["c.country", "c.city", "o.product_category"])
                     .add_having("SUM(o.quantity)", FilterOperator.BETWEEN, 100, 1000)
                     .build_sql())

    print(f"SQL: {sql3}")
    print(f"参数: {params3}")
    print()

    # 示例4：参数化的HAVING条件
    print("=== 示例4：参数化的HAVING条件 ===")
    assembler.clear_all()

    min_orders = 5
    min_revenue = 10000
    max_revenue = 100000

    sql4, params4 = (assembler
                     .add_column("c.customer_id")
                     .add_column("c.customer_name")
                     .add_raw_column("COUNT(o.order_id)", "total_orders")
                     .add_raw_column("SUM(o.total_amount)", "lifetime_value")
                     .add_group_by("c.customer_id")
                     .add_group_by("c.customer_name")
                     .add_parameterized_having(
        "COUNT(o.order_id) >= %s AND SUM(o.total_amount) BETWEEN %s AND %s",
        [min_orders, min_revenue, max_revenue]
    )
                     .add_order_by("lifetime_value", OrderDirection.DESC)
                     .set_limit(10)
                     .build_sql())

    print(f"SQL: {sql4}")
    print(f"参数: {params4}")
    print()

    # 示例5：原始HAVING条件
    print("=== 示例5：复杂的原始HAVING条件 ===")
    assembler.clear_all()

    sql5, params5 = (assembler
                     .add_column("o.product_id")
                     .add_raw_column("COUNT(*)", "sale_count")
                     .add_raw_column("AVG(o.unit_price)", "avg_price")
                     .add_raw_column("STDDEV(o.unit_price)", "price_variance")
                     .add_group_by("o.product_id")
                     .add_raw_having(
        "COUNT(*) > 20 AND (AVG(o.unit_price) > 100 OR STDDEV(o.unit_price) < 10)"
    )
                     .build_sql())

    print(f"SQL: {sql5}")
    print(f"参数: {params5}")
    print()

    # 示例6：带ROLLUP的GROUP BY（MySQL特性）
    print("=== 示例6：带ROLLUP的GROUP BY ===")
    assembler.clear_all()

    sql6, params6 = (assembler
                     .add_column("c.region")
                     .add_column("c.country")
                     .add_raw_column("SUM(o.total_amount)", "total_sales")
                     .add_raw_column("COUNT(DISTINCT o.customer_id)", "customer_count")
                     .add_raw_group_by("c.region, c.country WITH ROLLUP")
                     .build_sql())

    print(f"SQL: {sql6}")
    print(f"参数: {params6}")
    print()

    # 示例7：综合示例 - 销售分析报表
    print("=== 示例7：综合销售分析报表 ===")
    assembler.clear_all()

    current_year = 2024

    sql7, params7 = (assembler
                     .add_raw_column("DATE_FORMAT(o.created_at, '%Y-%m')", "month")
                     .add_column("p.category")
                     .add_raw_column("COUNT(DISTINCT o.order_id)", "order_count")
                     .add_raw_column("COUNT(DISTINCT o.customer_id)", "customer_count")
                     .add_raw_column("SUM(o.quantity)", "units_sold")
                     .add_raw_column("SUM(o.total_amount)", "revenue")
                     .add_raw_column("AVG(o.total_amount)", "avg_order_value")
                     .add_raw_column("SUM(o.total_amount) / COUNT(DISTINCT o.customer_id)", "revenue_per_customer")
                     .add_clause("additional_joins", "INNER JOIN products p ON o.product_id = p.id")
                     .add_parameterized_where("YEAR(o.created_at) = %s", [current_year])
                     .add_filter("o.status", FilterOperator.IN, ["completed", "shipped"])
                     .add_raw_group_by("DATE_FORMAT(o.created_at, '%Y-%m'), p.category")
                     .add_having("SUM(o.total_amount)", FilterOperator.GREATER_THAN, 10000)
                     .add_raw_having("COUNT(DISTINCT o.customer_id) >= 50")
                     .add_raw_order_by("DATE_FORMAT(o.created_at, '%Y-%m')")
                     .add_order_by("revenue", OrderDirection.DESC)
                     .build_sql())

    print(f"SQL: {sql7}")
    print(f"参数: {params7}")
    print()

    # 示例8：HAVING中使用IN操作符
    print("=== 示例8：HAVING中使用IN操作符 ===")
    assembler.clear_all()

    target_counts = [10, 20, 30, 40, 50]

    sql8, params8 = (assembler
                     .add_column("c.customer_id")
                     .add_raw_column("COUNT(o.order_id)", "order_count")
                     .add_group_by("c.customer_id")
                     .add_having("COUNT(o.order_id)", FilterOperator.IN, target_counts)
                     .build_sql_for_jdbc())  # 使用JDBC格式

    print(f"JDBC SQL: {sql8}")
    print(f"参数: {params8}")
    print()

    print("=== 总结：GROUP BY和HAVING的使用方式 ===")
    print("1. 标准分组 -> 使用 add_group_by()")
    print("2. 表达式分组 -> 使用 add_raw_group_by()")
    print("3. 多字段分组 -> 使用 add_multiple_group_by()")
    print("4. 标准HAVING -> 使用 add_having()")
    print("5. 复杂HAVING -> 使用 add_raw_having()")
    print("6. 参数化HAVING -> 使用 add_parameterized_having()")

    print("=== 测试增强的raw方法 ===")

    assembler = FlexibleSQLAssembler("users u", DatabaseType.MYSQL)

    # 测试1: 用户误传入WHERE关键字
    print("\n测试1: 误传入WHERE关键字")
    try:
        assembler.add_raw_where("WHERE u.age > 18")
        assembler.add_raw_where("u.status = 'active'")  # 正确格式
        sql, params = assembler.build_sql()
        print(f"生成的SQL: {sql}")
    except ValueError as e:
        print(f"错误: {e}")

    assembler.clear_all()

    # 测试2: 用户误传入HAVING关键字
    print("\n测试2: 误传入HAVING关键字")
    try:
        assembler.add_raw_column("COUNT(*)", "user_count")
        assembler.add_group_by("u.department")
        assembler.add_raw_having("HAVING COUNT(*) > 5")  # 会被清理
        assembler.add_raw_having("SUM(u.salary) > 100000")  # 正确格式
        sql, params = assembler.build_sql()
        print(f"生成的SQL: {sql}")
    except ValueError as e:
        print(f"错误: {e}")

    assembler.clear_all()

    # 测试3: 用户传入空条件
    print("\n测试3: 传入空条件")
    try:
        assembler.add_raw_where("")
    except ValueError as e:
        print(f"预期错误: {e}")

    # 测试4: 用户传入只有关键字的条件
    print("\n测试4: 传入只有关键字的条件")
    try:
        assembler.add_raw_where("WHERE")
    except ValueError as e:
        print(f"预期错误: {e}")

    # 测试5: 测试ORDER BY和GROUP BY的清理
    print("\n测试5: ORDER BY和GROUP BY关键字清理")
    assembler.clear_all()
    try:
        assembler.add_raw_group_by("GROUP BY DATE(u.created_at)")
        assembler.add_raw_order_by("ORDER BY u.name ASC")
        assembler.add_raw_column("COUNT(*)", "count")
        sql, params = assembler.build_sql()
        print(f"生成的SQL: {sql}")
    except ValueError as e:
        print(f"错误: {e}")

    # 测试6: 智能条件添加方法
    print("\n测试6: 智能条件添加")
    assembler.clear_all()
    try:
        # 使用字符串
        assembler.add_where_condition("u.age > 18")
        # 使用FilterCondition对象
        condition = FilterCondition("u.status", FilterOperator.EQUALS, "active")
        assembler.add_where_condition(condition)

        sql, params = assembler.build_sql()
        print(f"生成的SQL: {sql}")
        print(f"参数: {params}")
    except ValueError as e:
        print(f"错误: {e}")

    print("\n=== 改进总结 ===")
    print("1. 自动检测并移除用户误传入的关键字（WHERE、HAVING、ORDER BY、GROUP BY）")
    print("2. 提供友好的警告信息，告知用户已进行自动清理")
    print("3. 增加内容验证，确保条件不为空且语法基本正确")
    print("4. 提供可选的验证开关，允许用户跳过验证")
    print("5. 添加智能条件添加方法，支持多种输入类型")
    print("6. 增强错误处理，提供更详细的错误信息")
