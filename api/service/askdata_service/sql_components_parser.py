import re
from typing import Dict, List, Optional, Any


class SQLComponentsParser:
    """SQL组件解析器，用于解析已经分离的SQL各部分"""

    def __init__(self, sql_components: Dict[str, Any]):
        """
        初始化解析器

        Args:
            sql_components: 包含SQL各部分的字典，如：
                {
                    'select': 'SELECT ...',
                    'from': 'FROM ...',
                    'where': 'WHERE ...',
                    'groupBy': 'GROUP BY ...',
                    'having': 'HAVING ...',
                    'orderBy': 'ORDER BY ...',
                    'limit': 'LIMIT ...'
                }
        """
        self.components = sql_components
        self._table_alias_map = {}

    def parse_select_columns(self) -> List[str]:
        """解析SELECT列"""
        select_clause = self.components.get('select', '')
        if not select_clause:
            return []

        # 移除SELECT关键字
        select_content = re.sub(r'^\s*SELECT\s+', '', select_clause, flags=re.IGNORECASE)

        # 处理逗号分隔的列
        columns = []
        # 使用正则表达式来更准确地分割列（考虑函数调用中的逗号）
        parts = self._split_by_comma(select_content)

        for part in parts:
            part = part.strip()
            if part:
                columns.append(part)

        return columns

    def parse_from_tables(self) -> List[Dict[str, str]]:
        """
        解析FROM子句中的所有表

        Returns:
            List[Dict]: 每个表的信息，包含 'table' 和 'alias' 键
        """
        from_clause = self.components.get('from', '')
        if not from_clause:
            return []

        # 移除FROM关键字
        from_content = re.sub(r'^\s*FROM\s+', '', from_clause, flags=re.IGNORECASE)

        tables = []

        # 分割JOIN语句
        # 先处理整个FROM子句，识别所有的表
        parts = re.split(
            r'\s+(?:INNER\s+|LEFT\s+OUTER\s+|RIGHT\s+OUTER\s+|FULL\s+OUTER\s+|LEFT\s+|RIGHT\s+|FULL\s+|CROSS\s+)?JOIN\s+',
            from_content, flags=re.IGNORECASE)

        for i, part in enumerate(parts):
            if i == 0:
                # 第一部分是主表
                table_info = self._extract_table_info(part)
                if table_info:
                    tables.append(table_info)
            else:
                # JOIN的表，需要去掉ON条件
                on_match = re.search(r'\s+ON\s+', part, re.IGNORECASE)
                if on_match:
                    table_part = part[:on_match.start()]
                else:
                    table_part = part

                table_info = self._extract_table_info(table_part)
                if table_info:
                    tables.append(table_info)

        return tables

    def get_table_alias_mapping(self) -> Dict[str, str]:
        """
        获取表别名到真实表名的映射

        Returns:
            Dict[str, str]: {别名: 表名} 的映射
        """
        if not self._table_alias_map:
            tables = self.parse_from_tables()
            for table_info in tables:
                table = table_info['table']
                alias = table_info['alias']
                self._table_alias_map[alias] = table

        return self._table_alias_map.copy()

    def parse_where_conditions(self) -> Dict:
        """
        解析WHERE条件

        Returns:
            Dict: 包含以下结构：
                - 'has_or': bool, 是否包含OR
                - 'raw_condition': str, 原始条件
                - 'parsed_conditions': List[Dict], 解析后的条件（仅当has_or=False时）
        """
        where_clause = self.components.get('where', '')
        if not where_clause:
            return {
                'has_or': False,
                'raw_condition': '',
                'parsed_conditions': []
            }

        # 移除WHERE关键字
        where_content = re.sub(r'^\s*WHERE\s+', '', where_clause, flags=re.IGNORECASE)

        # 检查是否包含OR
        has_or = bool(re.search(r'\bOR\b', where_content, re.IGNORECASE))

        result = {
            'has_or': has_or,
            'raw_condition': where_content,
            'parsed_conditions': []
        }

        # 如果包含OR，直接返回
        if has_or:
            return result

        # 解析AND连接的条件
        and_conditions = re.split(r'\s+AND\s+', where_content, flags=re.IGNORECASE)

        for condition in and_conditions:
            parsed = self._parse_single_condition(condition.strip())
            if parsed:
                result['parsed_conditions'].append(parsed)

        return result

    def parse_having_conditions(self) -> Dict:
        """
        解析HAVING条件

        Returns:
            Dict: 包含以下结构：
                - 'has_or': bool, 是否包含OR
                - 'raw_condition': str, 原始条件
                - 'parsed_conditions': List[Dict], 解析后的条件（仅当has_or=False时）
        """
        having_clause = self.components.get('having', '')
        if not having_clause:
            return {
                'has_or': False,
                'raw_condition': '',
                'parsed_conditions': []
            }

        # 移除HAVING关键字
        having_content = re.sub(r'^\s*HAVING\s+', '', having_clause, flags=re.IGNORECASE)

        # 检查是否包含OR
        has_or = bool(re.search(r'\bOR\b', having_content, re.IGNORECASE))

        result = {
            'has_or': has_or,
            'raw_condition': having_content,
            'parsed_conditions': []
        }

        # 如果包含OR，直接返回
        if has_or:
            return result

        # 解析AND连接的条件
        and_conditions = re.split(r'\s+AND\s+', having_content, flags=re.IGNORECASE)

        for condition in and_conditions:
            parsed = self._parse_single_condition(condition.strip())
            if parsed:
                result['parsed_conditions'].append(parsed)

        return result

    def parse_order_by(self) -> List[Dict[str, str]]:
        """
        解析ORDER BY字段

        Returns:
            List[Dict]: 每个排序字段的信息，包含 'field' 和 'direction' 键
        """
        order_clause = self.components.get('orderBy', '')
        if not order_clause:
            return []

        # 移除ORDER BY关键字
        order_content = re.sub(r'^\s*ORDER\s+BY\s+', '', order_clause, flags=re.IGNORECASE)

        if not order_content.strip():
            return []

        order_fields = []

        # 分割逗号分隔的字段
        parts = self._split_by_comma(order_content)

        for part in parts:
            part = part.strip()
            if not part:
                continue

            # 检查是否有排序方向
            match = re.match(r'^(.+?)\s+(ASC|DESC)\s*$', part, re.IGNORECASE)
            if match:
                field = match.group(1).strip()
                direction = match.group(2).upper()
            else:
                field = part
                direction = 'ASC'  # 默认升序

            order_fields.append({
                'field': field,
                'direction': direction
            })

        return order_fields

    def parse_group_by(self) -> List[str]:
        """解析GROUP BY字段"""
        group_clause = self.components.get('groupBy', '')
        if not group_clause:
            return []

        # 移除GROUP BY关键字
        group_content = re.sub(r'^\s*GROUP\s+BY\s+', '', group_clause, flags=re.IGNORECASE)

        if not group_content.strip():
            return []

        # 分割逗号分隔的字段
        fields = [f.strip() for f in group_content.split(',') if f.strip()]

        return fields

    def parse_limit(self) -> Optional[int]:
        """解析LIMIT值"""
        limit_clause = self.components.get('limit', '')
        if not limit_clause:
            return None

        # 提取数字
        match = re.search(r'\b(\d+)\b', limit_clause)
        if match:
            return int(match.group(1))

        return None

    def parse_all(self) -> Dict:
        """解析所有组件"""
        return {
            'select_columns': self.parse_select_columns(),
            'from_tables': self.parse_from_tables(),
            'table_alias_mapping': self.get_table_alias_mapping(),
            'where_conditions': self.parse_where_conditions(),
            'group_by': self.parse_group_by(),
            'having_conditions': self.parse_having_conditions(),
            'order_by': self.parse_order_by(),
            'limit': self.parse_limit()
        }

    # 辅助方法
    def _split_by_comma(self, text: str) -> List[str]:
        """智能分割逗号分隔的内容（考虑括号内的逗号）"""
        parts = []
        current = []
        paren_level = 0

        for char in text:
            if char == '(':
                paren_level += 1
            elif char == ')':
                paren_level -= 1
            elif char == ',' and paren_level == 0:
                parts.append(''.join(current))
                current = []
                continue

            current.append(char)

        if current:
            parts.append(''.join(current))

        return parts

    def _extract_table_info(self, table_expr: str) -> Optional[Dict[str, str]]:
        """从表表达式中提取表名和别名"""
        table_expr = table_expr.strip()
        if not table_expr:
            return None

        # 匹配 table AS alias 或 table alias
        as_match = re.match(r'(\S+)\s+AS\s+(\S+)', table_expr, re.IGNORECASE)
        if as_match:
            return {
                'table': as_match.group(1),
                'alias': as_match.group(2)
            }

        # 匹配 table alias (没有AS)
        space_match = re.match(r'(\S+)\s+(\S+)', table_expr)
        if space_match:
            # 确保第二部分不是SQL关键字
            potential_alias = space_match.group(2)
            if not re.match(r'^(ON|WHERE|GROUP|ORDER|HAVING|LIMIT)$', potential_alias, re.IGNORECASE):
                return {
                    'table': space_match.group(1),
                    'alias': potential_alias
                }

        # 只有表名，没有别名
        return {
            'table': table_expr,
            'alias': table_expr  # 别名与表名相同
        }

    def _parse_single_condition(self, condition: str) -> Optional[Dict[str, str]]:
        """解析单个WHERE或HAVING条件"""
        condition = condition.strip()
        if not condition:
            return None

        # 定义操作符模式
        operators = [
            (r'\s+IS\s+NOT\s+NULL\b', 'IS NOT NULL'),
            (r'\s+IS\s+NULL\b', 'IS NULL'),
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

        for pattern, op in operators:
            match = re.search(pattern, condition, re.IGNORECASE)
            if match:
                field = condition[:match.start()].strip()

                # 处理NULL操作符（这些操作符没有值）
                if op in ['IS NULL', 'IS NOT NULL']:
                    return {
                        'field': field,
                        'operator': op,
                        'value': None  # NULL操作符没有值
                    }

                # 处理其他操作符
                value = condition[match.end():].strip()

                # 去除引号
                if value:
                    if (value.startswith("'") and value.endswith("'")) or \
                            (value.startswith('"') and value.endswith('"')):
                        value = value[1:-1]

                return {
                    'field': field,
                    'operator': op,
                    'value': value
                }

        return None


def format_parsed_components(parsed: Dict) -> None:
    """格式化输出解析结果"""
    print("SQL组件解析结果：")
    print("=" * 60)

    # SELECT列
    if parsed['select_columns']:
        print("\n1. SELECT列：")
        for i, col in enumerate(parsed['select_columns'], 1):
            print(f"   {i}. {col}")

    # FROM表
    if parsed['from_tables']:
        print("\n2. FROM表：")
        for i, table in enumerate(parsed['from_tables'], 1):
            if table['table'] == table['alias']:
                print(f"   {i}. {table['table']} (无别名)")
            else:
                print(f"   {i}. {table['table']} AS {table['alias']}")

    # 表别名映射
    if parsed['table_alias_mapping']:
        print("\n3. 表别名映射：")
        for alias, table in parsed['table_alias_mapping'].items():
            if alias != table:
                print(f"   {alias} -> {table}")

    # WHERE条件
    where = parsed['where_conditions']
    if where['raw_condition']:
        print("\n4. WHERE条件：")
        if where['has_or']:
            print(f"   包含OR操作符，原始条件：")
            print(f"   {where['raw_condition']}")
        else:
            print(f"   解析后的条件（AND连接）：")
            for i, cond in enumerate(where['parsed_conditions'], 1):
                if cond['value'] is None:  # NULL操作符没有值
                    print(f"   {i}. {cond['field']} {cond['operator']}")
                else:
                    value_str = f"'{cond['value']}'"
                    print(f"   {i}. {cond['field']} {cond['operator']} {value_str}")

    # GROUP BY
    if parsed['group_by']:
        print("\n5. GROUP BY：")
        for i, field in enumerate(parsed['group_by'], 1):
            print(f"   {i}. {field}")

    # HAVING条件
    having = parsed['having_conditions']
    if having['raw_condition']:
        print("\n6. HAVING条件：")
        if having['has_or']:
            print(f"   包含OR操作符，原始条件：")
            print(f"   {having['raw_condition']}")
        else:
            print(f"   解析后的条件（AND连接）：")
            for i, cond in enumerate(having['parsed_conditions'], 1):
                if cond['value'] is None:  # NULL操作符没有值
                    print(f"   {i}. {cond['field']} {cond['operator']}")
                else:
                    value_str = f"'{cond['value']}'"
                    print(f"   {i}. {cond['field']} {cond['operator']} {value_str}")

    # ORDER BY
    if parsed['order_by']:
        print("\n7. ORDER BY：")
        for i, order in enumerate(parsed['order_by'], 1):
            print(f"   {i}. {order['field']} {order['direction']}")

    # LIMIT
    if parsed['limit'] is not None:
        print(f"\n8. LIMIT: {parsed['limit']}")


# 使用示例
if __name__ == "__main__":
    # 测试用例1：包含HAVING子句的查询
    sql_components1 = {
        'select': 'SELECT department_id, COUNT(*) as emp_count, AVG(salary) as avg_salary',
        'from': 'FROM employees e JOIN departments d ON e.dept_id = d.id',
        'where': 'WHERE e.status = "active" AND e.join_date > "2020-01-01"',
        'groupBy': 'GROUP BY department_id',
        'having': 'HAVING COUNT(*) > 5 AND AVG(salary) > 50000',
        'orderBy': 'ORDER BY avg_salary DESC',
        'limit': 'LIMIT 10'
    }

    print("测试用例 1 (包含HAVING子句):")
    print("-" * 60)
    parser1 = SQLComponentsParser(sql_components1)
    result1 = parser1.parse_all()
    format_parsed_components(result1)

    # 测试用例2：HAVING子句包含OR条件
    sql_components2 = {
        'select': 'SELECT category_id, SUM(revenue) as total_revenue, COUNT(*) as product_count',
        'from': 'FROM products p',
        'where': 'WHERE p.active = 1',
        'groupBy': 'GROUP BY category_id',
        'having': 'HAVING SUM(revenue) > 100000 OR COUNT(*) > 50 OR AVG(price) < 10',
        'orderBy': 'ORDER BY total_revenue DESC',
        'limit': ''
    }

    print("\n\n测试用例 2 (HAVING包含OR条件):")
    print("-" * 60)
    parser2 = SQLComponentsParser(sql_components2)
    result2 = parser2.parse_all()
    format_parsed_components(result2)

    # 测试用例3：复杂的HAVING条件（包含函数和多个条件）
    sql_components3 = {
        'select': 'SELECT store_id, YEAR(sale_date) as sale_year, SUM(amount) as total_sales, MAX(amount) as max_sale',
        'from': 'FROM sales s INNER JOIN stores st ON s.store_id = st.id',
        'where': 'WHERE st.region = "North" AND s.sale_date >= "2023-01-01"',
        'groupBy': 'GROUP BY store_id, YEAR(sale_date)',
        'having': 'HAVING SUM(amount) > 50000 AND MAX(amount) > 1000 AND COUNT(*) >= 100',
        'orderBy': 'ORDER BY sale_year DESC, total_sales DESC',
        'limit': 'LIMIT 20'
    }

    print("\n\n测试用例 3 (复杂HAVING条件):")
    print("-" * 60)
    parser3 = SQLComponentsParser(sql_components3)
    result3 = parser3.parse_all()
    format_parsed_components(result3)

    # 测试用例4：HAVING子句中包含NULL条件
    sql_components4 = {
        'select': 'SELECT manager_id, COUNT(*) as employee_count, AVG(performance_score) as avg_performance',
        'from': 'FROM employees e',
        'where': 'WHERE e.department_id = 10',
        'groupBy': 'GROUP BY manager_id',
        'having': 'HAVING AVG(performance_score) IS NOT NULL AND COUNT(*) > 3',
        'orderBy': 'ORDER BY avg_performance DESC',
        'limit': ''
    }

    print("\n\n测试用例 4 (HAVING包含NULL条件):")
    print("-" * 60)
    parser4 = SQLComponentsParser(sql_components4)
    result4 = parser4.parse_all()
    format_parsed_components(result4)

    # 测试用例5：没有HAVING子句的查询（向后兼容）
    sql_components5 = {
        'from': 'FROM gx_test_teachers AS t1 JOIN gx_test_teacher_performance AS t2 ON t1.teacher_id = t2.teacher_id',
        'groupBy': '',
        'limit': '',
        'orderBy': '',
        'select': 'SELECT t1.teacher_id, t1.name, t2.total_score',
        'where': "WHERE t2.evaluation_year = 2023 AND t2.total_score > 82"
    }

    print("\n\n测试用例 5 (没有HAVING子句，向后兼容):")
    print("-" * 60)
    parser5 = SQLComponentsParser(sql_components5)
    result5 = parser5.parse_all()
    format_parsed_components(result5)

    # 测试用例6：HAVING子句包含LIKE操作符
    sql_components6 = {
        'select': 'SELECT customer_name, GROUP_CONCAT(product_name) as products',
        'from': 'FROM orders o JOIN customers c ON o.customer_id = c.id',
        'where': 'WHERE o.order_date > "2023-01-01"',
        'groupBy': 'GROUP BY customer_name',
        'having': 'HAVING GROUP_CONCAT(product_name) LIKE "%laptop%" AND COUNT(*) > 2',
        'orderBy': 'ORDER BY customer_name',
        'limit': 'LIMIT 50'
    }

    print("\n\n测试用例 6 (HAVING包含LIKE操作符):")
    print("-" * 60)
    parser6 = SQLComponentsParser(sql_components6)
    result6 = parser6.parse_all()
    format_parsed_components(result6)

    # 测试用例7：HAVING子句包含IN操作符
    sql_components7 = {
        'select': 'SELECT region, COUNT(DISTINCT customer_id) as customer_count',
        'from': 'FROM sales s',
        'where': 'WHERE s.year = 2023',
        'groupBy': 'GROUP BY region',
        'having': 'HAVING COUNT(DISTINCT customer_id) IN (100, 200, 300) AND SUM(amount) > 10000',
        'orderBy': 'ORDER BY customer_count DESC',
        'limit': ''
    }

    print("\n\n测试用例 7 (HAVING包含IN操作符):")
    print("-" * 60)
    parser7 = SQLComponentsParser(sql_components7)
    result7 = parser7.parse_all()
    format_parsed_components(result7)