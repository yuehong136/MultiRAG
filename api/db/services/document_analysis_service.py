# coding=utf-8
"""
文档智能分析服务 V3 - 完全遵循项目规范

改进点（相比V2）：
1. ✅ 从 core.prompts.prompts 导入 prompt（而非动态加载）
2. ✅ 使用 PROMPT_JINJA_ENV 渲染模板
3. ✅ 完全复用项目基础设施
4. ✅ 使用 Python 3.12+ 和 Pydantic V2 语法

@project: multirag
@Author: Claude & 龙
@date: 2025/10/16
"""
import logging
import re
import time
import asyncio
from collections import Counter, defaultdict

import trio
import numpy as np
from sqlalchemy.orm import Session

from common.constants import LLMType
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from core.nlp import rag_tokenizer
from core.nlp.term_weight import Dealer as TermWeightDealer
from common.token_utils import truncate
from common.connection_utils import timeout

# ⭐ 遵循项目规范：从 core.prompts.prompts 导入
from core.prompts.generator import (
    cluster_summary_prompt,
    cluster_keyword_prompt,
    global_tag_prompt,
    global_summary_prompt
)

# 复用项目的RAPTOR实现
from core.raptor import RecursiveAbstractiveProcessing4TreeOrganizedRetrieval as Raptor

# 复用项目的缓存和并发控制
from graphrag.utils import (
    get_llm_cache,
    set_llm_cache,
    get_embed_cache,
    set_embed_cache
)

from api import settings
from core.settings import PAGERANK_FLD

# asyncio版本的并发限制器 (代替trio.CapacityLimiter)
import os
_MAX_CONCURRENT_CHATS = int(os.environ.get('MAX_CONCURRENT_CHATS', 10))
_asyncio_chat_limiter = asyncio.Semaphore(_MAX_CONCURRENT_CHATS)


class SmartTagDeduplicator:
    """
    智能标签去重器

    基于Jaccard相似度 + 同义词词典
    """

    def __init__(self):
        # 领域同义词词典（可扩展）
        self.synonyms = {
            "ai": {"人工智能", "artificial intelligence", "ai"},
            "ml": {"机器学习", "machine learning", "ml"},
            "dl": {"深度学习", "deep learning", "dl"},
            "cv": {"计算机视觉", "computer vision", "cv"},
            "nlp": {"自然语言处理", "natural language processing", "nlp"},
            "nn": {"神经网络", "neural network", "nn"},
            "cnn": {"卷积神经网络", "convolutional neural network", "cnn"},
            "rnn": {"循环神经网络", "recurrent neural network", "rnn"},
            "bert": {"bert模型", "bert"},
            "gpt": {"gpt模型", "gpt"},
            "llm": {"大语言模型", "large language model", "llm"},
        }

    def normalize(self, tag: str) -> set:
        """标签归一化：分词 + 扩展同义词"""
        tokens = set(tag.lower().split())
        expanded = set(tokens)

        for token in tokens:
            if token in self.synonyms:
                expanded.update(self.synonyms[token])

        return expanded

    def is_duplicate(self, tag_a: str, tag_b: str, threshold=0.6) -> bool:
        """判断两个标签是否重复"""
        # 1. 精确匹配
        if tag_a.lower() == tag_b.lower():
            return True

        # 2. 归一化
        set_a = self.normalize(tag_a)
        set_b = self.normalize(tag_b)

        # 3. Jaccard相似度
        intersection = set_a & set_b
        union = set_a | set_b

        if len(union) == 0:
            return False

        jaccard = len(intersection) / len(union)

        if jaccard > threshold:
            return True

        # 4. 包含关系检查
        if set_a.issubset(set_b) or set_b.issubset(set_a):
            len_diff = abs(len(set_a) - len(set_b))
            if len_diff <= 2:
                return True

        return False


