import asyncio
import logging
import re
import time

from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy.orm import Session

from api.db.services.document_service import DocumentService
from api.db.services.llm_service import LLMBundle
from api.db.joint_services.tenant_model_service import get_tenant_default_model_by_type
from api.db.services.metadata_extractor import BatchMetadataExtractor
from common import settings
from common.constants import LLMType
from common.misc_utils import thread_pool_exec
from core.nlp.term_weight import Dealer as TermWeightDealer
from core.raptor import RecursiveAbstractiveProcessing4TreeOrganizedRetrieval as Raptor

logger = logging.getLogger(__name__)

# 并发控制
import os
_MAX_CONCURRENT = int(os.environ.get('MAX_CONCURRENT_CHATS', 10))
_asyncio_limiter = asyncio.Semaphore(_MAX_CONCURRENT)


class SmartTagDeduplicator:
    """
    智能标签去重器 - 复用自 document_analysis_service.py
    
    基于 Jaccard 相似度 + 同义词词典
    """
    
    def __init__(self):
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
            "transformer": {"transformer", "注意力机制", "自注意力"},
            "rag": {"rag", "检索增强生成", "retrieval augmented generation"},
            "embedding": {"embedding", "嵌入", "向量化"},
        }
    
    def normalize(self, tag: str) -> set:
        """标签归一化：分词 + 扩展同义词"""
        tokens = set(tag.lower().split())
        expanded = set(tokens)
        
        for token in tokens:
            if token in self.synonyms:
                expanded.update(self.synonyms[token])
        
        return expanded
    
    def is_duplicate(self, tag_a: str, tag_b: str, threshold: float = 0.6) -> bool:
        """判断两个标签是否重复"""
        # 1. 精确匹配
        if tag_a.lower() == tag_b.lower():
            return True
        
        # 2. 归一化
        set_a = self.normalize(tag_a)
        set_b = self.normalize(tag_b)
        
        # 3. Jaccard 相似度
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
    
    def deduplicate_list(self, tags: list[str]) -> list[str]:
        """列表内去重"""
        unique = []
        for tag in tags:
            tag = tag.strip()
            if not tag:
                continue
            
            is_dup = False
            for existing in unique:
                if self.is_duplicate(tag, existing):
                    is_dup = True
                    break
            
            if not is_dup:
                unique.append(tag)
        
        return unique


class SemanticTagDeduplicator:
    """
    基于语义 Embedding 的标签去重器
    
    使用 cosine 相似度进行深度语义去重
    """
    
    def __init__(self, embd_model, threshold: float = 0.85):
        """
        Args:
            embd_model: Embedding 模型
            threshold: 语义相似度阈值（0.0-1.0）
        """
        self.embd_model = embd_model
        self.threshold = threshold
    
    async def deduplicate_list(self, tags: list[str]) -> list[str]:
        """基于语义相似度去重"""
        if len(tags) <= 1:
            return tags
        
        # 过滤空值
        valid_tags = [t.strip() for t in tags if t.strip()]
        if not valid_tags:
            return []
        
        try:
            # 生成 embeddings
            embeddings, _ = await thread_pool_exec(
                lambda: self.embd_model.encode(valid_tags)
            )
            
            # 计算相似度矩阵
            sim_matrix = cosine_similarity(embeddings)
            
            # 去重：保留第一个，移除后续相似的
            keep_indices = []
            for i in range(len(valid_tags)):
                is_duplicate = False
                for j in keep_indices:
                    if sim_matrix[i][j] > self.threshold:
                        is_duplicate = True
                        break
                
                if not is_duplicate:
                    keep_indices.append(i)
            
            return [valid_tags[i] for i in keep_indices]
            
        except Exception as e:
            logger.error(f"Semantic deduplication failed: {e}")
            # 降级到简单去重
            return list(dict.fromkeys(valid_tags))


