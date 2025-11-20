# coding=utf-8
"""
core/flow/hierarchical_merger 的纯函数提取

对应组件：core/flow/hierarchical_merger/hierarchical_merger.py
参考来源：HierarchicalMerger 类的 _invoke 方法

无需 Canvas/DSL/Graph 依赖，直接使用核心层次化合并逻辑

@project: multirag
@date: 2025-11-10
"""
import asyncio
import logging
import re
from copy import deepcopy

logger = logging.getLogger(__name__)


class FlowHierarchicalMerger:
    """
    core/flow/hierarchical_merger 的独立版本
    
    提取 core/flow/hierarchical_merger/hierarchical_merger.py 的核心逻辑
    
    功能：按标题层级合并文档内容
    """
    
    @staticmethod
    async def merge(
        chunks: list[dict],
        levels: list[list[str]] | None = None,
        hierarchy: int = 1,
        callback=None
    ) -> dict:
        """
        层次化合并（参考 core/flow/hierarchical_merger/hierarchical_merger.py 第 49-186 行）
        
        Args:
            chunks: chunk 列表，每个包含 content_with_weight 或 text
            levels: 标题层级正则表达式列表
                例如：[
                    ["^#\\s+", "^第[一二三]+章"],  # 一级标题
                    ["^##\\s+", "^\\d+\\.\\s+"]    # 二级标题
                ]
            hierarchy: 合并到第几层（0-5）
            callback: 进度回调
        
        Returns:
            {
                "chapters": [
                    {"title": "...", "text": "...", "chunks": [...], "chunk_indices": [...]},
                    ...
                ],
                "summaries": ["chapter1_text", "chapter2_text", ...]
            }
        """
        # 默认配置
        if levels is None:
            levels = [
                ["^#\\s+", "^第[一二三四五六七八九十百]+章"],
                ["^##\\s+", "^\\d+\\.\\s+"]
            ]
        
        if callback:
            callback(0.1, "Start to merge hierarchically.")
        
        # 提取文本行
        lines = [c.get("content_with_weight", c.get("text", "")) for c in chunks]
        
        # 识别每行的层级
        matches = []
        for txt in lines:
            matched_level = None
            for lvl, patterns in enumerate(levels):
                for pattern in patterns:
                    if re.search(pattern, txt, re.MULTILINE):
                        matched_level = lvl
                        break
                if matched_level is not None:
                    break
            
            # 如果没匹配到，设为最底层
            if matched_level is None:
                matches.append(len(levels))
            else:
                matches.append(matched_level)
        
        assert len(matches) == len(lines), f"{len(matches)} vs. {len(lines)}"
        
        # 构建层次化树状结构
        root = {"level": -1, "index": -1, "texts": [], "children": []}
        
        for i, level in enumerate(matches):
            if level == 0:
                # 一级标题
                root["children"].append({
                    "level": level,
                    "index": i,
                    "texts": [],
                    "children": []
                })
            elif level == len(levels):
                # 普通文本
                def add_text(node):
                    if not node["children"]:
                        node["texts"].append(i)
                    else:
                        add_text(node["children"][-1])
                add_text(root)
            else:
                # 中间层级
                def add_child(node, target_level, idx):
                    if not node["children"] or target_level == node["level"] + 1:
                        node["children"].append({
                            "level": target_level,
                            "index": idx,
                            "texts": [],
                            "children": []
                        })
                    else:
                        add_child(node["children"][-1], target_level, idx)
                add_child(root, level, i)
        
        # 按 hierarchy 级别收集路径
        all_paths = []
        
        def collect_paths(node, path, depth):
            if not node["children"] and path:
                all_paths.append(path)
            
            for child in node["children"]:
                if depth < hierarchy:
                    new_path = deepcopy(path)
                else:
                    new_path = path
                
                new_path.extend([child["index"], *child["texts"]])
                collect_paths(child, new_path, depth + 1)
                
                if depth == hierarchy:
                    all_paths.append(new_path)
        
        if root["texts"]:
            all_paths.insert(0, root["texts"])
        
        collect_paths(root, [], 0)
        
        # 合并文本
        chapters = []
        summaries = []
        
        for path_indices in all_paths:
            if not path_indices:
                continue
            
            chapter_text = "\n".join([lines[i] for i in path_indices])
            chapter_chunks = [chunks[i] for i in path_indices]
            title_text = lines[path_indices[0]] if path_indices else ""
            
            chapters.append({
                "title": title_text.strip(),
                "text": chapter_text,
                "chunks": chapter_chunks,
                "chunk_indices": path_indices
            })
            
            summaries.append(chapter_text)
        
        if callback:
            callback(1.0, f"Merged into {len(chapters)} chapters.")
        
        logger.info(f"HierarchicalMerger: {len(chapters)} chapters identified")
        
        return {
            "chapters": chapters,
            "summaries": summaries
        }


# ========== 便捷接口 ==========

async def hierarchical_merge(
    chunks: list[dict],
    levels: list[list[str]] | None = None,
    hierarchy: int = 1,
    callback=None
) -> dict:
    """
    层次化合并便捷接口
    
    参考：core/flow/hierarchical_merger/hierarchical_merger.py
    
    Args:
        chunks: chunk 列表
        levels: 标题层级正则表达式
        hierarchy: 合并到第几层
        callback: 进度回调
    
    Returns:
        {
            "chapters": [...],
            "summaries": [...]
        }
    """
    return await FlowHierarchicalMerger.merge(chunks, levels, hierarchy, callback)

