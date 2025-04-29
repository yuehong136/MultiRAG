import jieba
import aiohttp
import asyncio
import os
import tempfile
from typing import List

from api.settings import DCS_SERVER_HOST, DCS_SERVER_PORT, DCS_SERVER_PROTOCOL


async def custom_tokenize_with_semantic_words(text: str, dataset_id_list: List[str]) -> List[str]:
    """
    使用自定义词典对文本进行分词（异步版本）

    参数:
        text: 要分词的文本
        dataset_id_list: 限定数据集范围，可选

    返回:
        分词后的词列表
    """
    # 1. 创建一个独立的Tokenizer实例
    tokenizer = jieba.Tokenizer()

    # 内部方法：从API异步获取自定义词列表
    async def _fetch_custom_words_from_api(dataset_id_list: List[str]) -> List[str]:
        """异步从API获取自定义词列表"""
        api_path = "/api/words"
        api_url = f"{DCS_SERVER_PROTOCOL}://{DCS_SERVER_HOST}:{DCS_SERVER_PORT}{api_path}"

        try:
            # 分页参数
            page_size = 100  # 默认较大的页大小以减少请求次数

            all_words = []
            page_num = 1
            total = None

            # 使用aiohttp进行异步HTTP请求
            async with aiohttp.ClientSession() as session:
                # 循环获取所有页的数据
                while True:
                    # 构造API请求参数
                    payload = {
                        "dataset_id_list": dataset_id_list,
                        "pageSize": page_size,
                        "pageNum": page_num
                    }

                    # 发送异步GET请求
                    async with session.get(api_url, json=payload) as response:
                        response.raise_for_status()
                        result = await response.json()

                        # 检查返回码
                        if result.get("code") != 0:
                            print(f"API返回错误: {result.get('message')}")
                            break

                        # 获取词列表和总数
                        data = result.get("data", {})
                        words = data.get("words", [])
                        all_words.extend(words)

                        # 首次获取总数
                        if total is None:
                            total = data.get("total", 0)

                        # 判断是否已获取全部数据
                        if len(all_words) >= total or not words:
                            break

                        # 下一页
                        page_num += 1

            return all_words
        except Exception as e:
            print(f"从API获取词列表失败: {e}")
            return []  # 失败时返回空列表

    # 内部方法：将词列表写入文件
    def _write_words_to_file(words: List[str]) -> str:
        """将词列表写入文件"""
        if not words:
            return ""

        # 使用tempfile生成唯一的临时文件
        fd, temp_file_path = tempfile.mkstemp(suffix='.txt', prefix='custom_dict_')
        os.close(fd)  # 关闭文件描述符，但保留文件

        try:
            with open(temp_file_path, 'w', encoding='utf-8') as f:
                for word in words:
                    f.write(f"{word}\n")

            return temp_file_path
        except Exception as e:
            print(f"写入词典文件失败: {e}")
            # 如果写入失败，删除临时文件
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            return ""

    # 2. 从API异步获取自定义词列表
    custom_words = await _fetch_custom_words_from_api(dataset_id_list or [])

    # 3. 将词列表写入临时文件
    dict_file_path = _write_words_to_file(custom_words)

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


def custom_tokenize(text: str) -> List[str]:
    """
    直接进行分词，不调用API
    """
    return list(jieba.cut(text))


# 同步包装函数，用于方便调用异步函数
def sync_custom_tokenize_with_semantic_words(text: str, dataset_id_list: List[str]) -> List[str]:
    """
    同步包装器：使用自定义词典对文本进行分词

    参数:
        text: 要分词的文本
        dataset_id_list: 限定数据集范围，可选

    返回:
        分词后的词列表
    """
    return asyncio.run(custom_tokenize_with_semantic_words(text, dataset_id_list))
