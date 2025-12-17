# coding=utf-8
"""
core/flow/extractor 的纯函数提取

对应组件：core/flow/extractor/extractor.py
参考来源：Extractor 类的 _invoke 方法

无需 Canvas/DSL/Graph 依赖，直接使用核心提取逻辑

注意：analyze_v2 已有完整的元数据提取实现，此文件主要用于与 core/flow 保持一致性

@project: multirag
@date: 2025-11-10
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


class FlowExtractor:
    """
    core/flow/extractor 的独立版本
    
    注意：analyze_v2 的元数据提取逻辑已经比 core/flow/extractor 更强大：
    - 支持 source（global_summary/cluster_summaries/original_chunks）
    - 支持 call_mode（single/batch）
    - 支持 post_process（none/split_comma/counter_top10/concat）
    
    此类主要用于：
    1. 保持与 core/flow 结构的一致性
    2. 未来可能的功能扩展
    3. 作为简单场景的便捷接口
    """
    
    @staticmethod
    async def extract(
        chunks: list[dict],
        field_name: str,
        prompt: str,
        llm_model,
        temperature: float = 0.1,
        max_tokens: int = 512,
        callback=None
    ) -> list[dict]:
        """
        从 chunks 提取元数据（参考 core/flow/extractor/extractor.py 第 34-61 行）
        
        Args:
            chunks: chunk 列表
            field_name: 目标字段名
            prompt: 提示词
            llm_model: LLM 模型实例
            temperature: 温度
            max_tokens: 最大 tokens
            callback: 进度回调
        
        Returns:
            原 chunks + 新字段 [{"text": "...", "field_name": "extracted_value"}, ...]
        """
        if callback:
            callback(0.1, "Start to generate.")
        
        result_chunks = []
        for i, chunk in enumerate(chunks):
            text = chunk.get("text", chunk.get("content_with_weight", ""))
            
            # 调用 LLM
            msg = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": text}
            ]
            
            extracted = await asyncio.to_thread(
                llm_model.chat,
                msg[0]["content"],
                msg[1:],
                {"temperature": temperature, "max_tokens": max_tokens}
            )
            
            # 添加字段
            new_chunk = chunk.copy()
            new_chunk[field_name] = extracted
            result_chunks.append(new_chunk)
            
            if callback and i % (len(chunks)//100+1) == 1:
                progress = (i + 1) / len(chunks)
                callback(progress, f"{i+1} / {len(chunks)}")
        
        if callback:
            callback(1.0, "Extraction complete.")
        
        return result_chunks


# ========== 便捷接口 ==========

async def extract_metadata(
    chunks: list[dict],
    field_name: str,
    prompt: str,
    llm_model,
    temperature: float = 0.1,
    max_tokens: int = 512,
    callback=None
) -> list[dict]:
    """
    元数据提取便捷接口
    
    参考：core/flow/extractor/extractor.py
    
    注意：对于 analyze_v2，推荐使用 task_executor.py 中的完整元数据提取逻辑，
    支持更多高级功能（source、call_mode、post_process）
    
    此接口主要用于简单场景或与 core/flow 保持一致性
    """
    return await FlowExtractor.extract(
        chunks, field_name, prompt, llm_model, 
        temperature, max_tokens, callback
    )

