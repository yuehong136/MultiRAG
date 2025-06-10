import sqlparse
from sqlparse.sql import IdentifierList, Identifier, Where, Token, TokenList
from sqlparse.tokens import Keyword, DML, Punctuation
import re


class SQLParser:
    """SQL解析器，用于提取SQL语句的各个部分"""

    def __init__(self, sql):
        self.sql = sql
        self.parsed = sqlparse.parse(sql)[0]
        self._table_alias_map = {}  # 存储表别名到表名的映射

    def _is_subselect(self, token):
        """检查是否是子查询"""
        if not token.is_group:
            return False
        return token.tokens[0].ttype is DML and token.tokens[0].value.upper() == 'SELECT'

    def _extract_from_part(self):
        """提取FROM到下一个主要子句之间的部分"""
        from_part = []
        from_seen = False

        for token in self.parsed.tokens:
            if from_seen:
                # 遇到JOIN关键字时停止，因为JOIN的表不属于FROM部分
                if isinstance(token, Where) or (token.ttype is Keyword and
                                                token.value.upper() in ['WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT',
                                                                        'OFFSET',
                                                                        'UNION', 'EXCEPT', 'INTERSECT', 'JOIN', 'INNER',
                                                                        'LEFT', 'RIGHT', 'FULL', 'CROSS']):
                    break
                from_part.append(token)
            elif token.ttype is Keyword and token.value.upper() == 'FROM':
                from_seen = True

        return from_part

    def _extract_table_identifiers(self, token_list):
        """从token列表中提取表标识符"""
        tables = []

        for token in token_list:
            if isinstance(token, IdentifierList):
                for identifier in token.get_identifiers():
                    tables.append(str(identifier).strip())
            elif isinstance(token, Identifier):
                tables.append(str(token).strip())
            elif token.ttype is None and not token.is_whitespace:
                # 处理简单表名（没有别名的情况）
                table_str = str(token).strip()
                if table_str and not table_str.startswith('('):
                    tables.append(table_str)

        return tables

    def _build_table_alias_map(self):
        """构建表别名到表名的映射"""
        self._table_alias_map = {}

        # 从FROM子句获取表和别名
        from_tables = self.extract_from_tables()
        for table_info in from_tables:
            parts = table_info.split()
            if len(parts) >= 2:
                # 表名 别名 的格式
                table_name = parts[0]
                alias = parts[1]
                self._table_alias_map[alias] = table_name
            else:
                # 没有别名的情况，表名即是别名
                self._table_alias_map[table_info] = table_info

        # 从JOIN子句获取表和别名
        join_info = self.extract_join_info()
        for join in join_info:
            table_info = join['table']
            parts = table_info.split()
            if len(parts) >= 2:
                table_name = parts[0]
                alias = parts[1]
                self._table_alias_map[alias] = table_name
            else:
                self._table_alias_map[table_info] = table_info

    def _convert_field_to_full_name(self, field):
        """
        将字段转换为完整的表名.字段名格式

        Args:
            field (str): 字段名，可能是 alias.column 或 column 格式

        Returns:
            str: 完整的表名.字段名格式
        """
        # 确保表别名映射已构建
        if not self._table_alias_map:
            self._build_table_alias_map()

        # 如果字段包含点号，说明是别名.字段名或表名.字段名格式
        if '.' in field:
            parts = field.split('.', 1)
            if len(parts) == 2:
                alias_or_table = parts[0]
                column_name = parts[1]

                # 查找别名对应的表名
                if alias_or_table in self._table_alias_map:
                    table_name = self._table_alias_map[alias_or_table]
                    return f"{table_name}.{column_name}"
                else:
                    # 如果找不到别名映射，可能已经是表名.字段名格式，保持原样
                    return field
        else:
            # 如果字段不包含点号，需要确定属于哪个表
            # 策略：优先使用主表（FROM子句中的第一个表）
            if self._table_alias_map:
                # 获取主表（FROM子句中的第一个表）
                from_tables = self.extract_from_tables()
                if from_tables:
                    # 获取第一个表的实际表名
                    main_table_info = from_tables[0]
                    parts = main_table_info.split()
                    main_table_name = parts[0]  # 取表名部分
                    return f"{main_table_name}.{field}"
                else:
                    # 如果没有FROM表，使用映射中的第一个表
                    first_table = list(self._table_alias_map.values())[0]
                    return f"{first_table}.{field}"
            else:
                # 如果没有表信息，返回原字段
                return field

        return field

    def extract_table_alias_mapping(self):
        """提取表和表别名的映射关系

        Returns:
            dict: 表别名到表名的映射字典，格式为 {别名: 表名}
                 如果表没有别名，则 {表名: 表名}
        """
        # 确保映射已构建
        self._build_table_alias_map()

        # 返回映射关系的副本，避免外部修改
        return self._table_alias_map.copy()

    def extract_select_columns(self):
        """提取SELECT列"""
        columns = []
        select_seen = False

        for token in self.parsed.tokens:
            if token.ttype is DML and token.value.upper() == 'SELECT':
                select_seen = True
                continue

            if select_seen:
                if token.ttype is Keyword:
                    break

                if not token.is_whitespace:
                    if isinstance(token, IdentifierList):
                        for identifier in token.get_identifiers():
                            columns.append(str(identifier).strip())
                    elif isinstance(token, Identifier):
                        columns.append(str(token).strip())
                    elif token.ttype is None:
                        # 处理特殊情况，如 COUNT(*), *等
                        col_str = str(token).strip()
                        if col_str and col_str not in ['(', ')']:
                            columns.append(col_str)

        return columns

    def extract_select_columns_with_full_names(self):
        """提取SELECT列并转换为完整的表名.列名格式"""
        # 首先构建表别名映射
        self._build_table_alias_map()

        # 获取原始的select列
        columns = self.extract_select_columns()

        # 转换为完整列名
        full_columns = []
        for col in columns:
            # 检查是否已经包含点号（已经是完整格式）
            if '.' in col:
                # 分离别名和列名
                parts = col.split('.', 1)
                if len(parts) == 2:
                    alias = parts[0]
                    column_name = parts[1]

                    # 查找别名对应的表名
                    if alias in self._table_alias_map:
                        full_name = f"{self._table_alias_map[alias]}.{column_name}"
                        full_columns.append(full_name)
                    else:
                        # 如果找不到别名映射，保持原样
                        full_columns.append(col)
                else:
                    full_columns.append(col)
            else:
                # 没有表前缀的列，保持原样
                full_columns.append(col)

        return full_columns

    def extract_from_tables(self):
        """提取FROM表"""
        from_part = self._extract_from_part()
        raw_tables = self._extract_table_identifiers(from_part)

        # 清理表名，移除明显不是表名的内容
        clean_tables = []
        for table in raw_tables:
            # 跳过包含等号的（这是条件，不是表名）
            if '=' in table:
                continue
            # 跳过包含比较运算符的
            if any(op in table for op in ['>', '<', '>=', '<=', '!=', '<>']):
                continue
            # 跳过包含AND/OR的（这是条件）
            if any(keyword in table.upper() for keyword in ['AND', 'OR']):
                continue
            # 跳过只包含列名的（没有表别名，且包含点号）
            if '.' in table and len(table.split()) == 1:
                continue
            # 跳过函数调用
            if '(' in table and ')' in table:
                continue

            # 清理后的表名
            clean_tables.append(table)

        return clean_tables

    def extract_join_info(self):
        """提取JOIN信息"""
        joins = []

        # 使用正则表达式匹配JOIN模式
        import re

        # JOIN模式：包括可选的JOIN类型和必须的JOIN关键字
        join_pattern = r'\b((?:INNER|LEFT\s+OUTER|RIGHT\s+OUTER|FULL\s+OUTER|LEFT|RIGHT|FULL|CROSS)\s+)?JOIN\s+([^\s]+(?:\s+[^\s]+)?)\s*(?:ON\s+([^WHERE|GROUP|HAVING|ORDER|LIMIT|UNION|EXCEPT|INTERSECT|JOIN]+?)(?=\s*(?:WHERE|GROUP|HAVING|ORDER|LIMIT|UNION|EXCEPT|INTERSECT|INNER|LEFT|RIGHT|FULL|CROSS|JOIN|$)))?'

        matches = re.finditer(join_pattern, self.sql, re.IGNORECASE | re.DOTALL)

        for match in matches:
            join_type = match.group(1)
            if join_type:
                join_type = join_type.strip() + ' JOIN'
            else:
                join_type = 'JOIN'

            table = match.group(2).strip()
            condition = match.group(3)

            if condition:
                condition = condition.strip()
                # 清理条件中的多余空格
                condition = ' '.join(condition.split())

            joins.append({
                'type': join_type,
                'table': table,
                'condition': condition
            })

        return joins

    def extract_where_conditions(self):
        """提取WHERE条件"""
        for token in self.parsed.tokens:
            if isinstance(token, Where):
                # 移除WHERE关键字，只保留条件部分
                conditions = []
                for t in token.tokens:
                    if t.ttype is not Keyword or t.value.upper() != 'WHERE':
                        conditions.append(str(t))
                return ''.join(conditions).strip()

        return None

    def extract_where_conditions_detailed(self):
        """
        提取WHERE条件的详细结构

        规则：
        1. 默认所有条件都是以AND连接
        2. 以AND为分隔符，提取出字段、操作符和值
        3. 如果存在OR，则返回整个WHERE条件（不进行细化拆分）
        4. 将字段转换为完整的表名.字段名格式

        Returns:
            dict: 包含以下结构的字典
                - 'has_or': bool, 是否包含OR操作符
                - 'raw_condition': str, 原始WHERE条件
                - 'parsed_conditions': list, 解析后的条件列表（仅当has_or=False时）
                  每个条件包含: {'field': str, 'operator': str, 'value': str, 'full_field': str}
        """
        # 确保表别名映射已构建
        self._build_table_alias_map()

        where_condition = self.extract_where_conditions()

        if not where_condition:
            return {
                'has_or': False,
                'raw_condition': None,
                'parsed_conditions': []
            }

        # 检查是否包含OR（忽略大小写）
        has_or = bool(re.search(r'\bOR\b', where_condition, re.IGNORECASE))

        result = {
            'has_or': has_or,
            'raw_condition': where_condition,
            'parsed_conditions': []
        }

        # 如果包含OR，直接返回原始条件，不进行细化解析
        if has_or:
            return result

        # 解析AND连接的条件
        # 先简单按AND分割（注意处理大小写）
        and_conditions = re.split(r'\s+AND\s+', where_condition, flags=re.IGNORECASE)

        for condition in and_conditions:
            condition = condition.strip()
            if not condition:
                continue

            parsed_condition = self._parse_single_condition(condition)
            if parsed_condition:
                result['parsed_conditions'].append(parsed_condition)

        return result

    def _parse_single_condition(self, condition):
        """
        解析单个条件，提取字段、操作符和值，并将字段转换为完整格式

        Args:
            condition (str): 单个条件字符串

        Returns:
            dict: {'field': str, 'operator': str, 'value': str, 'full_field': str} 或 None
        """
        condition = condition.strip()

        # 定义各种操作符的正则模式（按优先级排序，长的在前）
        operators = [
            (r'\s+IS\s+NOT\s+NULL\b', 'IS NOT', 'NULL'),  # 特殊处理：IS NOT NULL
            (r'\s+IS\s+NULL\b', 'IS', 'NULL'),  # 特殊处理：IS NULL
            (r'\s+NOT\s+LIKE\s+', 'NOT LIKE'),
            (r'\s+NOT\s+IN\s+', 'NOT IN'),
            (r'\s+LIKE\s+', 'LIKE'),
            (r'\s+IN\s+', 'IN'),
            (r'\s*>=\s*', '>='),
            (r'\s*<=\s*', '<='),
            (r'\s*!=\s*', '!='),
            (r'\s*<>\s*', '<>'),
            (r'\s*=\s*', '='),
            (r'\s*>\s*', '>'),
            (r'\s*<\s*', '<'),
        ]

        for operator_info in operators:
            if len(operator_info) == 3:  # 特殊处理的NULL情况
                pattern, op, special_value = operator_info
                match = re.search(pattern, condition, re.IGNORECASE)
                if match:
                    field = condition[:match.start()].strip()
                    full_field = self._convert_field_to_full_name(field)
                    return {
                        'field': field,
                        'full_field': full_field,
                        'operator': op,
                        'value': special_value
                    }
            else:  # 普通操作符
                pattern, op = operator_info
                match = re.search(pattern, condition, re.IGNORECASE)
                if match:
                    # 分割字段和值
                    field = condition[:match.start()].strip()
                    value = condition[match.end():].strip()

                    # 转换字段为完整格式
                    full_field = self._convert_field_to_full_name(field)

                    # 移除值两端的引号（如果有的话）
                    if value:
                        if (value.startswith("'") and value.endswith("'")) or \
                                (value.startswith('"') and value.endswith('"')):
                            value = value[1:-1]

                    return {
                        'field': field,
                        'full_field': full_field,
                        'operator': op,
                        'value': value
                    }

        # 如果没有匹配到任何操作符，返回None
        return None

    def _extract_group_by_part(self):
        """提取GROUP BY到下一个主要子句之间的部分"""
        group_part = []
        group_seen = False
        by_seen = False

        for token in self.parsed.tokens:
            if token.ttype is Keyword and token.value.upper() == 'GROUP':
                group_seen = True
                continue

            if group_seen and token.ttype is Keyword and token.value.upper() == 'BY':
                by_seen = True
                continue

            if by_seen:
                if token.ttype is Keyword and token.value.upper() in ['HAVING', 'ORDER', 'LIMIT', 'OFFSET', 'UNION',
                                                                      'EXCEPT', 'INTERSECT']:
                    break
                if not token.is_whitespace:
                    group_part.append(token)

        return group_part

    def extract_group_by_fields(self):
        """提取GROUP BY字段"""
        fields = []
        sql_upper = self.sql.upper()

        # 查找GROUP BY的位置
        group_by_pos = sql_upper.find('GROUP BY')
        if group_by_pos == -1:
            return fields

        # 找到GROUP BY之后的内容
        after_group_by = self.sql[group_by_pos + 8:].strip()

        # 找到下一个关键字的位置
        next_keywords = ['HAVING', 'ORDER BY', 'LIMIT', 'OFFSET']
        end_pos = len(after_group_by)

        for keyword in next_keywords:
            pos = after_group_by.upper().find(keyword)
            if pos != -1 and pos < end_pos:
                end_pos = pos

        # 提取GROUP BY字段部分
        group_by_part = after_group_by[:end_pos].strip()

        # 解析字段
        if group_by_part:
            # 按逗号分割
            field_list = [f.strip() for f in group_by_part.split(',')]
            fields = [f for f in field_list if f]

        return fields

    def extract_order_by_fields(self):
        """提取ORDER BY字段"""
        fields = []
        sql_upper = self.sql.upper()

        # 查找ORDER BY的位置
        order_by_pos = sql_upper.find('ORDER BY')
        if order_by_pos == -1:
            return fields

        # 找到ORDER BY之后的内容
        after_order_by = self.sql[order_by_pos + 8:].strip()

        # 找到下一个关键字的位置
        next_keywords = ['LIMIT', 'OFFSET', 'FOR UPDATE', 'UNION', 'EXCEPT', 'INTERSECT']
        end_pos = len(after_order_by)

        for keyword in next_keywords:
            pos = after_order_by.upper().find(keyword)
            if pos != -1 and pos < end_pos:
                end_pos = pos

        # 提取ORDER BY字段部分
        order_by_part = after_order_by[:end_pos].strip()

        # 解析字段（保留ASC/DESC）
        if order_by_part:
            # 处理逗号分隔的字段
            current_field = []
            tokens = order_by_part.replace(',', ' , ').split()

            for token in tokens:
                if token == ',':
                    if current_field:
                        fields.append(' '.join(current_field))
                        current_field = []
                else:
                    current_field.append(token)

            # 添加最后一个字段
            if current_field:
                fields.append(' '.join(current_field))

        return fields

    def extract_order_by_fields_with_full_names(self):
        """提取ORDER BY字段并转换为完整的表名.字段名格式"""
        # 确保表别名映射已构建
        self._build_table_alias_map()

        # 获取原始的order by字段
        order_fields = self.extract_order_by_fields()

        # 转换为完整字段名
        full_order_fields = []

        for field_expr in order_fields:
            # 分离字段名和排序方向（ASC/DESC）
            # 使用正则表达式匹配最后的ASC或DESC
            match = re.match(r'^(.+?)\s+(ASC|DESC)\s*$', field_expr, re.IGNORECASE)

            if match:
                field = match.group(1).strip()
                direction = match.group(2).upper()
            else:
                # 没有明确的排序方向，默认为ASC
                field = field_expr.strip()
                direction = None

            # 转换字段为完整格式
            full_field = self._convert_field_to_full_name(field)

            # 重新组合字段和排序方向
            if direction:
                full_field_expr = f"{full_field} {direction}"
            else:
                full_field_expr = full_field

            full_order_fields.append(full_field_expr)

        return full_order_fields

    def extract_order_by_fields_detailed(self):
        """
        提取ORDER BY字段的详细结构

        将字段和排序方向分开存储，如果没有指定排序方向，默认为ASC

        Returns:
            list: 解析后的ORDER BY字段列表，每个元素包含：
                {
                    'field': str,           # 原始字段名
                    'full_field': str,      # 完整的表名.字段名格式
                    'direction': str        # 排序方向 'ASC' 或 'DESC'
                }
        """
        # 确保表别名映射已构建
        self._build_table_alias_map()

        # 获取原始的order by字段
        order_fields = self.extract_order_by_fields()

        # 解析每个字段
        detailed_order_fields = []

        for field_expr in order_fields:
            # 分离字段名和排序方向（ASC/DESC）
            # 使用正则表达式匹配最后的ASC或DESC
            match = re.match(r'^(.+?)\s+(ASC|DESC)\s*$', field_expr, re.IGNORECASE)

            if match:
                field = match.group(1).strip()
                direction = match.group(2).upper()
            else:
                # 没有明确的排序方向，默认为ASC
                field = field_expr.strip()
                direction = 'ASC'

            # 转换字段为完整格式
            full_field = self._convert_field_to_full_name(field)

            # 构建详细信息
            detailed_order_fields.append({
                'field': field,
                'full_field': full_field,
                'direction': direction
            })

        return detailed_order_fields

    def extract_limit(self):
        """提取LIMIT值"""
        limit_value = None
        sql_upper = self.sql.upper()

        # 查找LIMIT的位置
        limit_pos = sql_upper.find('LIMIT')
        if limit_pos == -1:
            return limit_value

        # 找到LIMIT之后的内容
        after_limit = self.sql[limit_pos + 5:].strip()

        # 提取数字
        import re
        match = re.match(r'^\s*(\d+)', after_limit)
        if match:
            limit_value = int(match.group(1))

        return limit_value

    def debug_tokens(self):
        """调试方法：打印所有tokens的详细信息"""
        print("\n调试信息：所有Tokens")
        print("-" * 80)
        for i, token in enumerate(self.parsed.tokens):
            print(f"Token {i}: {repr(str(token).strip())} | Type: {token.ttype} | Is Keyword: {token.ttype is Keyword}")
            if hasattr(token, 'tokens'):
                for j, sub_token in enumerate(token.tokens):
                    print(f"  Sub-token {j}: {repr(str(sub_token).strip())} | Type: {sub_token.ttype}")
        print("-" * 80)

    def parse_all(self):
        """解析SQL的所有部分"""
        return {
            'select_columns': self.extract_select_columns(),
            'select_columns_full': self.extract_select_columns_with_full_names(),  # 完整列名
            'from_tables': self.extract_from_tables(),
            'join_info': self.extract_join_info(),
            'table_alias_mapping': self.extract_table_alias_mapping(),  # 表别名映射
            'where_conditions': self.extract_where_conditions(),
            'where_conditions_detailed': self.extract_where_conditions_detailed(),  # 详细WHERE条件解析
            'group_by_fields': self.extract_group_by_fields(),
            'order_by_fields': self.extract_order_by_fields(),
            'order_by_fields_full': self.extract_order_by_fields_with_full_names(),  # 完整ORDER BY字段
            'order_by_fields_detailed': self.extract_order_by_fields_detailed(),  # 新增：详细ORDER BY解析
            'limit': self.extract_limit()
        }


