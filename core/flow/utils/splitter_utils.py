# coding=utf-8
"""
core/flow/splitter 的纯函数提取

对应组件：core/flow/splitter/splitter.py
参考来源：Splitter 类的 _invoke 方法

无需 Canvas/DSL/Graph 依赖，直接使用核心切分逻辑

@project: multirag
@date: 2025-11-10
"""
import asyncio
import logging
import re
from copy import deepcopy

import trio
from deepdoc.parser.pdf_parser import RAGFlowPdfParser
from core.nlp import naive_merge, naive_merge_with_images

logger = logging.getLogger(__name__)


def _get_running_backend():
    """检测当前运行的异步后端（trio 或 asyncio）"""
    try:
        trio.lowlevel.current_task()
        return "trio"
    except RuntimeError:
        pass
    
    try:
        asyncio.get_running_loop()
        return "asyncio"
    except RuntimeError:
        pass
    
    return None


async def _to_thread(func, *args, **kwargs):
    """兼容 trio 和 asyncio 的 to_thread"""
    backend = _get_running_backend()
    
    if backend == "trio":
        return await trio.to_thread.run_sync(lambda: func(*args, **kwargs))
    elif backend == "asyncio":
        return await asyncio.to_thread(func, *args, **kwargs)
    else:
        return func(*args, **kwargs)


class FlowSplitter:
    """
    core/flow/splitter 的独立版本 - 不依赖 Canvas/Graph
    
    提取 core/flow/splitter/splitter.py 的核心逻辑
    
    支持：
    - 重叠切分（overlapped_percent）
    - 自定义 chunk 大小
    - 自定义分隔符
    - 保留位置信息
    - 保留图片关联
    """
    
    @staticmethod
    async def split_text(
        text: str,
        chunk_token_size: int = 512,
        delimiters: list[str] | None = None,
        overlapped_percent: float = 0,
        children_delimiters: list[str] | None = None,
        callback=None
    ) -> list[dict]:
        """
        纯文本切分（参考 core/flow/splitter/splitter.py 第 64-97 行）
        
        Args:
            text: 待切分的文本
            chunk_token_size: chunk 大小
            delimiters: 分隔符列表
            overlapped_percent: 重叠比例（0-0.5）
            children_delimiters: 子块分隔符列表（用于 child-parent chunking）
            callback: 进度回调
        
        Returns:
            [{"text": "chunk1"}, {"text": "chunk2"}, ...]
            如果设置了 children_delimiters，则返回:
            [{"text": "child_chunk", "mom": "parent_chunk"}, ...]
        """
        if delimiters is None:
            delimiters = ["\n\n", "\n", "。", "！", "？"]
        
        if callback:
            callback(0.1, "Start to split into chunks.")
        
        # 转换分隔符格式
        deli = ""
        for d in delimiters:
            if len(d) > 1:
                deli += f"`{d}`"
            else:
                deli += d
        
        # 处理 children_delimiters
        child_deli = ""
        if children_delimiters:
            for d in children_delimiters:
                if len(d) > 1:
                    child_deli += f"`{d}`"
                else:
                    child_deli += d
        child_deli = [m.group(1) for m in re.finditer(r"`([^`]+)`", child_deli)]
        custom_pattern = "|".join(re.escape(t) for t in sorted(set(child_deli), key=len, reverse=True))
        
        # 调用底层切分函数
        chunks = await _to_thread(naive_merge, text, chunk_token_size, deli, overlapped_percent)
        
        if custom_pattern:
            result = []
            for c in chunks:
                if not c.strip():
                    continue
                split_sec = re.split(r"(%s)" % custom_pattern, c, flags=re.DOTALL)
                if split_sec:
                    for txt in split_sec:
                        result.append({
                            "text": txt,
                            "mom": c
                        })
                else:
                    result.append({"text": c})
        else:
            result = [{"text": c.strip()} for c in chunks if c.strip()]
        
        if callback:
            callback(1.0, f"Split into {len(result)} chunks.")
        
        return result
    
    @staticmethod
    async def split_sections_with_images(
        sections: list[tuple],  # [(text, position_tag), ...]
        images: list,
        chunk_token_size: int = 512,
        delimiters: list[str] | None = None,
        overlapped_percent: float = 0,
        children_delimiters: list[str] | None = None,
        callback=None
    ) -> list[dict]:
        """
        带图片的 sections 切分（参考 core/flow/splitter/splitter.py 第 99-152 行）
        
        保留位置信息和图片关联
        
        Args:
            sections: [(text, position_tag), ...] 格式的 sections
            images: 对应的图片列表
            chunk_token_size: chunk 大小
            delimiters: 分隔符列表
            overlapped_percent: 重叠比例
            children_delimiters: 子块分隔符列表（用于 child-parent chunking）
            callback: 进度回调
        
        Returns:
            [
                {
                    "text": "chunk text",
                    "image": <Image object>,
                    "positions": [[page, x0, x1, top, bottom], ...]
                },
                ...
            ]
            如果设置了 children_delimiters，则每个 chunk 还包含:
            {"mom": "parent_chunk_text", ...}
        """
        if delimiters is None:
            delimiters = ["\n"]
        
        if callback:
            callback(0.1, "Start to split into chunks.")
        
        # 转换分隔符格式
        deli = ""
        for d in delimiters:
            if len(d) > 1:
                deli += f"`{d}`"
            else:
                deli += d
        
        # 处理 children_delimiters
        child_deli = ""
        if children_delimiters:
            for d in children_delimiters:
                if len(d) > 1:
                    child_deli += f"`{d}`"
                else:
                    child_deli += d
        child_deli = [m.group(1) for m in re.finditer(r"`([^`]+)`", child_deli)]
        custom_pattern = "|".join(re.escape(t) for t in sorted(set(child_deli), key=len, reverse=True))
        
        # 调用底层切分函数（带图片）
        chunks, chunk_images = await _to_thread(
            naive_merge_with_images,
            sections,
            images,
            chunk_token_size,
            deli,
            overlapped_percent
        )
        
        # 提取位置信息
        cks = [
            {
                "text": RAGFlowPdfParser.remove_tag(c),
                "image": img,
                "positions": [[pos[0][-1]+1, *pos[1:]] for pos in RAGFlowPdfParser.extract_positions(c)]
            }
            for c, img in zip(chunks, chunk_images) if c.strip()
        ]
        
        if custom_pattern:
            result = []
            for c in cks:
                split_sec = re.split(r"(%s)" % custom_pattern, c["text"], flags=re.DOTALL)
                if split_sec:
                    c["mom"] = c["text"]
                    for txt in split_sec:
                        cc = deepcopy(c)
                        cc["text"] = txt
                        result.append(cc)
                else:
                    result.append(c)
        else:
            result = cks
        
        if callback:
            callback(1.0, f"Split into {len(result)} chunks.")
        
        return result