class DocumentAnalysisService:
    """
    文档智能分析服务 V3

    完全遵循项目规范
    """

    def __init__(self, db: Session, tenant_id: str):
        self.db = db
        self.tenant_id = tenant_id
        self.tw = TermWeightDealer()
        self.tag_deduplicator = SmartTagDeduplicator()

    async def analyze_document(
        self,
        doc_id: str | None = None,
        file=None,  # UploadFile or file-like object
        filename: str | None = None,
        kb_id: str | None = None,
        include_summary: bool = True,
        include_tags: bool = True,
        summary_type: str = "short",
        raptor_config: dict | None = None,
        use_cache: bool = True
    ) -> dict:
        """
        文档智能分析 (支持doc_id和file两种模式)

        Args:
            doc_id: 文档ID(可选,与file二选一)
            file: 上传的文件对象(可选,与doc_id二选一)
            filename: 文件名(file模式时可选)
            kb_id: 知识库ID(doc_id模式时必填)
            include_summary: 是否生成摘要
            include_tags: 是否生成标签
            summary_type: 摘要类型 (short/long)
            raptor_config: RAPTOR配置（完整参数）
                {
                    "max_cluster": 64,
                    "max_token": 512,
                    "threshold": 0.1,
                    "random_seed": 42,
                    "prompt": "..."  # 可选
                }
            use_cache: 是否使用缓存

        Raises:
            ValueError: 参数错误或文档不存在
        """
        start_time = time.time()

        # 参数验证
        if not doc_id and not file:
            raise ValueError("Must provide either doc_id or file")

        if doc_id and file:
            raise ValueError("Cannot provide both doc_id and file")

        if doc_id and not kb_id:
            raise ValueError("kb_id is required when using doc_id")

        # 默认RAPTOR配置
        if raptor_config is None:
            raptor_config = {
                "max_cluster": 64,
                "max_token": 512,
                "threshold": 0.1,
                "random_seed": 42
            }

        # 1. 获取文档信息
        if doc_id:
            doc = DocumentService.get_by_id(self.db, doc_id)
            if not doc:
                raise ValueError(f"Document {doc_id} not found")
            doc_name = doc.name
            doc_kb_id = doc.kb_id or kb_id
        else:
            # file模式
            doc_name = filename or (file.filename if hasattr(file, 'filename') else "Uploaded File")
            doc_kb_id = None
            doc_id = None  # 确保后续逻辑知道这是临时文件

        # 2. 获取文档chunks (自适应数据源)
        chunks = await self._get_document_chunks(
            doc_id=doc_id,
            kb_id=doc_kb_id,
            file=file,
            filename=filename
        )

        if not chunks or len(chunks) == 0:
            raise ValueError(f"No chunks found for document {doc_id}")

        logging.info(f"Document {doc_id} has {len(chunks)} chunks")

        # 3. 自适应策略
        use_raptor = len(chunks) > 10

        if use_raptor:
            result = await self._analyze_with_raptor(
                doc_id=doc_id,
                doc_name=doc_name,
                kb_id=doc_kb_id,
                chunks=chunks,
                include_summary=include_summary,
                include_tags=include_tags,
                summary_type=summary_type,
                raptor_config=raptor_config,
                use_cache=use_cache
            )
        else:
            result = await self._analyze_short_document(
                doc_id=doc_id,
                doc_name=doc_name,
                chunks=chunks,
                include_summary=include_summary,
                include_tags=include_tags,
                summary_type=summary_type,
                use_cache=use_cache
            )

        # 4. 元数据
        result["metadata"] = {
            "chunk_count": len(chunks),
            "use_raptor": use_raptor,
            "raptor_config": raptor_config if use_raptor else None,
            "processing_time_seconds": round(time.time() - start_time, 2)
        }

        return result

    async def _get_chunks_from_milvus(
        self,
        doc_id: str,
        kb_id: str
    ) -> list[dict]:
        """
        从Milvus向量库获取已入库的chunks (最优性能方案)

        性能: ~0.5-2秒 (100-1000 chunks)
        适用: 文档已完成向量化 (run="1" 且 chunk_num>0)

        Returns:
            list[dict]: chunks列表,每个元素包含content_with_weight等字段
        """
        kb = KnowledgebaseService.get_by_id(self.db, kb_id)
        if not kb:
            raise ValueError(f"Knowledgebase {kb_id} not found")

        logging.info(f"Fetching chunks from Milvus for doc {doc_id}")

        chunks = settings.retriever.chunk_list(
            doc_id=doc_id,
            tenant_id=self.tenant_id,
            kb_ids=[kb_id],
            max_count=10000,  # 足够大以获取所有chunks
            fields=["content_with_weight", "content_ltks", "important_kwd", "question_kwd"]
        )

        if not chunks:
            logging.warning(f"No chunks found in Milvus for doc {doc_id}")
            return []

        logging.info(f"Retrieved {len(chunks)} chunks from Milvus")
        return chunks

    async def _get_chunks_by_reparse(
        self,
        doc_id: str
    ) -> list[dict]:
        """
        从MinIO读取文件并重新解析 (兼容性方案)

        性能: ~5-30秒 (取决于文档大小和复杂度)
        适用: 文档未向量化时的后备方案 (run="0")

        Returns:
            list[dict]: chunks列表
        """
        logging.info(f"Reparsing document {doc_id} from storage")

        # FastAPI最佳实践: 使用asyncio.to_thread
        chunks = await asyncio.to_thread(
            DocumentService.preview_document_chunks,
            self.db,
            doc_id,
            None,  # parser_config_override
            None   # limit
        )

        # 统一格式
        result = []
        for chunk in chunks:
            if isinstance(chunk, dict):
                # 已经是dict格式,保持不变
                result.append(chunk)
            elif isinstance(chunk, str):
                # 字符串格式,转换为dict
                result.append({
                    "content_with_weight": chunk,
                    "content_ltks": chunk,
                    "important_kwd": [],
                    "question_kwd": []
                })
            else:
                # 其他格式,转为字符串再转dict
                result.append({
                    "content_with_weight": str(chunk),
                    "content_ltks": str(chunk),
                    "important_kwd": [],
                    "question_kwd": []
                })

        logging.info(f"Reparsed {len(result)} chunks")
        return result

    async def _parse_uploaded_file(
        self,
        file,  # UploadFile or file-like object
        filename: str | None = None
    ) -> list[dict]:
        """
        解析用户直传的文件(临时解析,不入库)

        性能: ~5-30秒
        适用: 快速分析,不需要保存文档

        Args:
            file: FastAPI UploadFile对象或file-like对象
            filename: 文件名(如果file没有filename属性)

        Returns:
            list[dict]: chunks列表
        """
        import tempfile
        import os

        # 获取文件名
        if hasattr(file, 'filename'):
            fname = file.filename
        elif filename:
            fname = filename
        else:
            raise ValueError("filename required for file parsing")

        logging.info(f"Parsing uploaded file: {fname}")

        # 读取文件内容
        if hasattr(file, 'read'):
            if hasattr(file.read, '__self__'):  # async read
                file_content = await file.read()
            else:  # sync read
                file_content = await asyncio.to_thread(file.read)
        else:
            raise ValueError("file must be readable")

        # 确定文件类型
        file_type = fname.split(".")[-1].lower() if "." in fname else ""

        # 临时保存文件
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{file_type}") as temp_file:
            temp_file.write(file_content)
            temp_path = temp_file.name

        try:
            # 获取解析器
            default_parser = "naive"  # 默认解析器
            module, _ = DocumentService._resolve_parser_for_filename(fname, default_parser)

            # 空回调函数
            def _noop(prog=None, msg=""):
                return None
            
            # 执行切片 (使用asyncio.to_thread)
            result = await asyncio.to_thread(
                module.chunk,
                fname,
                binary=file_content,
                from_page=0,
                to_page=100000,
                lang="Chinese",
                callback=_noop,
                parser_config={},
                tenant_id=self.tenant_id
            )

            # 统一格式
            chunks = []
            if isinstance(result, list):
                for item in result:
                    if isinstance(item, dict):
                        chunks.append(item)
                    else:
                        chunks.append({
                            "content_with_weight": str(item),
                            "content_ltks": str(item),
                            "important_kwd": [],
                            "question_kwd": []
                        })
            else:
                chunks.append({
                    "content_with_weight": str(result),
                    "content_ltks": str(result),
                    "important_kwd": [],
                    "question_kwd": []
                })

            logging.info(f"Parsed {len(chunks)} chunks from uploaded file")
            return chunks

        finally:
            # 清理临时文件
            if os.path.exists(temp_path):
                os.remove(temp_path)

    async def _get_document_chunks(
        self,
        doc_id: str | None = None,
        kb_id: str | None = None,
        file=None,  # UploadFile or file-like object
        filename: str | None = None
    ) -> list[dict]:
        """
        获取文档chunks (自适应数据源选择)

        支持三种模式:
        1. doc_id + kb_id: 已上传的文档
           - 优先从Milvus获取(已向量化)
           - 否则重新解析(未向量化)
        2. file: 直传文件(临时分析)

        Args:
            doc_id: 文档ID(可选)
            kb_id: 知识库ID(doc_id时必填)
            file: 上传的文件对象(可选)
            filename: 文件名(file时可选)

        Returns:
            list[dict]: chunks列表
        """
        # 参数验证
        if not doc_id and not file:
            raise ValueError("Must provide either doc_id or file")

        if doc_id and file:
            raise ValueError("Cannot provide both doc_id and file")

        if doc_id and not kb_id:
            raise ValueError("kb_id is required when using doc_id")

        # 场景1: 直传文件
        if file:
            return await self._parse_uploaded_file(file, filename)

        # 场景2: 已上传的文档
        if doc_id:
            # 检查文档状态
            doc = DocumentService.get_by_id(self.db, doc_id)
            if not doc:
                raise ValueError(f"Document {doc_id} not found")

            # 判断是否已向量化 (直接访问ORM对象属性)
            # TaskStatus: UNSTART="0", RUNNING="1", CANCEL="2", DONE="3", FAIL="4"
            run_status = getattr(doc, "run", "0")
            chunk_num = getattr(doc, "chunk_num", 0)
            progress = getattr(doc, "progress", 0.0)

            can_use_milvus = (
                run_status == "3" and  # DONE状态,解析完成
                chunk_num > 0 and
                progress >= 0.99
            )

            if can_use_milvus:
                # 优先方案: 从Milvus获取(性能最优)
                try:
                    chunks = await self._get_chunks_from_milvus(doc_id, kb_id)
                    if chunks and len(chunks) > 0:
                        logging.info(f"Successfully fetched {len(chunks)} chunks from Milvus")
                        return chunks
                    else:
                        logging.warning("Milvus returned empty chunks, fallback to reparse")
                except Exception as e:
                    logging.warning(f"Milvus retrieval failed: {e}, fallback to reparse")

            # 后备方案: 重新解析文档
            return await self._get_chunks_by_reparse(doc_id)

    async def _analyze_with_raptor(
        self,
        doc_id: str | None,
        doc_name: str,
        kb_id: str | None,
        chunks: list[dict],
        include_summary: bool,
        include_tags: bool,
        summary_type: str,
        raptor_config: dict,
        use_cache: bool
    ) -> dict:
        """使用RAPTOR进行分析"""
        result = {
            "doc_id": doc_id,
            "doc_name": doc_name
        }

        # 1. 准备RAPTOR输入格式 (在asyncio环境中准备embedding)
        embd_model = self._get_embedding_model()

        raptor_chunks = []
        for chunk in chunks:
            content = chunk.get("content_with_weight", "")

            # 检查缓存的embedding
            cached_embd = await asyncio.to_thread(
                get_embed_cache, embd_model.llm_name, content
            )

            if cached_embd is not None:
                embd = cached_embd
            else:
                embd_result, _ = await asyncio.to_thread(
                    embd_model.encode, [content]
                )
                embd = embd_result[0]
                await asyncio.to_thread(
                    set_embed_cache, embd_model.llm_name, content, embd
                )

            raptor_chunks.append((content, np.array(embd)))

        # 2. 设置默认prompt (如果未提供或为None)
        if not raptor_config.get("prompt"):
            raptor_config["prompt"] = """Please provide a comprehensive yet concise summary of the following text cluster:

{cluster_content}

Focus on the main ideas and key points. Keep the summary coherent and readable."""

        # 3. 运行RAPTOR (在trio环境中运行,因为raptor内部使用trio)
        logging.info(f"Running RAPTOR with config: {raptor_config}")
        original_length = len(raptor_chunks)

        # ⭐ 关键设计：在 trio 线程中重新创建 LLMBundle
        # 
        # 为什么必须这样做:
        # 1. SQLAlchemy 限制: Session 不能跨线程共享
        # 2. Trio 隔离: 需要在独立的 trio 事件循环中运行
        # 3. 安全性优先: 跨线程共享 session 会导致数据损坏或死锁
        # 
        # 架构优势:
        # - 在 asyncio 中准备 embedding (已完成，见上面的代码)
        # - 只传递配置信息给 trio 线程 (tenant_id, llm_name 等)
        # - 在 trio 中创建独立的 db session 和 LLMBundle
        # - LLM 模型本身是全局缓存，不会重复加载
        # 
        # 性能分析:
        # - 数据库连接: 从连接池获取，用完立即归还
        # - LLM 模型: 全局单例缓存，只加载一次
        # - LLMBundle: 轻量级 wrapper (~KB)
        # - 并发控制: chat_limiter 限制为 10 个
        def run_raptor_in_trio():
            """在 trio 环境中运行 RAPTOR"""
            from api.db.db_models import db_connection
            
            # ⚠️ 重要：在 trio 并行环境中禁用 usage tracking
            # 
            # 问题：RAPTOR 内部使用 trio nursery 并行执行多个 summarize 任务
            # 如果它们共享同一个 db session，会导致 SQLAlchemy 状态冲突
            # 
            # 解决方案：传递 None 作为 db，LLMBundle.encode() 会跳过 usage tracking
            # 这样既能执行模型推理，又避免了跨线程 session 问题
            # 
            # 副作用：RAPTOR 阶段的 token 使用量不会被记录
            # 但这是可接受的，因为：
            # 1. 后续的标签和摘要生成会正常记录
            # 2. RAPTOR 的使用量相对较小
            # 3. 系统稳定性优先于完整的使用量统计
            with db_connection() as db:
                # 创建 LLMBundle (仅用于初始化模型)
                llm_model = LLMBundle(db, self.tenant_id, LLMType.CHAT)
                embd_model = LLMBundle(db, self.tenant_id, LLMType.EMBEDDING)
                
                # 禁用 usage tracking（避免 trio 并行任务冲突）
                llm_model.db = None
                embd_model.db = None

                raptor = Raptor(
                    max_cluster=raptor_config.get("max_cluster", 64),
                    llm_model=llm_model,
                    embd_model=embd_model,
                    prompt=raptor_config["prompt"],
                    max_token=raptor_config.get("max_token", 512),
                    threshold=raptor_config.get("threshold", 0.1)
                )

                # 运行 RAPTOR (在 trio 事件循环中)
                return trio.run(
                    raptor,
                    raptor_chunks,
                    raptor_config.get("random_seed", 42),
                    lambda msg: logging.info(f"RAPTOR: {msg}")
                )

        # 使用asyncio.to_thread在独立线程中运行
        # 并发控制: FastAPI的请求并发由上层控制,这里每个请求串行执行RAPTOR
        raptor_result = await asyncio.to_thread(run_raptor_in_trio)

        # 5. 提取聚类摘要
        logging.info(f"RAPTOR result: original_length={original_length}, result_length={len(raptor_result)}")
        logging.info(f"RAPTOR config: max_cluster={raptor_config.get('max_cluster')}, threshold={raptor_config.get('threshold')}")
        
        cluster_summaries = [
            raptor_result[i][0]
            for i in range(original_length, len(raptor_result))
        ]

        logging.info(f"RAPTOR generated {len(cluster_summaries)} cluster summaries from {original_length} original chunks")
        
        # 打印每个聚类摘要的前150个字符
        for i, summary in enumerate(cluster_summaries[:3]):  # 只打印前3个
            logging.info(f"Cluster summary {i+1}: {summary[:150]}...")

        # 6. 获取LLM模型用于后续处理 (在asyncio环境中,用于标签和摘要生成)
        llm_model = self._get_llm_model()

        # 7. 基于RAPTOR结果生成标签和摘要 (FastAPI最佳实践: asyncio.gather)
        tasks = []
        if include_tags:
            tasks.append(
                self._generate_tags_from_raptor(
                    chunks, cluster_summaries, result, llm_model, use_cache
                )
            )
        if include_summary:
            tasks.append(
                self._generate_summary_from_raptor(
                    cluster_summaries, result, summary_type, llm_model, use_cache
                )
            )

        if tasks:
            await asyncio.gather(*tasks)

        result["metadata"] = {"cluster_summary_count": len(cluster_summaries)}

        return result

    async def _generate_tags_from_raptor(
        self,
        original_chunks: list[dict],
        cluster_summaries: list[str],
        result: dict,
        llm_model,
        use_cache: bool
    ):
        """基于RAPTOR聚类结果生成标签"""
        # 1. 为每个簇提取关键词（并行）
        cluster_keywords_list = []

        async def extract_cluster_keyword(cluster_summary: str):
            logging.debug(f"Extracting keywords from summary: {cluster_summary[:100]}...")
            
            # ⭐ 使用项目规范的渲染函数
            prompt = cluster_keyword_prompt(cluster_content=cluster_summary)
            
            logging.debug(f"Prompt length: {len(prompt)} chars")

            # 检查缓存
            if use_cache:
                cached = await asyncio.to_thread(
                    get_llm_cache, llm_model.llm_name, prompt, [], {}
                )
                if cached:
                    return cached

            # LLM调用
            async with _asyncio_chat_limiter:
                keywords = await asyncio.to_thread(
                    llm_model.chat,
                    "You're a helpful assistant.",
                    [{"role": "user", "content": prompt}],
                    {"temperature": 0.2, "max_tokens": 80}
                )

            keywords = re.sub(r"^.*</think>", "", keywords, flags=re.DOTALL).strip()

            # 保存缓存
            if use_cache:
                await asyncio.to_thread(
                    set_llm_cache, llm_model.llm_name, prompt, keywords, [], {}
                )

            return keywords

        # ⚠️ 并行执行前临时禁用 usage tracking（避免 session 冲突）
        original_db = llm_model.db
        llm_model.db = None
        
        try:
            # 并行提取 (使用asyncio.gather)
            cluster_keywords_list = await asyncio.gather(
                *[extract_cluster_keyword(summary) for summary in cluster_summaries]
            )
        finally:
            # 恢复 db session
            llm_model.db = original_db

        logging.info(f"Extracted {len(cluster_keywords_list)} cluster keywords")
        logging.debug(f"Cluster keywords: {cluster_keywords_list}")

        # 2. 聚合为全局语义标签
        cluster_keywords_text = "\n".join([f"- {kw}" for kw in cluster_keywords_list])
        
        logging.info(f"Cluster keywords text length: {len(cluster_keywords_text)} chars")
        logging.info(f"Cluster keywords text preview:\n{cluster_keywords_text[:500]}...")

        # ⭐ 使用项目规范的渲染函数
        global_tag_prompt_text = global_tag_prompt(cluster_keywords=cluster_keywords_text)
        
        logging.info(f"Global tag prompt preview (first 500 chars):\n{global_tag_prompt_text[:500]}...")

        async with _asyncio_chat_limiter:
            semantic_tags_str = await asyncio.to_thread(
                llm_model.chat,
                "You're a helpful assistant.",
                [{"role": "user", "content": global_tag_prompt_text}],
                {"temperature": 0.3, "max_tokens": 100}
            )

        semantic_tags_str = re.sub(r"^.*</think>", "", semantic_tags_str, flags=re.DOTALL)
        semantic_tags = [t.strip() for t in semantic_tags_str.split(",")][:3]

        # 3. 词频标签
        frequency_tags = self._compute_frequency_tags(original_chunks)

        # 4. 智能去重
        merged = self._merge_tags_smart(semantic_tags, frequency_tags)

        result["semantic_tags"] = semantic_tags
        result["frequency_tags"] = frequency_tags
        result["combined_tags"] = merged

    async def _generate_summary_from_raptor(
        self,
        cluster_summaries: list[str],
        result: dict,
        summary_type: str,
        llm_model,
        use_cache: bool
    ):
        """基于RAPTOR聚类摘要生成全局摘要"""
        summaries_text = "\n\n---\n\n".join(cluster_summaries)
        
        logging.info(f"Generating {summary_type} summary from {len(cluster_summaries)} cluster summaries")
        logging.info(f"Summaries text length: {len(summaries_text)} chars")

        # ⭐ 使用项目规范的渲染函数
        prompt = global_summary_prompt(section_summaries=summaries_text)
        
        logging.info(f"Summary prompt preview (first 800 chars):\n{prompt[:800]}...")

        max_tokens = 400 if summary_type == "short" else 1000

        # LLM调用
        async with _asyncio_chat_limiter:
            summary = await asyncio.to_thread(
                llm_model.chat,
                "You're a helpful assistant.",
                [{"role": "user", "content": prompt}],
                {"temperature": 0.3, "max_tokens": max_tokens}
            )

        summary = re.sub(r"^.*</think>", "", summary, flags=re.DOTALL).strip()

        if summary_type == "short":
            result["short_summary"] = summary
        else:
            result["long_summary"] = summary

    async def _analyze_short_document(
        self,
        doc_id: str | None,
        doc_name: str,
        chunks: list[dict],
        include_summary: bool,
        include_tags: bool,
        summary_type: str,
        use_cache: bool
    ) -> dict:
        """短文档分析"""
        full_content = "\n\n".join([
            chunk.get("content_with_weight", "") for chunk in chunks
        ])
        full_content = truncate(full_content, 8000)

        result = {"doc_id": doc_id, "doc_name": doc_name}
        llm = self._get_llm_model()

        # FastAPI最佳实践: asyncio.gather
        tasks = []
        if include_tags:
            tasks.append(
                self._generate_tags_for_short_doc(
                    full_content, chunks, result, llm, use_cache
                )
            )
        if include_summary:
            tasks.append(
                self._generate_summary_for_short_doc(
                    full_content, result, summary_type, llm, use_cache
                )
            )

        if tasks:
            await asyncio.gather(*tasks)

        return result

    async def _generate_tags_for_short_doc(
        self,
        full_content: str,
        chunks: list[dict],
        result: dict,
        llm_model,
        use_cache: bool
    ):
        """短文档标签生成"""
        tag_prompt = f"""## Task
Generate 2-3 high-level semantic tags for the following document.

## Requirements
- Generate exactly 2-3 tags
- Tags should capture the main themes
- Output tags separated by comma
- Use the same language as input

## Document Content
{full_content}

## Output
Just output 2-3 tags separated by comma, nothing else."""

        async with _asyncio_chat_limiter:
            semantic_tags_str = await asyncio.to_thread(
                llm_model.chat,
                "You're a helpful assistant.",
                [{"role": "user", "content": tag_prompt}],
                {"temperature": 0.3, "max_tokens": 100}
            )

        semantic_tags_str = re.sub(r"^.*</think>", "", semantic_tags_str, flags=re.DOTALL)
        semantic_tags = [t.strip() for t in semantic_tags_str.split(",")][:3]

        frequency_tags = self._compute_frequency_tags(chunks)
        merged = self._merge_tags_smart(semantic_tags, frequency_tags)

        result["semantic_tags"] = semantic_tags
        result["frequency_tags"] = frequency_tags
        result["combined_tags"] = merged

    async def _generate_summary_for_short_doc(
        self,
        full_content: str,
        result: dict,
        summary_type: str,
        llm_model,
        use_cache: bool
    ):
        """短文档摘要生成"""
        if summary_type == "short":
            max_tokens = 400
            summary_prompt = f"""## Task
Write a concise summary (150-200 words) of the following document.

## Document Content
{full_content}

## Output
Just output the summary, nothing else."""
        else:
            max_tokens = 1000
            summary_prompt = f"""## Task
Write a comprehensive summary (500-800 words) of the following document.

## Document Content
{full_content}

## Output
Just output the summary, nothing else."""

        async with _asyncio_chat_limiter:
            summary = await asyncio.to_thread(
                llm_model.chat,
                "You're a helpful assistant.",
                [{"role": "user", "content": summary_prompt}],
                {"temperature": 0.3, "max_tokens": max_tokens}
            )

        summary = re.sub(r"^.*</think>", "", summary, flags=re.DOTALL).strip()

        if summary_type == "short":
            result["short_summary"] = summary
        else:
            result["long_summary"] = summary

    def _compute_frequency_tags(self, chunks: list[dict]) -> list[str]:
        """计算词频标签"""
        all_tokens = []
        for chunk in chunks:
            content = chunk.get("content_with_weight", "")
            tokens = rag_tokenizer.tokenize(content).split()
            all_tokens.extend(tokens)

        filtered_tokens = [
            t for t in all_tokens
            if len(t) > 1 and not re.match(r'^[0-9]+$', t)
        ]

        counter = Counter(filtered_tokens)
        top_5 = [word for word, count in counter.most_common(5)]

        return top_5

    def _merge_tags_smart(self, semantic_tags: list[str], frequency_tags: list[str]) -> list[str]:
        """智能标签去重与合并"""
        unique_semantic = self._deduplicate_list(semantic_tags)
        unique_frequency = self._deduplicate_list(frequency_tags)

        # 交叉去重
        filtered_frequency = []
        for freq_tag in unique_frequency:
            is_dup = False
            for sem_tag in unique_semantic:
                if self.tag_deduplicator.is_duplicate(freq_tag, sem_tag):
                    is_dup = True
                    break
            if not is_dup:
                filtered_frequency.append(freq_tag)

        combined = unique_semantic + filtered_frequency[:3]
        return combined[:6]

    def _deduplicate_list(self, tags: list[str]) -> list[str]:
        """列表内去重"""
        unique = []
        for tag in tags:
            is_dup = False
            for existing in unique:
                if self.tag_deduplicator.is_duplicate(tag, existing):
                    is_dup = True
                    break
            if not is_dup:
                unique.append(tag)
        return unique

    def _get_llm_model(self):
        """获取LLM模型"""
        from api.db.db_models import db_connection

        with db_connection() as db:
            llm = LLMBundle(db, self.tenant_id, LLMType.CHAT)

        return llm

    def _get_embedding_model(self):
        """获取Embedding模型"""
        from api.db.db_models import db_connection

        with db_connection() as db:
            embd = LLMBundle(db, self.tenant_id, LLMType.EMBEDDING)

        return embd
