import jieba
import aiohttp
import os
import logging
import time
import hashlib
import hmac
from typing import List, Set, Optional, Dict
from pathlib import Path

from api.settings import DCS_SERVER_HOST, DCS_SERVER_PORT, DCS_SERVER_PROTOCOL, DCS_SEMANTIC_SERVER_ACCESS_KEY, \
    DCS_SEMANTIC_SERVER_SECRET_KEY

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("tokenizer")

CURRENT_DIR = Path(os.path.dirname(__file__))


def _generate_signature() -> Dict[str, str]:
    """生成API请求签名"""
    timestamp = str(int(time.time()))
    access_key = DCS_SEMANTIC_SERVER_ACCESS_KEY
    secret_key = DCS_SEMANTIC_SERVER_SECRET_KEY

    if not access_key or not secret_key:
        return {}

    # Using HMAC-SHA256 for the signature
    message = f"{access_key}{timestamp}".encode('utf-8')
    secret = secret_key.encode('utf-8')

    signature = hmac.new(secret, message, digestmod=hashlib.sha256).hexdigest()

    return {
        "accessKey": access_key,
        "timestamp": timestamp,
        "signature": signature
    }


def load_stopwords(file_path: Optional[str] = None) -> Set[str]:
    """
    加载停用词列表

    参数:
        file_path: 停用词文件路径，默认为None，此时使用代码所在目录下的stopwords.txt

    返回:
        停用词集合
    """
    stopwords = set()

    # 如果未指定文件路径，则使用代码所在目录下的stopwords.txt
    if file_path is None:
        file_path = CURRENT_DIR / "stopwords.txt"

    try:
        logger.info(f"尝试从 {file_path} 加载停用词")
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                word = line.strip()
                if word:
                    stopwords.add(word)
        logger.info(f"成功加载停用词 {len(stopwords)} 个")
        return stopwords
    except Exception as e:
        logger.error(f"加载停用词失败: {e}")
        return set()