def format_sql_parts(parts):
    """格式化输出SQL各部分"""
    print("SQL解析结果：")
    print("-" * 50)

    if parts['select_columns']:
        print("SELECT 列（原始）：")
        for col in parts['select_columns']:
            print(f"  - {col}")

    if parts.get('select_columns_full'):
        print("\nSELECT 列（完整表名）：")
        for col in parts['select_columns_full']:
            print(f"  - {col}")

    if parts['from_tables']:
        print("\nFROM 表：")
        for table in parts['from_tables']:
            print(f"  - {table}")

    if parts['join_info']:
        print("\nJOIN 信息：")
        for join in parts['join_info']:
            print(f"  - {join['type']} {join['table']}")
            if join['condition']:
                print(f"    ON {join['condition']}")

    # 显示表别名映射关系
    if parts.get('table_alias_mapping'):
        print("\n表别名映射关系：")
        for alias, table_name in parts['table_alias_mapping'].items():
            if alias == table_name:
                print(f"  - {table_name} (无别名)")
            else:
                print(f"  - {alias} -> {table_name}")

    if parts['where_conditions']:
        print("\nWHERE 条件（原始）：")
        print(f"  {parts['where_conditions']}")

    # 显示详细WHERE条件解析，包含完整字段名
    if parts.get('where_conditions_detailed'):
        where_detail = parts['where_conditions_detailed']
        print("\nWHERE 条件（详细解析）：")
        if where_detail['has_or']:
            print("  包含OR操作符，返回原始条件：")
            print(f"  {where_detail['raw_condition']}")
        elif where_detail['parsed_conditions']:
            print("  解析后的条件：")
            for i, condition in enumerate(where_detail['parsed_conditions'], 1):
                # 对于NULL值，不加引号；对于其他字符串值，加引号
                if condition['value'] == 'NULL':
                    value_str = 'NULL'
                else:
                    value_str = f"'{condition['value']}'"
                print(f"    {i}. 原始字段: {condition['field']}")
                print(f"       完整字段: {condition['full_field']}")
                print(f"       操作符: {condition['operator']}")
                print(f"       值: {value_str}")
        else:
            print("  无WHERE条件")

    if parts['group_by_fields']:
        print("\nGROUP BY 字段：")
        for field in parts['group_by_fields']:
            print(f"  - {field}")

    if parts['order_by_fields']:
        print("\nORDER BY 排序（原始）：")
        for field in parts['order_by_fields']:
            print(f"  - {field}")

    # 新增：显示完整的ORDER BY字段
    if parts.get('order_by_fields_full'):
        print("\nORDER BY 排序（完整表名）：")
        for field in parts['order_by_fields_full']:
            print(f"  - {field}")

    # 新增：显示详细的ORDER BY解析结果
    if parts.get('order_by_fields_detailed'):
        print("\nORDER BY 排序（详细解析）：")
        for i, order_info in enumerate(parts['order_by_fields_detailed'], 1):
            print(f"  {i}. 原始字段: {order_info['field']}")
            print(f"     完整字段: {order_info['full_field']}")
            print(f"     排序方向: {order_info['direction']}")

    if parts['limit'] is not None:
        print(f"\nLIMIT 限制：{parts['limit']}")


