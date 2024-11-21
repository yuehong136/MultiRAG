import re
import ast
from typing import Any


def extract_brackets(text: str, merge: bool = True) -> list[Any]:
    """
    从文本中提取所有被中括号[]包围的内容，并将其解析为Python列表

    Args:
        text (str): 输入文本
        merge (bool): 是否合并所有括号中的内容并去重，默认为True

    Returns:
        list: 解析后的列表。如果merge=True，返回一个合并后的去重列表；
             如果merge=False，返回包含所有括号内容的列表的列表
    """
    # 预处理：去除多余的空白字符
    text = text.strip()

    def flatten(lst):
        """递归展平嵌套列表"""
        for item in lst:
            if isinstance(item, list):
                yield from flatten(item)
            else:
                yield item

    def try_parse(s):
        """尝试解析字符串为Python对象"""
        try:
            parsed = ast.literal_eval(s)
            return parsed if isinstance(parsed, list) else None
        except (ValueError, SyntaxError):
            return None

    # 首先尝试直接解析整个文本
    parsed = try_parse(text)
    if parsed is not None:
        if merge:
            return list(dict.fromkeys(flatten(parsed)))
        return [parsed]

    # 如果直接解析失败，使用正则表达式查找所有可能的列表
    # 使用非贪婪匹配并处理嵌套的括号
    results = []
    pattern = r'\[((?:[^\[\]]|\[(?:[^\[\]]|\[[^\[\]]*\])*\])*)\]'
    matches = re.finditer(pattern, text)

    for match in matches:
        found_text = match.group(0)
        parsed = try_parse(found_text)
        if parsed is not None:
            if merge:
                results.extend(flatten(parsed))
            else:
                results.append(parsed)

    if merge:
        return list(dict.fromkeys(results))
    return results


if __name__ == "__main__":
    # 测试用例1：嵌套列表
    text1 = '[["柱状图", "饼图"]]'
    print("\n测试用例1 - 嵌套列表:")
    print("原始文本:", text1)
    print("合并去重结果:", extract_brackets(text1))
    print("不合并结果:", extract_brackets(text1, merge=False))

    # 测试用例2：混合类型
    text2 = "这里有一些数据 ['a', 'b', 'c'] 和更多数据 [1, 2, 3]"
    print("\n测试用例2 - 混合类型:")
    print("原始文本:", text2)
    print("合并去重结果:", extract_brackets(text2))
    print("不合并结果:", extract_brackets(text2, merge=False))

    # 测试用例3：多层嵌套列表
    text3 = "数据 [['a', 'b'], ['c', 'd']] 更多 [1, [2, 3]]"
    print("\n测试用例3 - 多层嵌套列表:")
    print("原始文本:", text3)
    print("合并去重结果:", extract_brackets(text3))
    print("不合并结果:", extract_brackets(text3, merge=False))

    # 测试用例4
    text4 = '''
        [["柱状图", "饼图"]]
    '''
    print("\n测试用例4 - 原始测试用例:")
    print("原始文本:", text4)
    print("合并去重结果:", extract_brackets(text4))
    print("不合并结果:", extract_brackets(text4, merge=False))