async def custom_tokenize_with_semantic_words(text: str, dataset_id_list: List[str], remove_stopwords: bool = True,
                                              stopwords_path: str = None) -> List[str]:
    """
    使用自定义词典对文本进行分词（异步版本）

    参数:
        text: 要分词的文本
        dataset_id_list: 限定数据集范围，可选
        remove_stopwords: 是否去除停用词，默认为True
        stopwords_path: 停用词文件路径，默认为None，此时使用代码所在目录下的stopwords.txt

    返回:
        分词后的词列表（已去除停用词和重复词）
    """
    # 1. 创建一个独立的Tokenizer实例
    tokenizer = jieba.Tokenizer()

    # 内部方法：从API异步获取自定义词列表
    async def _fetch_custom_words_from_api(dataset_id_list: List[str]) -> List[str]:
        """异步从API获取自定义词列表"""
        api_path = "/api/drm/semanticOpenApi/getSemanticWords"
        api_url = f"{DCS_SERVER_PROTOCOL}://{DCS_SERVER_HOST}:{DCS_SERVER_PORT}{api_path}"

        try:
            # 分页参数
            page_size = 1000  # 增大页大小以减少请求次数

            all_words = []
            page_num = 1
            total = None

            logger.info(f"开始从API获取自定义词表, 数据集ID: {dataset_id_list}")

            # 使用aiohttp进行异步HTTP请求
            async with aiohttp.ClientSession() as session:
                # 循环获取所有页的数据
                while True:
                    # 构造API请求参数
                    params = {
                        "pi": page_num,
                        "ps": page_size
                    }

                    # 构造请求体
                    payload = {
                        "datasetIds": dataset_id_list
                    }

                    # 构造请求头
                    headers = {
                        "Content-Type": "application/json"
                    }
                    headers.update(_generate_signature())

                    # 发送异步POST请求
                    async with session.post(api_url, params=params, json=payload, headers=headers) as response:
                        response.raise_for_status()
                        result = await response.json()

                        # 检查返回码
                        if result.get("code") != "0":
                            logger.error(f"API返回错误: {result.get('msg')}")
                            break

                        # 获取词列表和总数
                        data = result.get("data", {})
                        words = data.get("rows", [])
                        all_words.extend(words)

                        # 首次获取总数
                        if total is None:
                            total = int(data.get("total", 0))

                        # 判断是否已获取全部数据
                        if len(all_words) >= total or not words:
                            break

                        # 下一页
                        page_num += 1

            logger.info(f"自定义词表获取完成, 共 {len(all_words)} 个词")

            return all_words
        except Exception as e:
            logger.error(f"从API获取词列表失败: {e}")
            return []  # 失败时返回空列表

    # 2. 从API异步获取自定义词列表
    custom_words = await _fetch_custom_words_from_api(dataset_id_list or [])

    # 创建自定义词集合，用于快速查找
    custom_word_set = set(custom_words)

    # 3. 直接将自定义词添加到tokenizer（无需写入文件）
    if custom_words:
        logger.info(f"正在将 {len(custom_words)} 个自定义词添加到分词器...")
        for word in custom_words:
            tokenizer.add_word(word)
        logger.info("自定义词添加完成")
    else:
        logger.warning("没有自定义词可添加")

    # 4. 使用自定义Tokenizer进行分词
    logger.info(f"原始文本: {text[:100]}..." if len(text) > 100 else f"原始文本: {text}")
    words = list(tokenizer.cut(text, cut_all=True))
    logger.info(f"分词结果(停用词过滤前): {len(words)} 个词")
    if len(words) <= 20:  # 如果词数量较少，则全部打印
        logger.info(f"分词结果: {words}")
    else:  # 否则只打印部分
        logger.info(f"分词结果(前20个): {words[:20]}...")

    # 记录被自定义词表命中的词
    custom_matches = [word for word in words if word in custom_word_set]
    logger.info(f"自定义词表命中: {len(custom_matches)} 个词")
    if custom_matches:
        if len(custom_matches) <= 20:
            logger.info(f"自定义词表命中的词: {custom_matches}")
        else:
            logger.info(f"自定义词表命中的词(前20个): {custom_matches[:20]}...")

    # 5. 去除停用词（如果需要）
    if remove_stopwords:
        stopwords = load_stopwords(stopwords_path)
        removed_words = [word for word in words if word in stopwords]
        words = [word for word in words if word not in stopwords and word.strip()]

        logger.info(f"停用词过滤: 移除了 {len(removed_words)} 个停用词")
        if removed_words:
            if len(removed_words) <= 20:
                logger.info(f"被移除的停用词: {removed_words}")
            else:
                logger.info(f"被移除的停用词(前20个): {removed_words[:20]}...")

        logger.info(f"最终分词结果(停用词过滤后): {len(words)} 个词")
        if len(words) <= 20:  # 如果词数量较少，则全部打印
            logger.info(f"最终分词结果: {words}")
        else:  # 否则只打印部分
            logger.info(f"最终分词结果(前20个): {words[:20]}...")

        # 记录最终结果中被自定义词表命中的词
        final_custom_matches = [word for word in words if word in custom_word_set]
        logger.info(f"最终结果中自定义词表命中: {len(final_custom_matches)} 个词")
        if final_custom_matches:
            if len(final_custom_matches) <= 20:
                logger.info(f"最终结果中自定义词表命中的词: {final_custom_matches}")
            else:
                logger.info(f"最终结果中自定义词表命中的词(前20个): {final_custom_matches[:20]}...")

    # 6. 去除重复词
    original_count = len(words)
    words = list(set(words))
    duplicates_removed = original_count - len(words)

    logger.info(f"去重过滤: 移除了 {duplicates_removed} 个重复词")
    logger.info(f"最终分词结果(去重后): {len(words)} 个词")
    if len(words) <= 20:  # 如果词数量较少，则全部打印
        logger.info(f"最终去重结果: {words}")
    else:  # 否则只打印部分
        logger.info(f"最终去重结果(前20个): {words[:20]}...")

    return words


def custom_tokenize(text: str, remove_stopwords: bool = True, stopwords_path: str = None) -> List[str]:
    """
    直接进行分词，不调用API

    参数:
        text: 要分词的文本
        remove_stopwords: 是否去除停用词，默认为True
        stopwords_path: 停用词文件路径，默认为None，此时使用代码所在目录下的stopwords.txt

    返回:
        分词后的词列表（已去除停用词）
    """
    logger.info(f"原始文本: {text[:100]}..." if len(text) > 100 else f"原始文本: {text}")
    words = list(jieba.cut(text))
    logger.info(f"分词结果(停用词过滤前): {len(words)} 个词")
    if len(words) <= 20:  # 如果词数量较少，则全部打印
        logger.info(f"分词结果: {words}")
    else:  # 否则只打印部分
        logger.info(f"分词结果(前20个): {words[:20]}...")

    # 去除停用词（如果需要）
    if remove_stopwords:
        stopwords = load_stopwords(stopwords_path)
        removed_words = [word for word in words if word in stopwords]
        words = [word for word in words if word not in stopwords and word.strip()]

        logger.info(f"停用词过滤: 移除了 {len(removed_words)} 个停用词")
        if removed_words:
            if len(removed_words) <= 20:
                logger.info(f"被移除的停用词: {removed_words}")
            else:
                logger.info(f"被移除的停用词(前20个): {removed_words[:20]}...")

    return words
