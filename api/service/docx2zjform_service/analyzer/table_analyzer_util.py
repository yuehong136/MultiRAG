import re

from bs4 import BeautifulSoup
from typing import List, Dict, Optional


def is_multiple_tables_with_name(html_content: str) -> bool:
    """
    是否为含有名称的多表格

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


def is_single_normal_table(html_content):
    """
    检查HTML表格是否符合特定特征：
    1. 第一行所有td单元格都有内容
    2. 其他行的td单元格都为空

    Parameters:
    html_content (str): 包含表格的HTML字符串

    Returns:
    bool: 如果表格符合特征返回True，否则返回False
    """
    try:
        # 解析HTML
        soup = BeautifulSoup(html_content, 'html.parser')

        # 找到表格
        table = soup.find('table')
        if not table:
            return False

        # 获取所有行
        rows = table.find_all('tr')
        if not rows:
            return False

        # 检查第一行：所有td都应该有内容
        first_row = rows[0]
        first_row_cells = first_row.find_all('td')
        if not first_row_cells:
            return False

        # 检查第一行的所有单元格是否都有内容
        if any(not cell.get_text().strip() for cell in first_row_cells):
            return False

        # 检查其他行：所有td都应该为空
        for row in rows[1:]:
            cells = row.find_all('td')
            if not cells:
                continue

            # 如果任何单元格含有非空内容，返回False
            if any(cell.get_text().strip() for cell in cells):
                return False

        return True

    except Exception as e:
        print(f"解析出错: {str(e)}")
        return False


def is_inputs_table(html_content):
    """
    检查HTML表格是否为表单类型（包含待填写项）

    参数:
    html_content (str): HTML表格内容

    返回:
    bool: 如果是表单类型表格返回True，否则返回False
    """
    try:
        # 解析HTML
        soup = BeautifulSoup(html_content, 'html.parser')

        # 获取所有表格行
        rows = soup.find_all('tr')

        # 检查是否至少有一行符合特征
        for row in rows:
            # 获取行内所有单元格
            cells = row.find_all('td')

            # 跳过空行
            if not cells:
                continue

            # 遍历单元格，检查是否存在"有内容td后接空td"的模式
            for i in range(len(cells) - 1):
                current_cell = cells[i]
                next_cell = cells[i + 1]

                # 检查当前单元格是否有内容（排除空白字符）
                current_content = re.sub(r'\s+', '', current_cell.get_text())
                if current_content:
                    # 检查下一个单元格是否为空（排除空白字符）
                    next_content = re.sub(r'\s+', '', next_cell.get_text())
                    if not next_content:
                        return True

        return False

    except Exception as e:
        print(f"解析出错: {str(e)}")
        return False


def is_single_empty_table_with_multiple_br(html):
    # 移除所有引号和转义字符，规范化HTML
    html = html.strip().strip("'\"").replace('\\n', '').replace('\\t', '')

    # 使用更简单的模式来匹配td内容
    td_pattern = r'<td[^>]*>(.*?)</td>'
    td_match = re.search(td_pattern, html, re.DOTALL)

    if td_match:
        content = td_match.group(1)
        # 移除所有<br/>标签和空白字符
        content_clean = re.sub(r'<br\s*/?\s*>', '', content)
        content_clean = re.sub(r'\s', '', content_clean)
        return content_clean == ''

    return False


def is_one_column_multiple_rows_table(html_content: str) -> bool:
    """
    验证HTML表格是否满足以下条件：
    1. 只有一列
    2. 有多行
    3. 每行都包含非空内容

    参数：
        html_content (str): 包含表格的HTML字符串

    返回：
        bool: 如果表格符合条件返回True，否则返回False
    """
    try:
        # 解析HTML内容
        soup = BeautifulSoup(html_content, 'html.parser')

        # 查找表格
        table = soup.find('table')
        if not table:
            return False

        # 获取所有行
        rows = table.find_all('tr')
        if len(rows) <= 1:  # 必须有多行
            return False

        # 检查每一行
        for row in rows:
            # 获取行中的所有单元格
            cells = row.find_all('td')

            # 检查行是否只有一个单元格
            if len(cells) != 1:
                return False

            # 检查单元格内容是否非空
            # 移除空白字符和换行符后检查
            cell_content = cells[0].get_text().strip()
            if not cell_content:
                return False

            # 额外检查：每个单元格应该包含中文字符
            # 这是针对提供的示例的特定要求
            if not re.search('[\u4e00-\u9fff]', cell_content):
                return False

    except Exception as e:
        print(f"处理HTML时发生错误：{str(e)}")
        return False

    return True


def extract_form_inputs_table(html_content: str) -> List[str]:
    """
    提取表格中的所有待填项标签名称

    参数:
    html_content (str): HTML表格内容

    返回:
    List[str]: 待填项标签名称列表
    """
    try:
        # 解析HTML
        soup = BeautifulSoup(html_content, 'html.parser')
        form_fields = []

        # 获取所有表格行
        rows = soup.find_all('tr')

        for row in rows:
            cells = row.find_all('td')
            current_label = None

            for cell in cells:
                content = cell.get_text().strip()

                # 如果单元格有内容，可能是标签名
                if content:
                    current_label = content
                    continue

                # 如果是空单元格且有前导标签，说明是待填项
                if not content and current_label:
                    # 检查该标签是否已经添加（处理合并单元格的情况）
                    if current_label not in form_fields:
                        form_fields.append(current_label)

        return form_fields

    except Exception as e:
        print(f"解析出错: {str(e)}")
        return []


def extract_table_headers(html_content: str) -> Optional[List[str]]:
    """
    从HTML表格中提取第一行的字段值

    Parameters:
    html_content (str): 包含表格的HTML字符串

    Returns:
    Optional[List[str]]: 包含表格字段的列表，如果解析失败则返回None
    """
    try:
        # 解析HTML
        soup = BeautifulSoup(html_content, 'html.parser')

        # 找到表格
        table = soup.find('table')
        if not table:
            return None

        # 获取第一行
        first_row = table.find('tr')
        if not first_row:
            return None

        # 提取所有单元格的文本
        headers = [cell.get_text().strip() for cell in first_row.find_all('td')]

        # 确保所有单元格都有内容
        if all(headers) and len(headers) > 0:
            return headers
        return None

    except Exception as e:
        print(f"解析出错: {str(e)}")
        return None


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
