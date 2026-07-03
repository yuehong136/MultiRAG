import re
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any


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
    def get_identifier_quote(db_type: DatabaseType) -> tuple[str, str]:
        """获取标识符引用符号"""
        quote_map = {DatabaseType.MYSQL: ("`", "`"), DatabaseType.POSTGRESQL: ('"', '"'), DatabaseType.SQLITE: ('"', '"'), DatabaseType.SQL_SERVER: ("[", "]"), DatabaseType.ORACLE: ('"', '"')}
        return quote_map.get(db_type, ("`", "`"))

    @staticmethod
    def escape_identifier(identifier: str, db_type: DatabaseType) -> str:
        """
        根据数据库类型转义标识符

        改进版本：能够智能处理已经部分或完全转义的标识符
        """
        if not identifier or identifier.strip() == "":
            return identifier

        # 清理标识符，移除多余空格
        identifier = identifier.strip()

        left_quote, right_quote = DatabaseDialect.get_identifier_quote(db_type)

        # 检查标识符是否已经被完全转义
        def is_fully_quoted(s: str) -> bool:
            """检查字符串是否已经被引号包围"""
            if not s:
                return False
            # 检查是否被当前数据库的引号包围
            if s.startswith(left_quote) and s.endswith(right_quote):
                return True
            # 也检查其他常见的引号
            quote_pairs = [("`", "`"), ('"', '"'), ("[", "]")]
            for lq, rq in quote_pairs:
                if s.startswith(lq) and s.endswith(rq):
                    return True
            return False

        def clean_quotes(s: str) -> str:
            """移除字符串两端的引号"""
            if not s:
                return s
            # 移除各种可能的引号
            quote_pairs = [("`", "`"), ('"', '"'), ("[", "]")]
            for lq, rq in quote_pairs:
                if s.startswith(lq) and s.endswith(rq):
                    return s[len(lq) : -len(rq)]
            return s

        def add_quotes(s: str) -> str:
            """给字符串添加适当的引号"""
            if not s:
                return s
            return f"{left_quote}{s}{right_quote}"

        # 处理复合标识符（如 table.column）
        if "." in identifier:
            parts = identifier.split(".")
            escaped_parts = []

            for part in parts:
                part = part.strip()
                if not part:  # 跳过空部分
                    continue

                # 如果这部分已经被引号包围，先移除旧引号
                if is_fully_quoted(part):
                    # 对于已经有引号的部分，检查是否需要更换引号类型
                    clean_part = clean_quotes(part)
                    escaped_parts.append(add_quotes(clean_part))
                else:
                    # 没有引号的部分，直接添加引号
                    escaped_parts.append(add_quotes(part))

            return ".".join(escaped_parts)
        else:
            # 单个标识符
            if is_fully_quoted(identifier):
                # 如果已经有引号，可能需要更换引号类型
                clean_identifier = clean_quotes(identifier)
                return add_quotes(clean_identifier)
            else:
                # 没有引号，直接添加
                return add_quotes(identifier)


class SQLFragment(ABC):
    """SQL片段抽象基类"""

    @abstractmethod
    def to_sql(self, db_type: DatabaseType = DatabaseType.POSTGRESQL) -> tuple[str, list[Any]]:
        """转换为SQL字符串和参数列表"""
        pass


class FilterCondition(SQLFragment):
    """标准过滤条件类"""

    def __init__(self, field: str, operator: FilterOperator, value: Any = None, value2: Any = None):
        self.field = field
        self.operator = operator
        self.value = value
        self.value2 = value2

    def to_sql(self, db_type: DatabaseType = DatabaseType.POSTGRESQL) -> tuple[str, list[Any]]:
        """转换为SQL条件和参数"""
        escaped_field = DatabaseDialect.escape_identifier(self.field, db_type)

        if self.operator in [FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL]:
            return f"{escaped_field} {self.operator.value}", []

        elif self.operator == FilterOperator.BETWEEN:
            return f"{escaped_field} BETWEEN %s AND %s", [self.value, self.value2]

        elif self.operator in [FilterOperator.IN, FilterOperator.NOT_IN]:
            if isinstance(self.value, (list, tuple)):
                placeholders = ",".join(["%s"] * len(self.value))
                return f"{escaped_field} {self.operator.value} ({placeholders})", list(self.value)
            else:
                placeholders = "%s"
                return f"{escaped_field} {self.operator.value} ({placeholders})", [self.value]

        else:
            return f"{escaped_field} {self.operator.value} %s", [self.value]


