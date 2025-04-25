import jieba
import requests
import os
from typing import List, Optional

from api.settings import DCS_SERVER_HOST, DCS_SERVER_PORT


def custom_tokenize(text: str, domains: Optional[List[str]] = None,
                    datasets: Optional[List[str]] = None) -> List[str]:
    """
    使用自定义词典对文本进行分词

    参数:
        text: 要分词的文本
        api_url: 获取自定义词列表的API URL
        domains: 限定主题域范围，可选
        datasets: 限定数据集范围，可选

    返回:
        分词后的词列表
    """
    # 1. 创建一个独立的Tokenizer实例
    tokenizer = jieba.Tokenizer()

    # 2. 从API获取自定义词列表
    custom_words = fetch_custom_words_from_api(
        domains or [],
        datasets or []
    )

    # 3. 将词列表写入临时文件
    dict_file_path = write_words_to_file(custom_words)

    # 4. 加载自定义词典到Tokenizer
    if dict_file_path:
        tokenizer.load_userdict(dict_file_path)

    # 5. 使用自定义Tokenizer进行分词
    words = list(tokenizer.cut(text))

    # 6. 清理临时文件
    try:
        if dict_file_path and os.path.exists(dict_file_path):
            os.remove(dict_file_path)
    except Exception as e:
        print(f"清理临时文件失败: {e}")

    return words


def fetch_custom_words_from_api(domains: List[str], datasets: List[str]) -> List[str]:
    """从API获取自定义词列表"""
    api_path = "/api/keywords"
    api_url = f"http://{DCS_SERVER_HOST}:{DCS_SERVER_PORT}{api_path}"

    try:
        # 分页参数移到函数内部
        page_size = 100  # 默认较大的页大小以减少请求次数

        page_num = 1
        all_keywords = []
        total = None

        # 循环获取所有页的数据
        while True:
            # 构造API请求参数
            payload = {
                "domainList": domains,
                "datasetList": datasets,
                "pageSize": page_size,
                "pageNum": page_num
            }

            # 发送POST请求
            response = requests.get(api_url, json=payload)
            response.raise_for_status()

            # 解析返回数据
            result = response.json()

            # 检查返回码
            if result.get("code") != 0:
                print(f"API返回错误: {result.get('message')}")
                break

            # 获取关键词列表和总数
            data = result.get("data", {})
            keywords = data.get("keywordList", [])
            all_keywords.extend(keywords)

            # 首次获取总数
            if total is None:
                total = data.get("total", 0)

            # 判断是否已获取全部数据
            if len(all_keywords) >= total or not keywords:
                break

            # 下一页
            page_num += 1

        return all_keywords
    except Exception as e:
        print(f"从API获取词列表失败: {e}")
        return []  # 失败时返回空列表


def write_words_to_file(words: List[str]) -> str:
    """将词列表写入文件"""
    if not words:
        return ""

    temp_file_path = "temp_custom_dict.txt"

    try:
        with open(temp_file_path, 'w', encoding='utf-8') as f:
            for word in words:
                f.write(f"{word}\n")

        return temp_file_path
    except Exception as e:
        print(f"写入词典文件失败: {e}")
        return ""


# 示例使用
if __name__ == "__main__":
    text = "计算机科学与技术学院的老师在计算机学院大楼讲课"

    # 使用特定领域
    domains = ["1", "2"]


    # 模拟API响应
    def mock_api_call(url, json_data):
        return {
            "code": 0,
            "message": "成功",
            "data": {
                "total": 2,
                "keywordList": ["计算机学院", "计算机科学与技术学院"]
            }
        }


    # 替换实际API调用为模拟调用(仅用于示例)
    import unittest.mock

    with unittest.mock.patch('requests.post', side_effect=lambda url, json: type('obj', (object,), {
        'json': lambda: mock_api_call(url, json),
        'raise_for_status': lambda: None
    })()):
        result = custom_tokenize(text, domains)
        print("分词结果:", result)