# ========== 便捷接口 ==========

async def split_chunks(
    parsed_result: dict,
    chunk_token_size: int = 512,
    delimiters: list[str] | None = None,
    overlapped_percent: float = 0,
    children_delimiters: list[str] | None = None,
    callback=None
) -> list[dict]:
    """
    使用 core/flow 逻辑切分（支持重叠、保留位置、child-parent chunking）
    
    参考：core/flow/splitter/splitter.py 的完整逻辑
    
    Args:
        parsed_result: parse_file 的返回结果
        chunk_token_size: chunk 大小
        delimiters: 分隔符列表
        overlapped_percent: 重叠比例
        children_delimiters: 子块分隔符列表（用于 child-parent chunking）
        callback: 进度回调
    
    Returns:
        [{"text": "...", "image": <Image>, "positions": [...]}, ...]
        如果设置了 children_delimiters，则每个 chunk 还包含:
        {"mom": "parent_chunk_text", ...}
    """
    output_format = parsed_result.get("output_format")
    
    if output_format == "text":
        # 纯文本切分
        text = parsed_result.get("text", "")
        
        # 检查文本是否为空
        if not text or not text.strip():
            logger.warning(f"Empty text for splitting, parsed_result keys: {parsed_result.keys()}")
            return []
        
        logger.info(f"Splitting text, length={len(text)}, preview={text[:100]}")
        
        return await FlowSplitter.split_text(
            text, chunk_token_size, delimiters, overlapped_percent, children_delimiters, callback
        )
    
    elif output_format == "json":
        # 结构化切分（保留位置）
        json_result = parsed_result.get("json", [])
        sections = [(o.get("text", ""), o.get("position_tag", "")) for o in json_result]
        images = [o.get("image") for o in json_result]
        
        return await FlowSplitter.split_sections_with_images(
            sections, images, chunk_token_size, delimiters, overlapped_percent, children_delimiters, callback
        )
    
    elif output_format == "markdown":
        # Markdown 切分
        markdown = parsed_result.get("markdown", "")
        return await FlowSplitter.split_text(
            markdown, chunk_token_size, delimiters, overlapped_percent, children_delimiters, callback
        )
    
    elif output_format == "html":
        # HTML 切分
        html = parsed_result.get("html", "")
        return await FlowSplitter.split_text(
            html, chunk_token_size, delimiters, overlapped_percent, children_delimiters, callback
        )
    
    else:
        raise ValueError(f"Unsupported output_format: {output_format}")