class RawSQLFragment(SQLFragment):
    """原始SQL片段类 - 支持纯自定义SQL"""

    def __init__(self, sql_content: str, parameters: list[Any] = None):
        """
        初始化原始SQL片段

        Args:
            sql_content: 原始SQL内容，可以是完整的SQL或带参数占位符的模板
            parameters: 可选的参数列表，如果SQL中有%s占位符
        """
        self.sql_content = sql_content.strip()
        self.parameters = parameters or []

        # 验证参数占位符数量
        placeholder_count = self.sql_content.count("%s")
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
            r"\bDROP\s+TABLE\b",
            r"\bDELETE\s+FROM\b",
            r"\bTRUNCATE\b",
            r"\bINSERT\s+INTO\b",
            r"\bUPDATE\s+\w+\s+SET\b",
            r"\bCREATE\s+TABLE\b",
            r"\bALTER\s+TABLE\b",
            r"--",  # SQL注释
            r"/\*.*?\*/",  # 多行注释
        ]

        sql_upper = self.sql_content.upper()
        for pattern in dangerous_patterns:
            if re.search(pattern, sql_upper):
                raise ValueError(f"检测到潜在危险的SQL操作: {pattern}")

    def to_sql(self, db_type: DatabaseType = DatabaseType.POSTGRESQL) -> tuple[str, list[Any]]:
        """转换为SQL字符串和参数列表"""
        return self.sql_content, self.parameters


class DynamicSQLFragment(SQLFragment):
    """动态SQL片段类 - 支持运行时替换变量"""

    def __init__(self, sql_template: str, dynamic_values: dict[str, Any] = None):
        """
        初始化动态SQL片段

        Args:
            sql_template: SQL模板，使用{变量名}进行占位
            dynamic_values: 动态值字典
        """
        self.sql_template = sql_template
        self.dynamic_values = dynamic_values or {}

    def to_sql(self, db_type: DatabaseType = DatabaseType.POSTGRESQL) -> tuple[str, list[Any]]:
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

    def to_sql(self, db_type: DatabaseType = DatabaseType.POSTGRESQL) -> tuple[str, list[Any]]:
        escaped_field = DatabaseDialect.escape_identifier(self.field, db_type)
        return f"{escaped_field} {self.direction.value}", []