class PipelineAnalysisService:
    """
    Pipeline 文档分析服务
    
    集成 core/flow 下的所有组件：
    - Parser: 文档解析
    - TitleChunker: 按标题分层
    - TokenChunker: 智能切片（带重叠）
    - RAPTOR: 聚类递归摘要
    - Extractor: 灵活元数据提取
    """
    
    def __init__(self, db: Session, tenant_id: str):
        self.db = db
        self.tenant_id = tenant_id
        self.tw = TermWeightDealer()
        self.smart_deduplicator = SmartTagDeduplicator()
    
    async def analyze_document(
        self,
        doc_id: str | None = None,
        file=None,
        filename: str | None = None,
        kb_id: str | None = None,
        parse_method: str = "auto",
        output_format: str = "json",
        processing_strategy: str = "auto",
        hierarchical_config: dict | None = None,
        splitter_config: dict | None = None,
        raptor_config: dict | None = None,
        metadata_fields: list[dict] | None = None,
        dedup_strategy: str = "smart",
        use_cache: bool = True
    ) -> dict:
        """
        文档分析主入口（使用 core/flow 逻辑）
        
        Args:
            doc_id: 文档ID（可选）
            file: 上传文件（可选）
            filename: 文件名
            kb_id: 知识库ID
            parse_method: 解析方法 (auto | deepdoc | plain_text | ocr | vlm)
            output_format: 输出格式 (json | text | markdown | html)
            processing_strategy: 处理策略 (auto | hierarchical | raptor | hybrid | simple)
            hierarchical_config: TitleChunker 配置（请求字段名保留 hierarchical_config）
            splitter_config: TokenChunker 配置（请求字段名保留 splitter_config）
            raptor_config: RAPTOR 配置
            metadata_fields: 元数据字段配置列表
            dedup_strategy: 去重策略 (smart | semantic | none)
            use_cache: 是否使用缓存
        """
        start_time = time.time()
        title_chunker_config = hierarchical_config
        token_chunker_config = splitter_config
        
        # 1. 获取文档 chunks（使用 core/flow 逻辑）
        chunks = await self._get_document_chunks(
            doc_id, 
            file, 
            filename, 
            kb_id,
            token_chunker_config=token_chunker_config,
            parse_method=parse_method
        )
        
        if not chunks:
            raise ValueError("No chunks found")
        
        logger.info(f"Got {len(chunks)} chunks with flow parser")
        
        # 标记使用了 TokenChunker
        components_used = ["Parser", "TokenChunker"]
        
        # 2. 策略选择
        if processing_strategy == "auto":
            strategy = self._auto_select_strategy(chunks, title_chunker_config)
            logger.info(f"Auto-selected strategy: {strategy}")
        else:
            strategy = processing_strategy
        
        # 3. 根据策略处理 chunks
        processed_data = await self._process_with_strategy(
            chunks=chunks,
            strategy=strategy,
            title_chunker_config=title_chunker_config,
            token_chunker_config=token_chunker_config,
            raptor_config=raptor_config,
            components_used=components_used
        )
        
        # 4. 提取元数据
        metadata_configs = metadata_fields or self._get_default_metadata_fields()
        
        metadata = await self._extract_metadata(
            processed_data=processed_data,
            metadata_configs=metadata_configs,
            dedup_strategy=dedup_strategy
        )
        
        # 5. 构建响应
        processing_time = time.time() - start_time
        
        result = {
            "metadata": metadata,
            "processing_info": {
                "strategy_used": strategy,
                "chunk_count": len(chunks),
                "processing_time_seconds": round(processing_time, 2),
                "components_used": components_used,
                "dedup_strategy": dedup_strategy
            }
        }
        
        # 添加结构信息（如果使用了 TitleChunker）
        if "structure" in processed_data:
            result["structure"] = processed_data["structure"]
        
        # 添加聚类信息（如果使用了 RAPTOR）
        if "cluster_count" in processed_data:
            result["processing_info"]["cluster_count"] = processed_data["cluster_count"]
        
        return result
    
    async def _get_document_chunks(
        self,
        doc_id: str | None,
        file=None,
        filename: str | None = None,
        kb_id: str | None = None,
        token_chunker_config: dict | None = None,
        parse_method: str = "auto"
    ) -> list[dict]:
        """获取文档 chunks"""
        # 参数验证
        if not doc_id and not file:
            raise ValueError("Must provide either doc_id or file")
        
        if doc_id and file:
            raise ValueError("Cannot provide both doc_id and file")
        
        if doc_id and not kb_id:
            raise ValueError("kb_id is required when using doc_id")
        
        # 场景1: 直传文件（使用 core/flow 逻辑）
        if file:
            return await self._parse_uploaded_file(
                file, 
                filename,
                parse_method=parse_method,
                output_format="json",
                token_chunker_config=token_chunker_config
            )
        
        # 场景2: 已上传文档
        if doc_id:
            doc = DocumentService.get_by_id(self.db, doc_id)
            if not doc:
                raise ValueError(f"Document {doc_id} not found")
            
            # 尝试从向量库获取（已向量化）
            try:
                from core.nlp import search
                from api.db.services.knowledgebase_service import KnowledgebaseService
                
                kb = KnowledgebaseService.get_by_id(self.db, kb_id)
                if not kb:
                    raise ValueError(f"Knowledgebase {kb_id} not found")
                
                # 从 Milvus 获取
                query_result = settings.docStoreConn.search(
                    {"doc_id": doc_id},
                    search.index_name(self.tenant_id),
                    kb_id,
                    page=1,
                    size=10000
                )
                
                if query_result.total > 0:
                    chunks = []
                    for chunk_id in query_result.ids:
                        chunk_data = query_result.field[chunk_id]
                        chunks.append({
                            "content_with_weight": chunk_data.get("content_with_weight", ""),
                            "content_ltks": chunk_data.get("content_ltks", ""),
                            "important_kwd": chunk_data.get("important_kwd", []),
                            "doc_id": chunk_data.get("doc_id", ""),
                        })
                    
                    if chunks:
                        logger.info(f"Loaded {len(chunks)} chunks from Milvus")
                        return chunks
                        
            except Exception as e:
                logger.warning(f"Failed to load from Milvus: {e}")
            
            # 降级：重新解析
            return await self._parse_document(doc_id)
        
        return []
    
    async def _parse_uploaded_file(
        self, 
        file, 
        filename: str | None,
        parse_method: str = "auto",
        output_format: str = "json",
        token_chunker_config: dict | None = None
    ) -> list[dict]:
        """
        使用 core/flow 逻辑解析上传文件（保留位置信息）
        
        Args:
            file: 上传文件对象
            filename: 文件名
            parse_method: 解析方法（auto/deepdoc/plain_text等）
            output_format: 输出格式
            token_chunker_config: TokenChunker 配置
        
        Returns:
            chunks with positions and images
        """
        # 获取文件名
        if hasattr(file, 'filename'):
            fname = file.filename
        elif filename:
            fname = filename
        else:
            raise ValueError("filename required")
        
        # 读取文件内容
        if hasattr(file, 'read'):
            if asyncio.iscoroutinefunction(file.read):
                file_content = await file.read()
            else:
                file_content = await thread_pool_exec(file.read)
        else:
            raise ValueError("file must be readable")
        
        try:
            # 使用 core/flow 逻辑解析
            from core.flow.utils import parse_file, split_chunks
            
            # 1. 解析文件（保留结构）
            # 构建配置（参考 core/flow/parser 的 setups 结构）
            pdf_config = {
                "parse_method": parse_method if parse_method in ["deepdoc", "plain_text", "mineru"] else (
                    parse_method if parse_method not in ["auto", "ocr", "vlm"] else "deepdoc"
                ),
                "output_format": output_format,
                "lang": "Chinese",
                "remove_toc": False,
            }
            image_config = {
                "parse_method": parse_method if parse_method in ["ocr", "vlm"] else "ocr",
                "llm_name": None,
                "lang": "Chinese",
            }
            word_config = {
                "output_format": output_format if output_format in ["json", "markdown"] else "json",
                "remove_toc": False,
            }
            markdown_config = {
                "output_format": output_format if output_format in ["json", "text"] else "json",
                "remove_toc": False,
            }
            code_config = {
                "output_format": output_format if output_format in ["json", "text"] else "text",
            }
            html_config = {
                "output_format": output_format if output_format in ["json", "text"] else "text",
                "remove_toc": False,
            }
            
            parsed_result = await parse_file(
                filename=fname,
                binary=file_content,
                tenant_id=self.tenant_id,
                pdf_config=pdf_config,
                image_config=image_config,
                word_config=word_config,
                markdown_config=markdown_config,
                code_config=code_config,
                html_config=html_config,
            )
            
            logger.info(f"Parsed with flow: format={parsed_result.get('output_format')}")
            
            # 2. 切分（保留位置）
            if token_chunker_config is None:
                token_chunker_config = {}
            
            chunk_token_size = token_chunker_config.get("chunk_token_size", 512)
            delimiters = token_chunker_config.get("delimiters")
            overlapped_percent = token_chunker_config.get("overlapped_percent", 0.1)  # 默认 10% 重叠
            
            chunked_result = await split_chunks(
                parsed_result=parsed_result,
                chunk_token_size=chunk_token_size,
                delimiters=delimiters,
                overlapped_percent=overlapped_percent
            )
            
            # 3. 转换格式
            chunks = []
            for c in chunked_result:
                chunk_dict = {
                    "content_with_weight": c.get("text", ""),
                    "content_ltks": c.get("text", "")
                }

                for field in ("doc_type_kwd", "position_int", "page_num_int", "top_int", "img_id"):
                    if field in c:
                        chunk_dict[field] = c[field]
                
                # 保留位置信息
                if "positions" in c:
                    chunk_dict["positions"] = c["positions"]
                elif "position_int" in c:
                    chunk_dict["positions"] = c["position_int"]
                
                # 保留图片
                if "image" in c and c["image"]:
                    chunk_dict["image"] = c["image"]
                
                chunks.append(chunk_dict)
            
            logger.info(f"Parsed {len(chunks)} chunks from uploaded file (overlap={overlapped_percent})")
            
            return chunks
        
        except Exception as e:
            logger.exception(f"Failed to parse file with flow: {e}")
            raise
    
    async def _parse_uploaded_file_old(self, file, filename: str | None) -> list[dict]:
        """旧的解析方法（备用）"""
        import tempfile
        import os
        
        # 获取文件名
        if hasattr(file, 'filename'):
            fname = file.filename
        elif filename:
            fname = filename
        else:
            raise ValueError("filename required")
        
        # 读取文件内容
        if hasattr(file, 'read'):
            if asyncio.iscoroutinefunction(file.read):
                file_content = await file.read()
            else:
                file_content = await thread_pool_exec(file.read)
        else:
            raise ValueError("file must be readable")
        
        # 临时保存
        file_type = fname.split(".")[-1].lower() if "." in fname else ""
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{file_type}") as temp_file:
            temp_file.write(file_content)
            temp_path = temp_file.name
        
        try:
            # 获取解析器
            module, _ = DocumentService._resolve_parser_for_filename(fname, None)
            
            def _noop(prog=None, msg=""):
                return None
            
            # 执行切片
            result = await thread_pool_exec(
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
                            "content_ltks": str(item)
                        })
            
            return chunks
            
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
    
    async def _parse_document(self, doc_id: str) -> list[dict]:
        """重新解析文档"""
        chunks = DocumentService.preview_document_chunks(
            self.db,
            doc_id=doc_id,
            parser_config_override=None,
            limit=None
        )
        
        # 转换为统一格式
        result = []
        for chunk in chunks:
            if isinstance(chunk, dict):
                result.append(chunk)
            else:
                result.append({
                    "content_with_weight": str(chunk),
                    "content_ltks": str(chunk)
                })
        
        return result
    
    def _auto_select_strategy(
        self,
        chunks: list[dict],
        title_chunker_config: dict | None
    ) -> str:
        """
        自动选择处理策略
        
        Args:
            chunks: 文档切片列表
            title_chunker_config: TitleChunker 配置
            
        Returns:
            策略名称
        """
        chunk_count = len(chunks)
        has_structure = self._detect_hierarchical_structure(chunks, title_chunker_config)
        is_long = chunk_count > 50
        
        if has_structure and is_long:
            return "hybrid"  # TitleChunker + RAPTOR
        elif has_structure:
            return "hierarchical"  # 只用 TitleChunker
        elif is_long:
            return "raptor"  # 只用 RAPTOR
        else:
            return "simple"  # 直接处理
    
    def _detect_hierarchical_structure(
        self,
        chunks: list[dict],
        title_chunker_config: dict | None
    ) -> bool:
        """
        检测文档是否有层次结构
        
        Args:
            chunks: 文档切片
            title_chunker_config: TitleChunker 配置（包含正则列表）
            
        Returns:
            是否有层次结构
        """
        if not title_chunker_config or not title_chunker_config.get("levels"):
            # 使用默认规则检测
            default_patterns = [
                r"^#\s+",  # Markdown 一级标题
                r"^第[一二三四五六七八九十百]+章",  # 中文章节
                r"^Chapter\s+\d+",  # 英文章节
                r"^\d+\.\s+[^\d]",  # 数字标题
            ]
        else:
            # 使用用户配置的规则
            levels = title_chunker_config.get("levels", [])
            default_patterns = []
            for level_patterns in levels:
                default_patterns.extend(level_patterns)
        
        # 检查前100个chunks
        match_count = 0
        check_limit = min(100, len(chunks))
        
        for chunk in chunks[:check_limit]:
            text = chunk.get("content_with_weight", "")
            if not text:
                continue
            
            # 检查是否匹配任何标题pattern
            for pattern in default_patterns:
                if re.search(pattern, text, re.MULTILINE):
                    match_count += 1
                    break
        
        # 如果超过10%的chunks匹配标题pattern，认为有结构
        has_structure = match_count / check_limit > 0.1 if check_limit > 0 else False
        
        logger.info(f"Structure detection: {match_count}/{check_limit} matches, has_structure={has_structure}")
        
        return has_structure
    
    async def _process_with_strategy(
        self,
        chunks: list[dict],
        strategy: str,
        title_chunker_config: dict | None,
        token_chunker_config: dict | None,
        raptor_config: dict | None,
        components_used: list[str]
    ) -> dict:
        """
        根据策略处理文档
        
        Returns:
            {
                "summaries": [...],  # 用于元数据提取的文本列表
                "structure": {...},  # 可选：结构信息
                "cluster_count": N   # 可选：聚类数量
            }
        """
        if strategy == "simple":
            # 直接使用原始 chunks
            return {
                "summaries": [c.get("content_with_weight", "") for c in chunks]
            }
        
        elif strategy == "hierarchical":
            # 使用 TitleChunker
            components_used.append("TitleChunker")
            return await self._hierarchical_merge(chunks, title_chunker_config)
        
        elif strategy == "raptor":
            # 使用 RAPTOR
            components_used.append("RAPTOR")
            return await self._raptor_cluster(chunks, raptor_config)
        
        elif strategy == "hybrid":
            # 混合：先层次化，再 RAPTOR
            components_used.extend(["TitleChunker", "RAPTOR"])
            
            # 先按标题分组
            hierarchical_result = await self._hierarchical_merge(chunks, title_chunker_config)
            
            # 对每个章节独立运行 RAPTOR
            all_summaries = []
            for chapter in hierarchical_result.get("chapters", []):
                chapter_chunks = chapter["chunks"]
                
                if len(chapter_chunks) > 10:
                    # 章节较长，使用 RAPTOR
                    raptor_result = await self._raptor_cluster(chapter_chunks, raptor_config)
                    all_summaries.extend(raptor_result["summaries"])
                else:
                    # 章节较短，直接使用
                    all_summaries.extend([c.get("content_with_weight", "") for c in chapter_chunks])
            
            return {
                "summaries": all_summaries,
                "structure": hierarchical_result.get("structure"),
                "chapter_count": len(hierarchical_result.get("chapters", []))
            }
        
        else:
            raise ValueError(f"Unknown strategy: {strategy}")
    
    async def _hierarchical_merge(
        self,
        chunks: list[dict],
        config: dict | None
    ) -> dict:
        """
        层次化合并 - 参考 core/flow/chunker/title_chunker/
        
        Args:
            chunks: 文档切片
            config: {
                "levels": [[pattern1, pattern2], [pattern3]],
                "hierarchy": 1
            }
        """
        from core.flow.utils import hierarchical_merge

        # 默认配置
        if not config:
            config = {
                "levels": [
                    ["^#\\s+", "^第[一二三四五六七八九十百]+章"],
                    ["^##\\s+", "^\\d+\\.\\s+"]
                ],
                "hierarchy": 1
            }
        
        levels = config.get("levels")
        hierarchy_level = config.get("hierarchy", 1)
        method = config.get("method", "hierarchy")
        include_heading_content = config.get("include_heading_content", False)

        result = await hierarchical_merge(
            chunks=chunks,
            levels=levels,
            hierarchy=hierarchy_level,
            method=method,
            include_heading_content=include_heading_content,
        )
        chapters = result.get("chapters", [])
        
        # 构建结构信息
        structure = {
            "chapters": [
                {
                    "title": ch["title"],
                    "level": 0,  # 简化，都标记为0级
                    "chunk_range": [ch["chunk_indices"][0], ch["chunk_indices"][-1]]
                    if ch["chunk_indices"] else [0, 0]
                }
                for ch in chapters
            ]
        }
        
        logger.info(f"TitleChunker: {len(chapters)} chapters identified")
        
        return {
            "chapters": chapters,
            "summaries": [ch["text"] for ch in chapters],
            "structure": structure
        }
    
    async def _smart_split(
        self,
        chunks: list[dict],
        config: dict | None
    ) -> list[dict]:
        """
        智能切片 - 参考 core/flow/chunker/token_chunker.py
        
        Args:
            chunks: 原始切片
            config: {
                "chunk_token_size": 512,
                "delimiters": ["\n\n", "\n", "。"],
                "overlapped_percent": 0.1
            }
        """
        # 默认配置
        if not config:
            config = {
                "chunk_token_size": 512,
                "delimiters": ["\n\n", "\n", "。"],
                "overlapped_percent": 0.1
            }
        
        chunk_token_size = config.get("chunk_token_size", 512)
        delimiters = config.get("delimiters", ["\n\n", "\n", "。"])
        overlapped_percent = config.get("overlapped_percent", 0.1)
        children_delimiters = config.get("children_delimiters")
        table_context_size = config.get("table_context_size", 0)
        image_context_size = config.get("image_context_size", 0)

        from core.flow.utils import split_chunks

        parsed_result = {
            "output_format": "json",
            "json": [
                {
                    **c,
                    "text": c.get("text") or c.get("content_with_weight", ""),
                    "doc_type_kwd": c.get("doc_type_kwd", "text"),
                }
                for c in chunks
            ],
        }
        split_result = await split_chunks(
            parsed_result=parsed_result,
            chunk_token_size=chunk_token_size,
            delimiters=delimiters,
            overlapped_percent=overlapped_percent,
            children_delimiters=children_delimiters,
            table_context_size=table_context_size,
            image_context_size=image_context_size,
        )

        # 转换为标准格式
        result_chunks = []
        for chunk in split_result:
            text = chunk.get("text", "")
            if not text.strip():
                continue
            result_chunk = {
                "content_with_weight": text.strip(),
                "content_ltks": text.strip(),
            }
            for field in ("doc_type_kwd", "position_int", "page_num_int", "top_int", "img_id", "mom"):
                if field in chunk:
                    result_chunk[field] = chunk[field]
            if "positions" in chunk:
                result_chunk["positions"] = chunk["positions"]
            elif "position_int" in chunk:
                result_chunk["positions"] = chunk["position_int"]
            if "image" in chunk and chunk["image"]:
                result_chunk["image"] = chunk["image"]
            result_chunks.append(result_chunk)

        logger.info(f"TokenChunker: {len(chunks)} → {len(result_chunks)} chunks (overlap: {overlapped_percent*100}%)")
        
        return result_chunks
    
    async def _raptor_cluster(
        self,
        chunks: list[dict],
        config: dict | None
    ) -> dict:
        """
        RAPTOR 聚类和摘要 - 复用 core/raptor.py
        
        Args:
            chunks: 文档切片
            config: {
                "max_cluster": 64,
                "max_token": 512,
                "threshold": 0.1,
                "random_seed": 42
            }
        """
        # 默认配置
        if not config:
            config = {
                "max_cluster": 64,
                "max_token": 512,
                "threshold": 0.1,
                "random_seed": 42
            }
        
        # 获取 LLM 和 Embedding 模型
        chat_config = get_tenant_default_model_by_type(self.db, self.tenant_id, LLMType.CHAT)
        llm_model = LLMBundle(self.db, self.tenant_id, chat_config)
        embd_config = get_tenant_default_model_by_type(self.db, self.tenant_id, LLMType.EMBEDDING)
        embd_model = LLMBundle(self.db, self.tenant_id, embd_config)
        
        # 创建 RAPTOR 实例
        raptor = Raptor(
            max_cluster=config.get("max_cluster", 64),
            llm_model=llm_model,
            embd_model=embd_model,
            prompt=config.get("prompt") or "Please summarize the following content:\n{cluster_content}",
            max_token=config.get("max_token", 512),
            threshold=config.get("threshold", 0.1)
        )
        
        # 准备数据：需要 (text, embedding) 对
        raptor_inputs = []
        for chunk in chunks:
            text = chunk.get("content_with_weight", "")
            if not text:
                continue
            
            # 如果已有 embedding，直接使用
            if "embeddings" in chunk and chunk["embeddings"]:
                embd = chunk["embeddings"]
            else:
                # 生成 embedding
                embd, _ = await thread_pool_exec(
                    lambda: embd_model.encode([text])
                )
                embd = embd[0] if embd else []
            
            raptor_inputs.append((text, embd))
        
        logger.info(f"Running RAPTOR on {len(raptor_inputs)} chunks")
        
        cluster_results = await raptor(
            raptor_inputs,
            random_state=config.get("random_seed", 42)
        )
        
        # 提取聚类摘要
        summaries = [text for text, _ in cluster_results]
        
        logger.info(f"RAPTOR generated {len(summaries)} cluster summaries")
        
        return {
            "summaries": summaries,
            "cluster_count": len(summaries)
        }
    
    async def _extract_metadata(
        self,
        processed_data: dict,
        metadata_configs: list[dict],
        dedup_strategy: str
    ) -> dict:
        """
        提取元数据
        
        Args:
            processed_data: 处理后的数据（包含 summaries）
            metadata_configs: 元数据字段配置
            dedup_strategy: 去重策略
        """
        summaries = processed_data.get("summaries", [])
        
        if not summaries:
            logger.warning("No summaries to extract metadata from")
            return {}
        
        # 使用 BatchMetadataExtractor 并行提取所有字段
        batch_extractor = BatchMetadataExtractor(self.db, self.tenant_id)
        metadata = await batch_extractor.extract_multiple_fields(
            summaries,
            metadata_configs
        )
        
        # 去重处理（针对列表类型的字段）
        if dedup_strategy != "none":
            metadata = await self._apply_deduplication(metadata, dedup_strategy)
        
        return metadata
    
    async def _apply_deduplication(
        self,
        metadata: dict,
        strategy: str
    ) -> dict:
        """
        应用去重策略
        
        Args:
            metadata: 元数据字典
            strategy: smart | semantic
        """
        result = {}
        
        for field_name, value in metadata.items():
            # 只对列表类型去重
            if not isinstance(value, list):
                result[field_name] = value
                continue
            
            if strategy == "smart":
                # 使用 SmartTagDeduplicator
                deduped = self.smart_deduplicator.deduplicate_list(value)
                result[field_name] = deduped
                
            elif strategy == "semantic":
                # 使用 Semantic 去重
                embd_dedup_config = get_tenant_default_model_by_type(self.db, self.tenant_id, LLMType.EMBEDDING)
                embd_model = LLMBundle(self.db, self.tenant_id, embd_dedup_config)
                semantic_dedup = SemanticTagDeduplicator(embd_model)
                deduped = await semantic_dedup.deduplicate_list(value)
                result[field_name] = deduped
                
            else:
                result[field_name] = value
        
        return result
    
    def _get_default_metadata_fields(self) -> list[dict]:
        """
        获取默认元数据字段配置
        """
        return [
            {
                "field_name": "semantic_tags",
                "prompt": """## 角色
你是一个文本分析专家。

## 任务
从给定的文本内容中提取最重要的关键词/短语。

## 要求
- 总结文本内容，提取最重要的 5 个关键词/短语。
- 关键词必须彼此独立，语义上不能有重叠。
- 缩写词与全称合并（优先使用全称）。
- 关键词必须与给定文本内容使用相同的语言。
- 关键词之间用英文逗号分隔。
- 只输出关键词，不要输出其他内容。""",
                "aggregate": "merge",
                "temperature": 0.2,
                "max_tokens": 100
            },
            {
                "field_name": "short_summary",
                "prompt": """## 角色
你是一个文档摘要专家。

## 任务
为给定的文本内容生成简洁的摘要。

## 要求
- 摘要长度控制在 150-200 字。
- 准确捕捉主题和关键要点。
- 保持内容连贯、结构清晰。
- 聚焦最重要的信息。
- 摘要必须与给定文本内容使用相同的语言。
- 只输出摘要文本，不要输出其他内容。""",
                "aggregate": "concat",
                "temperature": 0.3,
                "max_tokens": 400
            }
        ]