# 使用示例和测试
if __name__ == "__main__":
    # 测试原始SQL
    test_sql1 = """SELECT teacher_id, name, title, d.department_name 
    FROM gx_test_teachers  
    LEFT JOIN gx_test_departments d ON department_id = d.department_id 
    WHERE d.department_name = '计算机科学与技术学院' AND title = '副教授'
    ORDER BY d.department_name ASC, teacher_id DESC;"""

    print("测试SQL 1 (带别名的ORDER BY条件):")
    print(test_sql1.strip())
    print("=" * 60)

    parser1 = SQLParser(test_sql1)
    parts1 = parser1.parse_all()
    format_sql_parts(parts1)

    print("\n" + "=" * 80 + "\n")

    # 测试包含复杂ORDER BY的SQL
    test_sql2 = """SELECT u.id, u.name, p.title 
    FROM users u
    JOIN profiles p ON u.id = p.user_id
    WHERE u.status = 'active' 
    ORDER BY u.created_at DESC, p.priority ASC, name;"""

    print("测试SQL 2 (复杂ORDER BY，混合别名和非别名):")
    print(test_sql2.strip())
    print("=" * 60)

    parser2 = SQLParser(test_sql2)
    parts2 = parser2.parse_all()
    format_sql_parts(parts2)

    print("\n" + "=" * 80 + "\n")

    # 测试无别名的ORDER BY
    test_sql3 = """SELECT id, name, email 
    FROM users 
    WHERE status = 'active'
    ORDER BY created_at DESC, name ASC;"""

    print("测试SQL 3 (无别名的ORDER BY):")
    print(test_sql3.strip())
    print("=" * 60)

    parser3 = SQLParser(test_sql3)
    parts3 = parser3.parse_all()
    format_sql_parts(parts3)

    print("\n" + "=" * 80 + "\n")

    # 测试多表但无别名的情况
    test_sql4 = """SELECT * FROM orders, customers 
    WHERE orders.customer_id = customers.id AND status = 'pending' 
    ORDER BY orders.total DESC, customers.name;"""

    print("测试SQL 4 (多表无别名，ORDER BY已包含表名):")
    print(test_sql4.strip())
    print("=" * 60)

    parser4 = SQLParser(test_sql4)
    parts4 = parser4.parse_all()
    format_sql_parts(parts4)

    print("\n" + "=" * 80 + "\n")

    # 单独测试ORDER BY字段详细解析功能
    print("单独测试ORDER BY字段详细解析功能（字段完整名称转换）：")
    print("-" * 50)

    test_order_cases = [
        ("SELECT * FROM users u ORDER BY u.name ASC, age DESC", "带别名和无别名混合"),
        ("SELECT * FROM users u JOIN profiles p ON u.id = p.user_id ORDER BY u.created_at, p.priority DESC",
         "多表JOIN带别名"),
        ("SELECT * FROM products ORDER BY category_id, price DESC", "单表无别名"),
        ("SELECT * FROM orders o ORDER BY o.total DESC", "单表带别名"),
        ("SELECT * FROM users u ORDER BY u.name, u.email ASC, created_at DESC", "混合情况"),
        ("SELECT * FROM users ORDER BY name", "无排序方向（默认ASC）")
    ]

    for i, (sql, desc) in enumerate(test_order_cases, 1):
        print(f"\nORDER BY测试 {i} ({desc}):")
        print(f"SQL: {sql}")
        temp_parser = SQLParser(sql)
        order_fields = temp_parser.extract_order_by_fields()
        order_fields_full = temp_parser.extract_order_by_fields_with_full_names()
        order_fields_detailed = temp_parser.extract_order_by_fields_detailed()

        print(f"  → 原始ORDER BY字段：")
        for field in order_fields:
            print(f"    - {field}")

        print(f"  → 完整ORDER BY字段：")
        for field in order_fields_full:
            print(f"    - {field}")

        print(f"  → 详细ORDER BY解析：")
        for j, detail in enumerate(order_fields_detailed, 1):
            print(f"    {j}. 字段: {detail['field']} → {detail['full_field']}, 方向: {detail['direction']}")

    print("\n" + "=" * 80 + "\n")

    # 原始的WHERE条件测试保留
    print("单独测试WHERE条件详细解析功能（字段完整名称转换）：")
    print("-" * 50)

    test_conditions = [
        ("SELECT * FROM users u WHERE u.name = 'John' AND age > 25", "带别名和无别名混合"),
        ("SELECT * FROM users u JOIN profiles p ON u.id = p.user_id WHERE u.status = 'active' AND p.verified = 1",
         "多表JOIN带别名"),
        ("SELECT * FROM products WHERE category_id = 1 AND price > 100", "单表无别名"),
        ("SELECT * FROM orders o WHERE o.total >= 500 OR o.status = 'urgent'", "OR条件（不解析）")
    ]

    for i, (sql, desc) in enumerate(test_conditions, 1):
        print(f"\n条件 {i} ({desc}):")
        print(f"SQL: {sql}")
        temp_parser = SQLParser(sql)
        where_detail = temp_parser.extract_where_conditions_detailed()

        if where_detail['has_or']:
            print("  → 包含OR，返回原始条件")
        else:
            print(f"  → 解析出 {len(where_detail['parsed_conditions'])} 个条件：")
            for j, cond in enumerate(where_detail['parsed_conditions'], 1):
                if cond['value'] == 'NULL':
                    value_str = 'NULL'
                else:
                    value_str = f"'{cond['value']}'"
                print(f"    {j}. {cond['field']} → {cond['full_field']} {cond['operator']} {value_str}")