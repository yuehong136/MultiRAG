import re

from bs4 import BeautifulSoup
from typing import List, Dict


def detect_multiple_tables(html_content: str) -> bool:
    """
    检测HTML表格中是否存在多个表格

    Args:
        html_content: HTML表格内容字符串

    Returns:
        Tuple[bool, List[int]]:
            - bool: 是否存在多个表格
            - List[int]: 可能的表头行索引列表
    """
    # 使用BeautifulSoup解析HTML
    soup = BeautifulSoup(html_content, 'html.parser')

    # 获取所有tr行
    rows = soup.find_all('tr')

    # 存储可能的表头行索引
    header_indices = []

    # 遍历每一行，检查是否为可能的表头
    for i, row in enumerate(rows):
        cells = row.find_all('td')

        # 检查这一行的所有单元格是否都有内容
        has_content = all(cell.get_text().strip() != '' for cell in cells)

        # 如果全都有内容，可能是表头
        if has_content:
            # 如果不是第一行，且与上一个表头间隔超过1行，认为是新表的表头
            if len(header_indices) > 0 and i - header_indices[-1] > 1:
                header_indices.append(i)
            # 如果是第一行，直接添加
            elif len(header_indices) == 0:
                header_indices.append(i)

    # 判断是否存在多个表格（至少有两个表头，且它们之间有数据行）
    has_multiple_tables = len(header_indices) > 1 and any(
        header_indices[i + 1] - header_indices[i] > 1
        for i in range(len(header_indices) - 1)
    )

    return has_multiple_tables


def identify_single_table_or_inputs_pattern(html_content):
    # 清理HTML中的空白字符，便于处理
    html_content = re.sub(r'\s+', ' ', html_content).strip()

    result = {
        'pattern': None,
        'fields': []
    }

    # 检查是否是表格
    table_pattern = r'<table[^>]*>.*?</table>'
    if not re.search(table_pattern, html_content, re.DOTALL):
        return result

    # 提取所有行
    rows = re.findall(r'<tr>(.*?)</tr>', html_content, re.DOTALL)
    if not rows:
        return result

    # 检查是否是第一种模式（普通表格模式）
    first_row = rows[0]
    header_fields = re.findall(r'<td>(.*?)</td>', first_row)

    # 检查其他行是否都是空td
    other_rows_empty = True
    for row in rows[1:]:
        td_contents = re.findall(r'<td>(.*?)</td>', row)
        if any(content.strip() for content in td_contents):
            other_rows_empty = False
            break

    # 如果满足第一种模式特征
    if header_fields and other_rows_empty:
        result['pattern'] = 'table'
        result['fields'] = header_fields
        return result

    # 检查是否是第二种模式（输入字段模式）
    fields = []
    for row in rows:
        # 提取这一行中的所有单元格内容
        td_contents = re.findall(r'<td>(.*?)</td>', row)

        # 处理这一行的字段
        i = 0
        while i < len(td_contents):
            content = td_contents[i].strip()
            if content:  # 如果不是空单元格
                fields.append(content)
            i += 1

    # 检查是否符合输入字段模式的特征
    # 1. 有非空字段
    # 2. 字段间通常有空单元格
    if fields:
        result['pattern'] = 'inputs'
        result['fields'] = fields

    return result


class MultiTableWithNameExtractor:
    """
    对于那些带有名称的多表，进行信息提取
    """

    def __init__(self, html_content: str):
        self.soup = BeautifulSoup(html_content, 'html.parser')

    def _get_consecutive_rows(self, start_row) -> List[Dict]:
        """获取连续的相同类型的行"""
        rows = []
        current_row = start_row
        first_cell_content = current_row.find('td').text.strip()

        while current_row:
            first_td = current_row.find('td')
            if not first_td:
                break

            current_content = first_td.text.strip()
            # 如果是空的行或者内容不匹配，但前一行是表头行，也继续收集
            if (current_content and current_content != first_cell_content and
                    len(rows) == 1 and self._is_header_row(rows[0])):
                row_data = [td.text.strip() for td in current_row.find_all('td')]
                rows.append({
                    'element': current_row,
                    'data': row_data
                })
                break

            # 如果是空行但前面已经有内容了，就停止
            if not current_content and rows:
                break

            # 如果当前行的第一个单元格内容与首行相同，或者是表头行的情况
            if current_content == first_cell_content or not current_content:
                row_data = [td.text.strip() for td in current_row.find_all('td')]
                rows.append({
                    'element': current_row,
                    'data': row_data
                })
                current_row = current_row.find_next('tr')
            else:
                break

        return rows

    def _is_header_row(self, row: Dict) -> bool:
        """检查是否是表头行（所有单元格内容相同）"""
        cells = [cell for cell in row['data'] if cell]  # 过滤空单元格
        return len(cells) > 1 and all(cell == cells[0] for cell in cells)

    def _is_merged_header_pattern(self, rows: List[Dict]) -> bool:
        """检查是否是合并表头模式（如科研获奖成果表）"""
        return len(rows) >= 2 and self._is_header_row(rows[0])

    def _is_repeated_first_column_pattern(self, rows: List[Dict]) -> bool:
        """检查是否是重复第一列模式（如科研论文表）"""
        if len(rows) < 2:
            return False

        first_cell = rows[0]['data'][0]
        return first_cell and all(row['data'][0] == first_cell for row in rows if any(row['data']))

    def extract_tables(self) -> List[Dict]:
        """提取所有表格结构"""
        tables = []
        current_row = self.soup.find('tr')

        while current_row:
            consecutive_rows = self._get_consecutive_rows(current_row)

            if len(consecutive_rows) > 0:
                if self._is_merged_header_pattern(consecutive_rows):
                    # 合并表头模式
                    table_name = consecutive_rows[0]['data'][0]
                    fields = [f for f in consecutive_rows[1]['data'] if f]  # 移除空字段
                    if table_name and fields:  # 只有当表名和字段都非空时才添加
                        tables.append({
                            'table_name': table_name,
                            'fields': fields,
                            'pattern': 'merged_header'
                        })
                    current_row = consecutive_rows[-1]['element'].find_next('tr')

                elif self._is_repeated_first_column_pattern(consecutive_rows):
                    # 重复第一列模式
                    table_name = consecutive_rows[0]['data'][0]
                    fields = [f for f in consecutive_rows[0]['data'][1:] if f]  # 移除空字段
                    if table_name and fields:  # 只有当表名和字段都非空时才添加
                        tables.append({
                            'table_name': table_name,
                            'fields': fields,
                            'pattern': 'repeated_column'
                        })
                    current_row = consecutive_rows[-1]['element'].find_next('tr')

                else:
                    current_row = current_row.find_next('tr')
            else:
                current_row = current_row.find_next('tr')

        return tables
