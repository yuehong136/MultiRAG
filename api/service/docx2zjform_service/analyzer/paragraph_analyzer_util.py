import re


def identify_input(text: str) -> list:
    """
    提取字符串中的待填项字段（以冒号结尾的部分）

    Args:
        text (str): 输入的字符串

    Returns:
        list: 待填项字段列表
    """

    # 修改正则表达式模式：
    # ([^：:]+) 匹配除冒号外的任何字符（一个或多个）
    # (?:：|:) 匹配中文或英文冒号（非捕获组）
    pattern = r"([^：:]+)(?:：|:)"

    # 查找所有匹配项
    matches = re.findall(pattern, text)

    # 处理匹配结果：去除首尾空格
    return [match.strip() for match in matches]


def extract_br_separated_content(html_text):
    """
    从HTML文本中提取被<br/>标签分隔的内容。

    参数:
        html_text (str): 包含<br/>标签的HTML文本

    返回:
        list: 从<br/>标签之间提取的非空字符串列表
    """
    # 将所有<br>标签的变体替换为标准形式
    standardized_text = re.sub(r"<br\s*/?>", "<br/>", html_text)

    # 按<br/>标签拆分文本
    parts = re.split(r"<br/>", standardized_text)

    # 清理每个部分（去除空白）并过滤掉空字符串
    result = [part.strip() for part in parts if part.strip()]

    return result


def split_lines(content):
    """
    将字符串按换行符分割并去除空行

    Args:
        content (str): 输入的字符串内容

    Returns:
        list: 分割后的非空行列表
    """
    if not content:
        return []

    # 使用splitlines()分割并过滤空行
    return [line for line in content.splitlines() if line.strip()]
