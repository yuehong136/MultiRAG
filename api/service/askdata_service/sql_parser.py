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
            'table_alias_mapping': self.extract_table_alias_mapping(),  # 新增：表别名映射
            'where_conditions': self.extract_where_conditions(),
            'group_by_fields': self.extract_group_by_fields(),
            'order_by_fields': self.extract_order_by_fields(),
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

    # 新增：显示表别名映射关系
    if parts.get('table_alias_mapping'):
        print("\n表别名映射关系：")
        for alias, table_name in parts['table_alias_mapping'].items():
            if alias == table_name:
                print(f"  - {table_name} (无别名)")
            else:
                print(f"  - {alias} -> {table_name}")

    if parts['where_conditions']:
        print("\nWHERE 条件：")
        print(f"  {parts['where_conditions']}")

    if parts['group_by_fields']:
        print("\nGROUP BY 字段：")
        for field in parts['group_by_fields']:
            print(f"  - {field}")

    if parts['order_by_fields']:
        print("\nORDER BY 排序：")
        for field in parts['order_by_fields']:
            print(f"  - {field}")

    if parts['limit'] is not None:
        print(f"\nLIMIT 限制：{parts['limit']}")


# 使用示例
if __name__ == "__main__":
    # 测试你提供的SQL
    test_sql = """SELECT teacher_id, name, title, d.department_name 
    FROM gx_test_teachers  
    LEFT JOIN gx_test_departments d ON department_id = d.department_id 
    WHERE d.department_name = '计算机科学与技术学院' AND title = '副教授';"""

    print("测试SQL:")
    print(test_sql.strip())
    print("=" * 60)

    parser = SQLParser(test_sql)
    parts = parser.parse_all()
    format_sql_parts(parts)

    print("\n" * 2)
    print("解析结果字典：")
    print(parts)

    print("\n" + "=" * 60)
    print("单独测试表别名映射功能：")
    alias_mapping = parser.extract_table_alias_mapping()
    print("表别名映射字典：")
    for alias, table in alias_mapping.items():
        print(f"  '{alias}' -> '{table}'")