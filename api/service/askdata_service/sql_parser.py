import sqlparse
from sqlparse.sql import IdentifierList, Identifier, Where, Token, TokenList
from sqlparse.tokens import Keyword, DML, Punctuation
import re


class SQLParser:
    """SQL解析器，用于提取SQL语句的各个部分"""

    def __init__(self, sql):
        self.sql = sql
        self.parsed = sqlparse.parse(sql)[0]

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
        """提取LIMIT值"""
        limit_value = None
        limit_seen = False

        for token in self.parsed.tokens:
            if limit_seen:
                if token.ttype is None and not token.is_whitespace:
                    try:
                        limit_value = int(str(token).strip())
                        break
                    except ValueError:
                        pass
            elif token.ttype is Keyword and token.value.upper() == 'LIMIT':
                limit_seen = True

        return limit_value

    def parse_all(self):
        """解析SQL的所有部分"""
        return {
            'select_columns': self.extract_select_columns(),
            'from_tables': self.extract_from_tables(),
            'join_info': self.extract_join_info(),
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
        print("SELECT 列：")
        for col in parts['select_columns']:
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
    # 测试SQL语句
    test_sqls = [
        """
        SELECT id, name, age, department
        FROM employees
        WHERE age > 25 AND department = 'IT'
        GROUP BY department, age
        ORDER BY age DESC, name ASC
        LIMIT 10
        """,

        """
        SELECT COUNT(*) as total, AVG(salary) as avg_salary
        FROM employees e
        WHERE e.hire_date > '2020-01-01'
        GROUP BY e.department
        ORDER BY total DESC
        LIMIT 5
        """,

        """
        SELECT *
        FROM users, orders
        WHERE users.id = orders.user_id
        ORDER BY orders.created_at
        """,

        """
        SELECT DISTINCT customer_name, SUM(amount) as total_amount
        FROM sales
        WHERE date BETWEEN '2023-01-01' AND '2023-12-31'
        GROUP BY customer_name
        HAVING SUM(amount) > 1000
        ORDER BY total_amount DESC
        LIMIT 20
        """,

        # 新增JOIN测试用例
        """
        SELECT e.name, e.salary, d.department_name
        FROM employees e
        INNER JOIN departments d ON e.department_id = d.id
        WHERE e.salary > 50000
        ORDER BY e.salary DESC
        """,

        """
        SELECT o.order_id, o.order_date, c.customer_name, p.product_name, oi.quantity
        FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.id
        LEFT JOIN order_items oi ON o.order_id = oi.order_id
        INNER JOIN products p ON oi.product_id = p.id
        WHERE o.order_date >= '2023-01-01'
        ORDER BY o.order_date DESC
        LIMIT 100
        """,

        """
        SELECT e1.name as employee_name, e2.name as manager_name
        FROM employees e1
        LEFT JOIN employees e2 ON e1.manager_id = e2.employee_id
        WHERE e1.department = 'Sales'
        """,

        """
        SELECT s.store_name, p.product_name, i.quantity
        FROM stores s
        CROSS JOIN products p
        LEFT JOIN inventory i ON s.store_id = i.store_id AND p.product_id = i.product_id
        ORDER BY s.store_name, p.product_name
        """,

        """
        SELECT DISTINCT c.country, COUNT(o.order_id) as order_count
        FROM customers c
        RIGHT JOIN orders o ON c.customer_id = o.customer_id
        WHERE o.status = 'completed'
        GROUP BY c.country
        HAVING COUNT(o.order_id) > 10
        ORDER BY order_count DESC
        """
    ]

    for i, sql in enumerate(test_sqls, 1):
        print(f"\n{'=' * 60}")
        print(f"测试SQL {i}:")
        print(sql.strip())
        print(f"{'=' * 60}")

        # 添加调试输出
        parser = SQLParser(sql)
        # parser.debug_tokens()  # 取消注释以查看token详情
        parts = parser.parse_all()
        format_sql_parts(parts)