class GroupByClause(SQLFragment):
    """分组子句类"""

    def __init__(self, field: str):
        self.field = field

    def to_sql(self, db_type: DatabaseType = DatabaseType.POSTGRESQL) -> tuple[str, list[Any]]:
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

    def to_sql(self, db_type: DatabaseType = DatabaseType.POSTGRESQL) -> tuple[str, list[Any]]:
        """转换为SQL条件和参数"""
        if self.operator in [FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL]:
            return f"{self.aggregate_expression} {self.operator.value}", []

        elif self.operator == FilterOperator.BETWEEN:
            return f"{self.aggregate_expression} BETWEEN %s AND %s", [self.value, self.value2]

        elif self.operator in [FilterOperator.IN, FilterOperator.NOT_IN]:
            if isinstance(self.value, (list, tuple)):
                placeholders = ",".join(["%s"] * len(self.value))
                return f"{self.aggregate_expression} {self.operator.value} ({placeholders})", list(self.value)
            else:
                placeholders = "%s"
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
        self.select_parts: list[SQLFragment] = []
        self.where_conditions: list[SQLFragment] = []
        self.group_by_parts: list[SQLFragment] = []
        self.having_conditions: list[SQLFragment] = []
        self.order_by_parts: list[SQLFragment] = []
        self.limit_count: int | None = None
        self.offset_count: int | None = None
        self.additional_clauses: dict[str, str] = {}

    @staticmethod
    def _clean_sql_clause(sql_input: str, clause_keywords: list[str], clause_name: str = "条件") -> tuple[str, bool]:
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
            pattern = rf"^\s*{re.escape(keyword)}\s+"
            if re.match(pattern, cleaned_sql, re.IGNORECASE):
                # 移除关键字
                cleaned_sql = re.sub(pattern, "", cleaned_sql, flags=re.IGNORECASE).strip()
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
        if cleaned_content.count("(") != cleaned_content.count(")"):
            raise ValueError(f"{clause_name}中的括号不匹配")

        return cleaned_content

    def set_database_type(self, db_type: DatabaseType) -> "FlexibleSQLAssembler":
        """设置数据库类型"""
        self.db_type = db_type
        return self

    # ============ SELECT相关方法 ============
    def add_column(self, column: str) -> "FlexibleSQLAssembler":
        """添加普通列"""
        # 使用DatabaseDialect来正确转义标识符
        escaped_column = DatabaseDialect.escape_identifier(column, self.db_type)
        raw_fragment = RawSQLFragment(escaped_column)
        self.select_parts.append(raw_fragment)
        return self

    def add_raw_column(self, sql_expression: str, alias: str = None) -> "FlexibleSQLAssembler":
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

    def add_parameterized_column(self, sql_template: str, parameters: list[Any], alias: str = None) -> "FlexibleSQLAssembler":
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

    def add_dynamic_column(self, sql_template: str, dynamic_values: dict[str, Any], alias: str = None) -> "FlexibleSQLAssembler":
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
    def add_filter(self, field: str, operator: FilterOperator, value: Any = None, value2: Any = None) -> "FlexibleSQLAssembler":
        """添加标准过滤条件"""
        condition = FilterCondition(field, operator, value, value2)
        self.where_conditions.append(condition)
        return self

    def add_raw_where(self, sql_condition: str, validate: bool = True) -> "FlexibleSQLAssembler":
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
            cleaned_condition, was_cleaned = self._clean_sql_clause(sql_condition, ["WHERE"], "WHERE条件")

            # 验证条件内容
            if validate:
                cleaned_condition = self._validate_condition_content(cleaned_condition, "WHERE条件")

            if was_cleaned:
                print(f"处理后的WHERE条件: {cleaned_condition}")

            raw_fragment = RawSQLFragment(cleaned_condition)
            self.where_conditions.append(raw_fragment)

        except ValueError as e:
            raise ValueError(f"WHERE条件添加失败: {e}")

        return self

    def add_parameterized_where(self, sql_template: str, parameters: list[Any]) -> "FlexibleSQLAssembler":
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
    def add_group_by(self, field: str) -> "FlexibleSQLAssembler":
        """添加标准分组字段"""
        group_clause = GroupByClause(field)
        self.group_by_parts.append(group_clause)
        return self

    def add_raw_group_by(self, sql_expression: str, validate: bool = True) -> "FlexibleSQLAssembler":
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
            cleaned_expression, was_cleaned = self._clean_sql_clause(sql_expression, ["GROUP BY"], "GROUP BY表达式")

            # 验证表达式内容
            if validate:
                cleaned_expression = self._validate_condition_content(cleaned_expression, "GROUP BY表达式")

            if was_cleaned:
                print(f"处理后的GROUP BY表达式: {cleaned_expression}")

            raw_fragment = RawSQLFragment(cleaned_expression)
            self.group_by_parts.append(raw_fragment)

        except ValueError as e:
            raise ValueError(f"GROUP BY表达式添加失败: {e}")

        return self

    # 添加便捷方法，支持多种输入方式
    def add_where_condition(self, condition: str | FilterCondition) -> "FlexibleSQLAssembler":
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

    def add_having_condition(self, condition: str | HavingCondition) -> "FlexibleSQLAssembler":
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

    def add_multiple_group_by(self, fields: list[str]) -> "FlexibleSQLAssembler":
        """批量添加分组字段"""
        for field in fields:
            self.add_group_by(field)
        return self

    # ============ HAVING相关方法 ============
    def add_having(self, aggregate_expression: str, operator: FilterOperator, value: Any = None, value2: Any = None) -> "FlexibleSQLAssembler":
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

    def add_raw_having(self, sql_condition: str, validate: bool = True) -> "FlexibleSQLAssembler":
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
            cleaned_condition, was_cleaned = self._clean_sql_clause(sql_condition, ["HAVING"], "HAVING条件")

            # 验证条件内容
            if validate:
                cleaned_condition = self._validate_condition_content(cleaned_condition, "HAVING条件")

            if was_cleaned:
                print(f"处理后的HAVING条件: {cleaned_condition}")

            raw_fragment = RawSQLFragment(cleaned_condition)
            self.having_conditions.append(raw_fragment)

        except ValueError as e:
            raise ValueError(f"HAVING条件添加失败: {e}")

        return self

    def add_parameterized_having(self, sql_template: str, parameters: list[Any]) -> "FlexibleSQLAssembler":
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
    def add_order_by(self, field: str, direction: OrderDirection | str = OrderDirection.ASC) -> "FlexibleSQLAssembler":
        """
        添加标准排序

        Args:
            field: 排序字段，可以是简单字段名或包含表别名的复合字段
            direction: 排序方向，可以是OrderDirection枚举或字符串
        """
        # 处理direction参数
        if isinstance(direction, str):
            direction = OrderDirection.from_value(direction.upper())

        order_clause = OrderByClause(field, direction)
        self.order_by_parts.append(order_clause)
        return self

    def add_raw_order_by(self, sql_expression: str, validate: bool = True) -> "FlexibleSQLAssembler":
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
            cleaned_expression, was_cleaned = self._clean_sql_clause(sql_expression, ["ORDER BY"], "ORDER BY表达式")

            # 验证表达式内容
            if validate:
                cleaned_expression = self._validate_condition_content(cleaned_expression, "ORDER BY表达式")

            if was_cleaned:
                print(f"处理后的ORDER BY表达式: {cleaned_expression}")

            raw_fragment = RawSQLFragment(cleaned_expression)
            self.order_by_parts.append(raw_fragment)

        except ValueError as e:
            raise ValueError(f"ORDER BY表达式添加失败: {e}")

        return self

    # ============ 其他方法 ============
    def set_limit(self, limit: int, offset: int = 0) -> "FlexibleSQLAssembler":
        """设置LIMIT和OFFSET"""
        self.limit_count = limit
        self.offset_count = offset if offset > 0 else None
        return self

    def set_offset(self, offset: int) -> "FlexibleSQLAssembler":
        """
        单独设置OFFSET值

        Args:
            offset: 偏移量，必须为非负整数

        Returns:
            self: 返回自身以支持链式调用

        Raises:
            ValueError: 如果offset为负数

        Example:
            >>> assembler = FlexibleSQLAssembler("users")
            >>> assembler.set_limit(10).set_offset(20)  # 获取第21-30条记录
            >>> assembler.set_offset(50)  # 单独设置offset
        """
        if offset < 0:
            raise ValueError("OFFSET值不能为负数")

        self.offset_count = offset if offset > 0 else None
        return self

    def set_pagination(self, page: int, page_size: int) -> "FlexibleSQLAssembler":
        """
        便捷的分页设置方法

        Args:
            page: 页码，从1开始
            page_size: 每页记录数

        Returns:
            self: 返回自身以支持链式调用

        Raises:
            ValueError: 如果page小于1或page_size小于等于0

        Example:
            >>> assembler = FlexibleSQLAssembler("users")
            >>> assembler.set_pagination(3, 20)  # 获取第3页，每页20条
            # 相当于 LIMIT 20 OFFSET 40
        """
        if page < 1:
            raise ValueError("页码必须从1开始")
        if page_size <= 0:
            raise ValueError("每页记录数必须大于0")

        offset = (page - 1) * page_size
        self.limit_count = page_size
        self.offset_count = offset if offset > 0 else None
        return self

    def clear_pagination(self) -> "FlexibleSQLAssembler":
        """
        清除分页设置（LIMIT和OFFSET）

        Returns:
            self: 返回自身以支持链式调用

        Example:
            >>> assembler = FlexibleSQLAssembler("users")
            >>> assembler.set_limit(10).set_offset(20)
            >>> assembler.clear_pagination()  # 清除limit和offset设置
        """
        self.limit_count = None
        self.offset_count = None
        return self

    def add_clause(self, clause_name: str, sql_content: str) -> "FlexibleSQLAssembler":
        """
        添加其他自定义子句

        Args:
            clause_name: 子句名称，如 'additional_joins'
            sql_content: SQL内容
        """
        self.additional_clauses[clause_name] = sql_content
        return self

    def build_sql(self) -> tuple[str, list[Any]]:
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
        if "additional_joins" in self.additional_clauses:
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
        if self.limit_count is not None or self.offset_count is not None:
            if self.db_type == DatabaseType.MYSQL:
                # MySQL支持两种格式：
                # 1. LIMIT limit
                # 2. LIMIT limit OFFSET offset （推荐，更清晰）
                # 3. LIMIT offset, limit （旧格式，容易混淆）
                if self.limit_count is not None and self.offset_count is not None:
                    # 使用更清晰的格式：LIMIT limit OFFSET offset
                    limit_clause = f"LIMIT {self.limit_count} OFFSET {self.offset_count}"
                elif self.limit_count is not None:
                    limit_clause = f"LIMIT {self.limit_count}"
                elif self.offset_count is not None:
                    # 仅有offset时，MySQL需要一个很大的limit值
                    limit_clause = f"LIMIT 18446744073709551615 OFFSET {self.offset_count}"

            elif self.db_type in [DatabaseType.POSTGRESQL, DatabaseType.SQLITE]:
                # PostgreSQL/SQLite: LIMIT limit OFFSET offset
                parts = []
                if self.limit_count is not None:
                    parts.append(f"LIMIT {self.limit_count}")
                if self.offset_count is not None:
                    parts.append(f"OFFSET {self.offset_count}")
                limit_clause = " ".join(parts)

            elif self.db_type == DatabaseType.SQL_SERVER:
                # SQL Server 2012+: 使用OFFSET...FETCH（需要ORDER BY）
                if self.order_by_parts:
                    offset_val = self.offset_count if self.offset_count is not None else 0
                    limit_clause = f"OFFSET {offset_val} ROWS"
                    if self.limit_count is not None:
                        limit_clause += f" FETCH NEXT {self.limit_count} ROWS ONLY"
                elif self.limit_count is not None:
                    # 没有ORDER BY时，使用TOP
                    # 注意：这需要在SELECT后面添加，不是在末尾
                    # 这里暂时使用标准格式，实际使用时可能需要调整
                    print("警告：SQL Server在没有ORDER BY时使用OFFSET/FETCH需要ORDER BY子句")

            elif self.db_type == DatabaseType.ORACLE:
                # Oracle 12c+: 使用OFFSET...FETCH
                if self.offset_count is not None or self.limit_count is not None:
                    offset_val = self.offset_count if self.offset_count is not None else 0
                    if offset_val > 0:
                        limit_clause = f"OFFSET {offset_val} ROWS"
                    if self.limit_count is not None:
                        if offset_val > 0:
                            limit_clause += f" FETCH NEXT {self.limit_count} ROWS ONLY"
                        else:
                            limit_clause = f"FETCH FIRST {self.limit_count} ROWS ONLY"

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

    def build_sql_for_jdbc(self) -> tuple[str, list[Any]]:
        """构建JDBC格式的SQL语句（使用?作为占位符）"""
        sql, params = self.build_sql()
        jdbc_sql = sql.replace("%s", "?")
        return jdbc_sql, params

    def build_count_sql_for_jdbc(self, cap: int | None = None) -> tuple[str, list[Any]]:
        """
        构建用于获取总数的JDBC格式SQL语句
        将当前SQL作为子查询，外层包装COUNT(*)

        Args:
            cap: 用户给定的行数上限（如自然语言里的「30 条」）。为正整数时，内层子查询保留
                 `LIMIT cap`，使 COUNT 返回 min(实际, cap)（且在大表上短路，不做全表 COUNT）；
                 为 None / 非正时维持原行为——清空 limit/offset，对全集计数。

        Returns:
            (JDBC格式的COUNT SQL, 参数列表)
        """
        # 临时保存 limit/offset；带 cap 时把内层封顶到 cap，否则清除（确保子查询不带分页窗口）
        temp_limit = self.limit_count
        temp_offset = self.offset_count
        if isinstance(cap, int) and cap > 0:
            self.limit_count = cap
            self.offset_count = None
        else:
            self.limit_count = None
            self.offset_count = None

        try:
            # 构建内部子查询SQL
            inner_sql, params = self.build_sql()

            # 将%s替换为?（JDBC格式）
            jdbc_inner_sql = inner_sql.replace("%s", "?")

            # 构建COUNT查询，将原SQL作为子查询
            count_sql = f"SELECT COUNT(*) FROM ({jdbc_inner_sql}) AS count_subquery"

            return count_sql, params

        finally:
            # 恢复limit设置
            self.limit_count = temp_limit
            self.offset_count = temp_offset

    def clear_all(self) -> "FlexibleSQLAssembler":
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
