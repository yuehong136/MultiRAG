import json
import logging
import re
import math
from collections import OrderedDict, defaultdict
from dataclasses import dataclass

import numpy as np
from pymilvus import AnnSearchRequest, WeightedRanker

from api.db.db_models import db_connection
from core.nlp import rag_tokenizer, query, is_english
from common import settings
from common.doc_store.doc_store_base import (
    DocStoreConnection,
    MatchDenseExpr,
    FusionExpr,
    OrderByExpr,
)
from common.string_utils import remove_redundant_spaces
from common.float_utils import get_float
from common.misc_utils import thread_pool_exec
from common.constants import TAG_FLD, PAGERANK_FLD


def index_name(uid, kb_names=None):
    # return [f"multirag_{uid}_{kb_name}" for kb_name in kb_names]
    if kb_names is None:
        kb_names = []
    elif isinstance(kb_names, str):
        kb_names = [kb_names]

    # 无知识库名时兼容旧调用，避免返回 None
    if not kb_names:
        if isinstance(uid, str):
            return [f"multirag_{uid}"]
        if isinstance(uid, list):
            return [f"multirag_{u}" for u in uid if isinstance(u, str) and u]
        return []

    # 情况1：单个 UID + 多个 KB
    if isinstance(uid, str):
        return [f"multirag_{uid}_{kb}" for kb in kb_names]

    # 情况2：UID 列表
    if isinstance(uid, list):
        uid = [u for u in uid if isinstance(u, str) and u]
        if not uid:
            return []

        # 完全一一对应
        if len(uid) == len(kb_names):
            return [f"multirag_{u}_{kb}" for u, kb in zip(uid, kb_names)]

        # 单租户 + 多 KB
        if len(uid) == 1:
            return [f"multirag_{uid[0]}_{kb}" for kb in kb_names]

        # 多租户 + 单 KB
        if len(kb_names) == 1:
            return [f"multirag_{u}_{kb_names[0]}" for u in uid]

        # 多租户 + 多 KB（数量不一致），兜底返回笛卡尔积
        return [f"multirag_{u}_{kb}" for u in uid for kb in kb_names]

    return []



def index_name_one(uid, kb_name):
    return f"multirag_{uid}_{kb_name}"


class Dealer:
    ARRAY_FILTER_FIELDS = {"important_kwd", "question_kwd", "entities_kwd"}

    def __init__(self, dataStore: DocStoreConnection):
        self.qryr = query.FulltextQueryer()
        self.dataStore = dataStore
        
        # 基础搜索字段（ES/OpenSearch/Infinity 兼容）
        base_fields = [
            "title_tks^10",
            "title_sm_tks^5",
            "important_kwd^30",
            "important_tks^20",
            "question_tks^20",
            "content_ltks^2",
            "content_sm_ltks"]
        
        # Milvus 额外支持 content_with_weight 搜索（ES 中该字段不可索引）
        if dataStore.db_type() == "milvus":
            base_fields.append("content_with_weight")
        
        self.qryr.flds = base_fields

    @dataclass
    class SearchResult:
        total: int
        ids: list[str]
        query_vector: list[float] = None
        field: dict | None = None
        highlight: dict | None = None
        aggregation: list | dict | None = None
        keywords: list[str] | None = None
        group_docs: list[list] | None = None

    @dataclass
    class QueryResult:
        total: int
        ids: list[str]
        field: dict | None = None
        aggregation: list | dict | None = None
        keywords: list[str] | None = None
        group_docs: list[list] | None = None

    # def get_vector(self, collection_name, txt, emb_mdl, topk=10, similarity=0.1):
    async def get_vector(self, txt, emb_mdl, topk=10, similarity=0.1):
        qv, _ = await thread_pool_exec(emb_mdl.encode_queries, txt)
        shape = np.array(qv).shape
        if len(shape) > 1:
            raise Exception(f"Dealer.get_vector returned array's shape {shape} doesn't match expectation(exact one dimension).")

        embedding_data = [get_float(v) for v in qv]
        vector_dim = len(embedding_data)

        # # 修改点：先使用标准向量字段名，避免报错
        # vector_column_name = "vector"
        #
        # # 再检查维度特定字段是否存在于集合中，如果存在则使用它
        # try:
        #     schema = self.dataStore.describe_collection(collection_name)
        #     for field in schema['fields']:
        #         if field['name'] == f"q_{vector_dim}_vec":
        #             vector_column_name = f"q_{vector_dim}_vec"
        #             break
        # except Exception as e:
        #     logging.warning(f"检查字段 q_{vector_dim}_vec 时出错: {str(e)}，将使用默认字段 vector")

        vector_column_name = f"q_{vector_dim}_vec"

        logging.info(f"使用向量字段: {vector_column_name} 进行查询，维度: {vector_dim}")
        return MatchDenseExpr(vector_column_name, embedding_data, 'float', 'cosine', topk, {"similarity": similarity})

    def get_filters(self, req):
        condition = dict()
        for key, field in {"kb_ids": "kb_id", "doc_ids": "doc_id"}.items():
            if key in req and req[key] is not None:
                condition[field] = req[key]
        # TODO(yzc): `available_int` is nullable however infinity doesn't support nullable columns.
        for key in ["knowledge_graph_kwd", "available_int", "entity_kwd", "from_entity_kwd", "to_entity_kwd", "removed_kwd"]:
            if key in req and req[key] is not None:
                condition[key] = req[key]
        if req.get("filter_exp"):
            exp = req["filter_exp"]
            # 如果 filter_exp 中含有 LIKE，就作为 content_with_weight 过滤，
            # 否则走 auth 逻辑
            if "like" in exp.lower():
                condition["content_with_weight"] = exp
            else:
                condition["auth"] = exp
        return condition


    # 构建过滤表达式的辅助方法
    def _build_filter_expr(self, filters):
        """
        将过滤条件字典转换为Milvus过滤表达式

        Args:
            filters: 过滤条件字典

        Returns:
            str: Milvus过滤表达式
        """
        if not filters:
            return ""

        filter_parts = []
        for k, v in filters.items():
            # 跳过 pk 字段和空值
            if k == "pk" or not v:
                continue

            if k == "doc_id":
                # doc_id 字段特殊处理
                if isinstance(v, list):
                    kb_exprs = [f"doc_id == '{kb}'" for kb in v]
                    filter_parts.append(f"({' || '.join(kb_exprs)})")
                else:
                    filter_parts.append(f"doc_id == '{v}'")
            elif k == "available_int":
                # available_int 字段特殊处理
                filter_parts.append(f"available_int != {v - 1}")  # 为了兼容老版本不存在available_int字段才这么写
            elif k == "auth":
                # auth 字段特殊处理 - 直接使用值作为表达式
                filter_parts.append(f"{v}")
            elif k in self.ARRAY_FILTER_FIELDS:
                if isinstance(v, list):
                    values = [f"'{item}'" if isinstance(item, str) else str(item) for item in v]
                    filter_parts.append(f"ARRAY_CONTAINS_ANY({k}, [{','.join(values)}])")
                else:
                    value = f"'{v}'" if isinstance(v, str) else str(v)
                    filter_parts.append(f"ARRAY_CONTAINS({k}, {value})")
            elif isinstance(v, list):
                # 其他字段按类型处理 - 列表
                values = [f"'{item}'" if isinstance(item, str) else str(item) for item in v]
                filter_parts.append(f"{k} in [{','.join(values)}]")
            elif isinstance(v, str):
                # 其他字段按类型处理 - 字符串
                filter_parts.append(f"{k} == '{v}'")
            elif isinstance(v, (int, float)):
                # 其他字段按类型处理 - 数值
                filter_parts.append(f"{k} == {v}")

        return " && ".join(filter_parts) if filter_parts else ""


    async def search(self, req, idx_names: str | list[str],
               kb_ids: list[str],
               emb_mdl=None,
               highlight: bool | list | None = False,
               rank_feature: dict | None = None
               ):
        if highlight is None:
            highlight = False

        """Milvus‑backend search (single‑method refactor)."""
        # ---------- 通用预处理 ----------
        filters = self.get_filters(req)
        pg = int(req.get("page", 1)) - 1
        topk = int(req.get("topk", 1024))
        size = int(req.get("size", topk))
        offset = pg * size
        limit = size

        default_fields = [
            "docnm_kwd", "content_ltks", "kb_id", "img_id", "doc_type_kwd", "title_tks", "important_kwd",
            "position_int", "doc_id", "page_num_int", "top_int", "create_timestamp_flt",
            "knowledge_graph_kwd", "question_kwd", "question_tks", "available_int",
            "content_with_weight", "mom_id", PAGERANK_FLD, TAG_FLD,
        ]
        src: list[str] = list(req.get("fields", default_fields))
        highlight_fields = ["content_ltks", "title_tks"]
        if not highlight:
            highlight_fields = []
        elif isinstance(highlight, list):
            highlight_fields = highlight

        qst: str = req.get("question", "")
        q_vec: list[float] = []
        kwds: set[str] = set()

        # 内部工具: 关键词扩展 & SearchResult 构造
        def _process_keywords(raw_kw: list[str]):
            for k in raw_kw:
                kwds.add(k)
                for tok in rag_tokenizer.fine_grained_tokenize(k).split():
                    if len(tok) >= 2:
                        kwds.add(tok)
            return list(kwds)

        def _build_result(results, kwds: list[str] | None = None):
            total = self.dataStore.get_total(results)
            keywords = _process_keywords(kwds)
            ids = self.dataStore.get_doc_ids(results)
            highlight_rst = self.dataStore.get_highlight(results, keywords, "content_with_weight")
            aggs = self.dataStore.get_aggregation(results, "docnm_kwd")
            fields = self.dataStore.get_fields(results, src + ["_score"])
            return self.SearchResult(
                total=total,
                ids=ids,
                query_vector=q_vec,
                aggregation=aggs,
                highlight=highlight_rst,
                field=fields,
                keywords=keywords,
            )

        # ---------- 无 query: 浏览/排序 ----------
        if not qst:
            order_by = OrderByExpr()
            if req.get("sort"):
                order_by.asc("page_num_int").asc("top_int").desc("create_timestamp_flt")
            res = await thread_pool_exec(self.dataStore.search, src, [], filters, [], order_by, offset, limit, idx_names, kb_ids)
            keywords_raw: list[str] =  []
            return _build_result(res, keywords_raw)

        # ---------- 有 query ----------
        search_mode = req.get("search_mode", "")
        keywords_raw: list[str] = []

        # 公共文本匹配表达式
        match_text, keywords_raw = self.qryr.question(qst, min_match=0.3)

        try:
            # === Hybrid 模式 ===
            if "hybrid" in search_mode and emb_mdl:
                logging.info("执行混合检索…")
                hybrid_params = search_mode["hybrid"]
                weight_dense = hybrid_params.get("weight_dense", 0.7)
                weight_sparse = hybrid_params.get("weight_sparse", 0.3)

                match_dense = await self.get_vector(qst, emb_mdl, topk, req.get("similarity", 0.1))
                q_vec = match_dense.embedding_data
                vector_field = f"q_{len(q_vec)}_vec"
                src.append(vector_field)

                dense_req = AnnSearchRequest(
                    data=[q_vec],
                    anns_field=vector_field,
                    param={"metric_type": "COSINE", "params": {"nprobe": 10}},
                    limit=topk,
                    expr=self._build_filter_expr(filters) if filters else "",
                )
                sparse_req = AnnSearchRequest(
                    data=[qst],
                    anns_field="sparse_vector",
                    param={"metric_type": "BM25", "params": {"drop_ratio_search": 0.1}},
                    limit=topk,
                    expr=self._build_filter_expr(filters) if filters else "",
                )
                ranker = WeightedRanker(weight_dense, weight_sparse)
                results = await thread_pool_exec(
                    self.dataStore.hybrid_search,
                    collection_name=idx_names,
                    reqs=[dense_req, sparse_req],
                    ranker=ranker,
                    limit=topk,
                    output_fields=src,
                    offset=offset,
                )
                return _build_result(results, keywords_raw)

            # === Sparse 模式 ===
            if "sparse" in search_mode:
                logging.info("执行全文检索…")
                results = await thread_pool_exec(
                    self.dataStore.search_by_milvus,
                    collection_name=idx_names,
                    data=[qst],
                    anns_field="sparse_vector",
                    limit=topk,
                    output_fields=src,
                )
                return _build_result(results, keywords_raw)

            # # === Dense 模式 ===
            # if "dense" in search_mode and emb_mdl:
            #     logging.info("执行向量检索…")
            #     match_dense = self.get_vector(qst, emb_mdl, topk, req.get("similarity", 0.1))
            #     q_vec = match_dense.embedding_data
            #     vector_field = f"q_{len(q_vec)}_vec"
            #     src.append(vector_field)
            #
            #     results = self.dataStore.search_by_milvus(
            #         collection_name=idx_names,
            #         data=[q_vec],
            #         anns_field=vector_field,
            #         limit=topk,
            #         output_fields=src,
            #         param={"metric_type": "COSINE", "params": {"nprobe": 10}},
            #     )
            #     return _build_result(results, keywords_raw)

            # === Fusion / Text-only 模式 ===
            order_by = OrderByExpr()
            match_exprs = [match_text]
            logging.info("执行向量融合检索「默认」")
            if emb_mdl:
                match_dense = await self.get_vector(qst, emb_mdl, topk, req.get("similarity", 0.1))
                q_vec = match_dense.embedding_data
                if not settings.DOC_ENGINE_INFINITY:
                    src.append(f"q_{len(q_vec)}_vec")
                fusion_expr = FusionExpr("weighted_sum", topk, {"weights": "0.05,0.95"})
                match_exprs = [match_text, match_dense, fusion_expr]

            res = await thread_pool_exec(
                self.dataStore.search,
                src,
                highlight_fields,
                filters,
                match_exprs,
                order_by,
                offset,
                limit,
                idx_names,
                kb_ids,
                rank_feature=rank_feature,
            )
            total = self.dataStore.get_total(res)

            # 若召回为 0 且使用嵌入，放宽阈值重试一次
            if emb_mdl and total == 0:
                match_text_low, _ = self.qryr.question(qst, min_match=0.1)
                filters.pop("doc_ids", None)
                match_dense.extra_options["similarity"] = 0.17
                res = await thread_pool_exec(
                    self.dataStore.search,
                    src,
                    highlight_fields,
                    filters,
                    [match_text_low, match_dense, fusion_expr],
                    order_by,
                    offset,
                    limit,
                    idx_names,
                    kb_ids,
                    rank_feature=rank_feature,
                )
            return _build_result(res, keywords_raw)

        except Exception as exc:  # noqa: BLE001
            logging.error("Search failed: %s", exc, exc_info=True)
            # 极端情况 fallback 为空结果，保持返回格式
            return self.SearchResult(
                total=0,
                ids=[],
                query_vector=q_vec,
                aggregation={},
                highlight={},
                field={},
                keywords=list(kwds),
            )

    def get_aggregation(self, res, g):
        if "aggregations" not in res or "aggs_" + g not in res["aggregations"]:
            return
        bkts = res["aggregations"]["aggs_" + g]["buckets"]
        return [(b["key"], b["doc_count"]) for b in bkts]

    def get_highlight(self, res, keywords, fieldnm):
        ans = {}
        for d in res[0]:
            # 从字典中提取 'entity' 部分
            entity = d.get('entity', {})
            # 'highlight' 字段可能不存在，因此需要通过 get 方法来访问
            hlts = entity.get("highlight")
            if not hlts:
                continue
            txt = "...".join([a for a in list(hlts.items())[0][1]])

            # 判断文本是否为英文
            if not is_english(txt.split()):
                ans[entity.get("doc_id", "")] = txt
                continue

            # 如果不是英文文本，则获取字段对应的文本
            txt = d["_source"][fieldnm]
            txt = re.sub(r"[\r\n]", " ", txt, flags=re.IGNORECASE | re.MULTILINE)
            txts = []

            # 分割文本并为关键词加上高亮标记
            for t in re.split(r"[.?!;\n]", txt):
                for w in keywords:
                    t = re.sub(r"(^|[ .?/'\"\(\)!,:;-])(%s)([ .?/'\"\(\)!,:;-])" % re.escape(w), r"\1<em>\2</em>\3", t,
                               flags=re.IGNORECASE | re.MULTILINE)
                if not re.search(r"<em>[^<>]+</em>", t, flags=re.IGNORECASE | re.MULTILINE):
                    continue
                txts.append(t)

            # 拼接并返回最终结果
            ans[entity.get("doc_id", "")] = "...".join(txts) if txts else txt

        return ans

    @staticmethod
    def trans2floats(txt):
        return [get_float(t) for t in txt.split("\t")]

    def insert_citations(self, answer, chunks, chunk_v,
                         embd_mdl, tkweight=0.1, vtweight=0.9):
        assert len(chunks) == len(chunk_v)
        if not chunks:
            return answer, set([])
        pieces = re.split(r"(```)", answer)
        if len(pieces) >= 3:
            i = 0
            pieces_ = []
            while i < len(pieces):
                if pieces[i] == "```":
                    st = i
                    i += 1
                    while i < len(pieces) and pieces[i] != "```":
                        i += 1
                    if i < len(pieces):
                        i += 1
                    pieces_.append("".join(pieces[st: i]) + "\n")
                else:
                    # Sentence boundary regex includes Arabic punctuation (، ؛ ؟ ۔)
                    pieces_.extend(
                        re.split(
                            r"([^\|][；。？!！،؛؟۔\n]|[a-z\u0600-\u06FF][.?;!،؛؟][ \n])",
                            pieces[i]))
                    i += 1
            pieces = pieces_
        else:
            # Sentence boundary regex includes Arabic punctuation (، ؛ ؟ ۔)
            pieces = re.split(r"([^\|][；。？!！،؛؟۔\n]|[a-z\u0600-\u06FF][.?;!،؛؟][ \n])", answer)
        for i in range(1, len(pieces)):
            if re.match(r"([^\|][；。？!！،؛؟۔\n]|[a-z\u0600-\u06FF][.?;!،؛؟][ \n])", pieces[i]):
                pieces[i - 1] += pieces[i][0]
                pieces[i] = pieces[i][1:]
        idx = []
        pieces_ = []
        for i, t in enumerate(pieces):
            if len(t) < 5:
                continue
            idx.append(i)
            pieces_.append(t)
        logging.info("{} => {}".format(answer, pieces_))
        if not pieces_:
            return answer, set([])

        ans_v, _ = embd_mdl.encode(pieces_)
        for i in range(len(chunk_v)):
            if len(ans_v[0]) != len(chunk_v[i]):
                chunk_v[i] = [0.0] * len(ans_v[0])
                logging.warning("The dimension of query and chunk do not match: {} vs. {}".format(len(ans_v[0]), len(chunk_v[i])))

        assert len(ans_v[0]) == len(chunk_v[0]), "The dimension of query and chunk do not match: {} vs. {}".format(len(ans_v[0]), len(chunk_v[0]))

        chunks_tks = [rag_tokenizer.tokenize(self.qryr.rmWWW(ck)).split()
                      for ck in chunks]
        cites = {}
        thr = 0.63
        while thr > 0.3 and len(cites.keys()) == 0 and pieces_ and chunks_tks:
            for i, a in enumerate(pieces_):
                sim, tksim, vtsim = self.qryr.hybrid_similarity(ans_v[i],
                                                                chunk_v,
                                                                rag_tokenizer.tokenize(
                                                                    self.qryr.rmWWW(pieces_[i])).split(),
                                                                chunks_tks,
                                                                tkweight, vtweight)
                mx = np.max(sim) * 0.99
                logging.info("{} SIM: {}".format(pieces_[i], mx))
                if mx < thr:
                    continue
                cites[idx[i]] = list(
                    set([str(ii) for ii in range(len(chunk_v)) if sim[ii] > mx]))[:4]
            thr *= 0.8

        res = ""
        seted = set([])
        for i, p in enumerate(pieces):
            res += p
            if i not in idx:
                continue
            if i not in cites:
                continue
            for c in cites[i]:
                assert int(c) < len(chunk_v)
            for c in cites[i]:
                if c in seted:
                    continue
                res += f" [ID:{c}]"
                seted.add(c)

        return res, seted

    def _rank_feature_scores(self, query_rfea, search_res):
        ## For rank feature(tag_fea) scores.
        rank_fea = []
        pageranks = []
        for chunk_id in search_res.ids:
            pageranks.append(search_res.field[chunk_id].get(PAGERANK_FLD, 0))
        pageranks = np.array(pageranks, dtype=float)

        if not query_rfea:
            return np.array([0 for _ in range(len(search_res.ids))]) + pageranks

        q_denor = np.sqrt(np.sum([s * s for t, s in query_rfea.items() if t != PAGERANK_FLD]))
        for i in search_res.ids:
            nor, denor = 0, 0
            if not search_res.field[i].get(TAG_FLD):
                rank_fea.append(0)
                continue
            for t, sc in eval(search_res.field[i].get(TAG_FLD, "{}")).items():
                if t in query_rfea:
                    nor += query_rfea[t] * sc
                denor += sc * sc
            if denor == 0:
                rank_fea.append(0)
            else:
                rank_fea.append(nor / np.sqrt(denor) / q_denor)
        return np.array(rank_fea) * 10. + pageranks

    def rerank(self, sres, query, tkweight=0.3,
               vtweight=0.7, cfield="content_ltks",
               rank_feature: dict | None = None
               ):
        """
        对搜索结果进行重排序

        Args:
            sres: 搜索结果
            query: 查询文本
            tkweight: 词项相似度权重
            vtweight: 向量相似度权重
            cfield: 内容字段名

        Returns:
            重排序后的相似度分数
        """
        _, keywords = self.qryr.question(query)

        # 检查结果是否为空
        if not sres.ids:
            return [], [], []

        # 获取结果中的嵌入向量
        vector_dim = len(sres.query_vector) if sres.query_vector else 0
        logging.info(f"rerank: vector_dim={vector_dim}, sres.ids count={len(sres.ids)}")
        
        if vector_dim == 0:
            logging.warning("rerank: query_vector 为空，无法计算向量相似度")
            # 返回基于文本的相似度
            sim = np.array([sres.field[id].get("_score", 0.0) for id in sres.ids], dtype=np.float64)
            return sim, sim, np.zeros(len(sim), dtype=np.float64)
            
        if vector_dim != 768:
            dim_field = f"q_{vector_dim}_vec"
        else:
            dim_field = "vector"
        
        ins_embd = []
        zero_count = 0
        for i in sres.ids:
            # 优先使用维度特定字段，如果没有则使用标准vector字段
            if dim_field in sres.field[i] and sres.field[i][dim_field]:
                vector = sres.field[i][dim_field]
            elif sres.field[i].get(f"q_{vector_dim}_vec") is None:
                vector = [0.0] * vector_dim
                zero_count += 1
            else:
                vector = sres.field[i].get(f"q_{vector_dim}_vec", [0.0] * vector_dim)

            # 确保向量是列表格式
            if isinstance(vector, str):
                # 如果是字符串格式，尝试解析
                try:
                    vector = [get_float(v) for v in vector.split("\t")]
                except Exception:
                    vector = [0.0] * vector_dim
                    zero_count += 1

            ins_embd.append(vector)
        
        logging.info(f"rerank: 获取到 {len(ins_embd)} 个向量，其中 {zero_count} 个为零向量，dim_field={dim_field}")

        # 处理文本相似度比较所需的token列表
        ins_tw = []
        for i in sres.ids:
            content_ltks = list(OrderedDict.fromkeys(sres.field[i][cfield].split()))
            # 处理 title_tks 字段
            title_tks = []
            if "title_tks" in sres.field[i]:
                if isinstance(sres.field[i]["title_tks"], str):
                    title_tks = [t for t in sres.field[i]["title_tks"].split() if t]
                elif isinstance(sres.field[i]["title_tks"], list):
                    title_tks = [t for t in sres.field[i]["title_tks"] if t]

            # 处理 question_tks 字段
            question_tks = []
            if "question_tks" in sres.field[i]:
                if isinstance(sres.field[i]["question_tks"], str):
                    question_tks = [t for t in sres.field[i]["question_tks"].split() if t]
                elif isinstance(sres.field[i]["question_tks"], list):
                    question_tks = [t for t in sres.field[i]["question_tks"] if t]

            important_kwd = []
            # 处理important_kwd字段，它可能是字符串或列表
            if "important_kwd" in sres.field[i]:
                if isinstance(sres.field[i]["important_kwd"], list):
                    important_kwd = sres.field[i]["important_kwd"]
                elif isinstance(sres.field[i]["important_kwd"], str):
                    if sres.field[i]["important_kwd"]:
                        important_kwd = [sres.field[i]["important_kwd"]]

            # 合并所有token，给不同来源的token加权
            tks = content_ltks + title_tks * 2 + important_kwd * 5 + question_tks * 6
            ins_tw.append(tks)

        ## For rank feature(tag_fea) scores.
        rank_fea = self._rank_feature_scores(rank_feature, sres)

        # 计算混合相似度
        sim, tksim, vtsim = self.qryr.hybrid_similarity(
            sres.query_vector,
            ins_embd,
            keywords,
            ins_tw,
            tkweight,
            vtweight
        )

        return sim + rank_fea, tksim, vtsim

    def rerank_by_model(self, rerank_mdl, sres, query, tkweight=0.3,
                        vtweight=0.7, cfield="content_ltks",
                        rank_feature: dict | None = None):
        _, keywords = self.qryr.question(query)

        for i in sres.ids:
            if isinstance(sres.field[i].get("important_kwd", []), str):
                sres.field[i]["important_kwd"] = [sres.field[i]["important_kwd"]]
        ins_tw = []
        for i in sres.ids:
            content_ltks = sres.field[i][cfield].split()
            title_tks = [t for t in sres.field[i].get("title_tks", "").split() if t]
            important_kwd = sres.field[i].get("important_kwd", [])
            # 将 RepeatedScalarContainer 转换为列表
            if not isinstance(important_kwd, list):
                important_kwd = list(important_kwd)
            tks = content_ltks + title_tks + important_kwd
            ins_tw.append(tks)

        tksim = self.qryr.token_similarity(keywords, ins_tw)
        vtsim, _ = rerank_mdl.similarity(query, [remove_redundant_spaces(" ".join(tks)) for tks in ins_tw])
        ## For rank feature(tag_fea) scores.
        rank_fea = self._rank_feature_scores(rank_feature, sres)

        return tkweight * np.array(tksim) + vtweight * vtsim + rank_fea, tksim, vtsim

    def hybrid_similarity(self, ans_embd, ins_embd, ans, inst):
        return self.qryr.hybrid_similarity(ans_embd,
                                           ins_embd,
                                           rag_tokenizer.tokenize(ans).split(),
                                           rag_tokenizer.tokenize(inst).split())

    async def retrieval(
            self,
            question,
            filter_exp,
            embd_mdl,
            tenant_id,
            kb_names,
            page,
            page_size,
            similarity_threshold=0.2,
            vector_similarity_weight=0.3,
            top=1024,
            doc_ids=None,
            aggs=True,
            rerank_mdl=None,
            highlight=False,
            rank_feature=None,
            search_mode=None,
            kb_ids=None
    ):
        """
        Args:
            kb_names: 知识库名称列表，用于构建索引名称
            kb_ids: 知识库ID列表，用于ES的kb_id过滤条件。如果不提供，则使用kb_names（仅适用于Milvus）
        """
        if rank_feature is None:
            rank_feature = {PAGERANK_FLD: 10}
        if search_mode is None:
            # 密集检索（向量检索）
            search_mode = {
                "dense": {}
            }

            # # 稀疏检索（全文检索）
            # search_mode = {
            #     "sparse": {}
            # }
            #
            # # 混合检索
            # search_mode = {
            #     "hybrid": {
            #         "weight_dense": 0.7,
            #         "weight_sparse": 0.3
            #     }
            # }
        ranks = {"total": 0, "chunks": [], "doc_aggs": {}}
        if not question:
            return ranks

        # Ensure RERANK_LIMIT is multiple of page_size
        RERANK_LIMIT = math.ceil(64/page_size) * page_size if page_size > 1 else 1
        RERANK_LIMIT = max(30, RERANK_LIMIT)
        req = {
            "kb_names": kb_names,
            "doc_ids": doc_ids,
            "page": math.ceil(page_size * page / RERANK_LIMIT),
            "size": RERANK_LIMIT,
            "question": question,
            "vector": True,
            "topk": top,
            "similarity": similarity_threshold,
            "available_int": 1,
            "filter_exp": filter_exp,
            "search_mode": search_mode
        }

        idxnms = index_name(tenant_id, kb_names)
        if not idxnms:
            logging.warning("No valid index names built: tenant_id=%s, kb_names=%s", tenant_id, kb_names)
            return ranks
        
        # 优先使用 kb_ids（知识库ID），如果没有则使用 kb_names（仅适用于Milvus场景）
        search_kb_ids = kb_ids if kb_ids else kb_names

        sres = await self.search(req, idxnms, search_kb_ids, embd_mdl, highlight=highlight, rank_feature=rank_feature)

        if rerank_mdl and sres.total > 0:
            sim, tsim, vsim = await thread_pool_exec(
                self.rerank_by_model,
                rerank_mdl,
                sres,
                question,
                1 - vector_similarity_weight,
                vector_similarity_weight,
                rank_feature=rank_feature
            )
        else:
            # 使用实际的数据库类型判断
            db_type = self.dataStore.db_type()
            logging.info(f"db_type: {db_type}, sres.total: {sres.total}, query_vector len: {len(sres.query_vector) if sres.query_vector else 0}")
            if settings.DOC_ENGINE_INFINITY:
                # Don't need rerank here since Infinity normalizes each way score before fusion.
                sim = [sres.field[id].get("_score", 0.0) for id in sres.ids]
                sim = [s if s is not None else 0.0 for s in sim]
                tsim = sim
                vsim = sim
            elif db_type == "milvus":
                if req.get("search_mode", {}).get("hybrid"):
                    if "distance" in sres.field and isinstance(sres.field["distance"], list):
                        sim = np.array(sres.field["distance"])
                    else:
                        sim = np.array([sres.field[id].get("_score", 0.0) for id in sres.ids], dtype=np.float64)
                    tsim = np.zeros(len(sim), dtype=np.float64)
                    vsim = np.zeros(len(sim), dtype=np.float64)
                else:
                    # 应用层已经对 BM25 + 向量做了融合，直接使用 _score
                    sim_list = [sres.field[id].get("_score", 0.0) for id in sres.ids]
                    sim = np.array([s if s is not None else 0.0 for s in sim_list], dtype=np.float64)
                    tsim = np.zeros(len(sim), dtype=np.float64)
                    vsim = np.zeros(len(sim), dtype=np.float64)
            elif db_type in ["elasticsearch", "opensearch"]:
                # ElasticSearch doesn't normalize each way score before fusion.
                logging.info("进入 ES rerank 分支")
                sim, tsim, vsim = self.rerank(
                    sres, question, 1 - vector_similarity_weight, vector_similarity_weight,
                    rank_feature=rank_feature)
                logging.info(f"rerank 返回: sim={sim[:3] if len(sim) > 0 else []}, tsim={tsim[:3] if len(tsim) > 0 else []}, vsim={vsim[:3] if len(vsim) > 0 else []}")
            else:
                # 其它已归一化的引擎
                sim_list = [sres.field[id].get("_score", 0.0) for id in sres.ids]
                sim = np.array([s if s is not None else 0.0 for s in sim_list], dtype=np.float64)
                tsim = sim.copy()
                vsim = sim.copy()

            if db_type == "milvus" and rank_feature:
                rank_scores = self._rank_feature_scores(rank_feature, sres)
                sim = sim + rank_scores

        # Already paginated in search function
        sim_np = np.array(sim, dtype=np.float64)
        if sim_np.size == 0:
            ranks["doc_aggs"] = []
            return ranks

        sorted_idx = np.argsort(sim_np * -1)

        # When vector_similarity_weight is 0, similarity_threshold is not meaningful for term-only scores.
        post_threshold = 0.0 if vector_similarity_weight <= 0 else similarity_threshold
        valid_idx = [int(i) for i in sorted_idx if sim_np[i] >= post_threshold]
        filtered_count = len(valid_idx)
        ranks["total"] = int(filtered_count)

        if filtered_count == 0:
            ranks["doc_aggs"] = []
            return ranks

        max_pages = max(RERANK_LIMIT // max(page_size, 1), 1)
        page_index = (page - 1) % max_pages
        begin = page_index * page_size
        end = begin + page_size
        page_idx = valid_idx[begin:end]

        dim = len(sres.query_vector)
        if dim != 768:
            vector_column = f"q_{dim}_vec"
        else:
            vector_column = "vector"
        zero_vector = [0.0] * dim

        for i in page_idx:
            id = sres.ids[i]
            chunk = sres.field[id]
            text = chunk["content_with_weight"]
            dnm = chunk.get("docnm_kwd", "")
            did = chunk.get("doc_id", "")

            position_int = chunk.get("position_int", [])
            d = {
                "chunk_id": id,
                "content_ltks": sres.field[id].get("content_ltks", ""),
                "content_with_weight": text,
                "text": text,
                "doc_id": did,
                "docnm_kwd": dnm,
                "kb_id": sres.field[id]["kb_id"],
                "important_kwd": list(sres.field[id].get("important_kwd", [])), # todo 临时用list解决important_kwd非标准python类型问题
                "img_id": sres.field[id].get("img_id", ""),
                "similarity": float(sim_np[i]),
                "vector_similarity": float(vsim[i]),
                "term_similarity": float(tsim[i]),
                "vector": chunk.get(vector_column, zero_vector),
                "positions": position_int,
                "doc_type_kwd": chunk.get("doc_type_kwd", ""),
                "mom_id": chunk.get("mom_id", ""),
            }
            if highlight and sres.highlight:
                if id in sres.highlight:
                    d["highlight"] = remove_redundant_spaces(sres.highlight[id])
                else:
                    d["highlight"] = d["text"]
            ranks["chunks"].append(d)

        if aggs:
            for i in valid_idx:
                id = sres.ids[i]
                chunk = sres.field[id]
                dnm = chunk.get("docnm_kwd", "")
                did = chunk.get("doc_id", "")
                if dnm not in ranks["doc_aggs"]:
                    ranks["doc_aggs"][dnm] = {"doc_id": did, "count": 0}
                ranks["doc_aggs"][dnm]["count"] += 1

            ranks["doc_aggs"] = [
                {
                    "doc_name": k,
                    "doc_id": v["doc_id"],
                    "count": v["count"],
                }
                for k, v in sorted(
                    ranks["doc_aggs"].items(),
                    key=lambda x: x[1]["count"] * -1,
                )
            ]
        else:
            ranks["doc_aggs"] = []

        return ranks

    def sql_retrieval(self, sql, fetch_size=128, format="json"):

        # from api.settings import chat_logger
        sql = re.sub(r"[ `]+", " ", sql)
        sql = sql.replace("%", "")
        logging.info(f"Get es sql: {sql}")
        replaces = []
        for r in re.finditer(r" ([a-z_]+_l?tks)( like | ?= ?)'([^']+)'", sql):
            fld, v = r.group(1), r.group(3)
            match = " MATCH({}, '{}', 'operator=OR;minimum_should_match=30%') ".format(
                fld, rag_tokenizer.fine_grained_tokenize(rag_tokenizer.tokenize(v)))
            replaces.append(
                ("{}{}'{}'".format(
                    r.group(1),
                    r.group(2),
                    r.group(3)),
                 match))

        for p, r in replaces:
            sql = sql.replace(p, r, 1)
        logging.info(f"To es: {sql}")

        tbl = self.dataStore.sql(sql, fetch_size, format)
        # 检查 Milvus 后端返回的错误格式
        if isinstance(tbl, dict) and "error" in tbl:
            error_msg = tbl.get("error", "Unknown SQL error")
            logging.error(f"SQL failure: {sql} => {error_msg}")
            raise Exception(error_msg)
        return tbl


    def chunk_list(self, doc_id: str, tenant_id: str,
                   kb_ids: list[str], max_count=1024,
                   offset=0,
                   fields=["docnm_kwd", "content_with_weight", "img_id"],
                   sort_by_position: bool = False):
        from api.db.services.knowledgebase_service import KnowledgebaseService
        condition = {"doc_id": doc_id}

        fields_set = set(fields or [])
        if sort_by_position:
            for need in ("page_num_int", "position_int", "top_int"):
                if need not in fields_set:
                    fields_set.add(need)
        fields = list(fields_set)

        orderBy = OrderByExpr()
        if sort_by_position:
            orderBy.asc("page_num_int")
            orderBy.asc("position_int")
            orderBy.asc("top_int")

        res = []
        bs = 128

        # db = SessionLocal()
        with db_connection() as db:
            kb = KnowledgebaseService.get_by_ids(db, kb_ids)[0]
        for p in range(offset, max_count, bs):
            limit = min(bs, max_count - p)
            if limit <= 0:
                break
            milvus_res = self.dataStore.search(fields, [], condition, [], orderBy, p, limit, index_name(tenant_id, [kb.name]),
                                           kb_ids)
            dict_chunks = self.dataStore.get_fields(milvus_res, fields)
            # 直接删除系统字段
            system_fields = ['distance']
            for field in system_fields:
                dict_chunks.pop(field, None)  # 使用pop(key, None)避免KeyError
            for id, doc in dict_chunks.items():
                doc["id"] = id
            if dict_chunks:
                res.extend(dict_chunks.values())
            chunk_count = len(dict_chunks)
            if chunk_count == 0 or chunk_count < limit:
                break
        return res

    def all_tags(self, tenant_id: str, kb_ids: list[str], S=1000):
        from api.db.services.knowledgebase_service import KnowledgebaseService
        with db_connection() as db:
            kb = KnowledgebaseService.get_by_ids(db, kb_ids)[0]
        if not self.dataStore.index_exist(index_name_one(tenant_id, kb.kb_name), kb_ids[0]):
            return []
        res = self.dataStore.search([], [], {}, [], OrderByExpr(), 0, 0, index_name(tenant_id, [kb.kb_name]), kb_ids, ["tag_kwd"])
        return self.dataStore.get_aggregation(res, "tag_kwd")

    def all_tags_in_portion(self, tenant_id: str, kb_ids: list[str], S=1000):
        from api.db.services.knowledgebase_service import KnowledgebaseService
        with db_connection() as db:
            kb = KnowledgebaseService.get_by_ids(db, kb_ids)[0]
        res = self.dataStore.search([], [], {}, [], OrderByExpr(), 0, 0, index_name(tenant_id, [kb.kb_name]), kb_ids, ["tag_kwd"])
        res = self.dataStore.get_aggregation(res, "tag_kwd")
        total = np.sum([c for _, c in res])
        return {t: (c + 1) / (total + S) for t, c in res}

    def tag_content(self, tenant_id: str, kb_ids: list[str], doc, all_tags, topn_tags=3, keywords_topn=30, S=1000):
        from api.db.services.knowledgebase_service import KnowledgebaseService
        with db_connection() as db:
            kb = KnowledgebaseService.get_by_ids(db, kb_ids)[0]
        idx_nm = index_name(tenant_id, [kb.kb_name])
        match_txt = self.qryr.paragraph(doc["title_tks"] + " " + doc["content_ltks"], doc.get("important_kwd", []), keywords_topn)
        res = self.dataStore.search([], [], {}, [match_txt], OrderByExpr(), 0, 0, idx_nm, kb_ids, ["tag_kwd"])
        aggs = self.dataStore.get_aggregation(res, "tag_kwd")
        if not aggs:
            return False
        cnt = np.sum([c for _, c in aggs])
        tag_fea = sorted([(a, round(0.1 * (c + 1) / (cnt + S) / max(1e-6, all_tags.get(a, 0.0001)))) for a, c in aggs],
                         key=lambda x: x[1] * -1)[:topn_tags]
        doc[TAG_FLD] = {a.replace(".", "_"): c for a, c in tag_fea if c > 0}
        return True

    def tag_query(self, question: str, tenant_ids: str | list[str], kb_ids: list[str], all_tags, topn_tags=3, S=1000):
        from api.db.services.knowledgebase_service import KnowledgebaseService
        with db_connection() as db:
            kb = KnowledgebaseService.get_by_ids(db, kb_ids)[0]
        if isinstance(tenant_ids, str):
            idx_nms = index_name(tenant_ids, [kb.kb_name])
        else:
            idx_nms = [index_name(tid, [kb.kb_name]) for tid in tenant_ids]
        match_txt, _ = self.qryr.question(question, min_match=0.0)
        res = self.dataStore.search([], [], {}, [match_txt], OrderByExpr(), 0, 0, idx_nms, kb_ids, ["tag_kwd"])
        aggs = self.dataStore.get_aggregation(res, "tag_kwd")
        if not aggs:
            return {}
        cnt = np.sum([c for _, c in aggs])
        tag_fea = sorted([(a, round(0.1 * (c + 1) / (cnt + S) / max(1e-6, all_tags.get(a, 0.0001)))) for a, c in aggs],
                         key=lambda x: x[1] * -1)[:topn_tags]
        return {a.replace(".", "_"): max(1, c) for a, c in tag_fea}

    async def retrieval_by_toc(self, query: str, chunks: list[dict], tenant_ids: list[str], kb_names: list[str], chat_mdl, topn: int = 6):
        """
        基于 TOC (Table of Contents) 的检索增强方法
        
        Args:
            query: 查询文本
            chunks: 初始检索到的 chunk 列表
            tenant_ids: 租户 ID 列表（支持跨租户共享）
            kb_names: 知识库名称列表（与 tenant_ids 对应）
            chat_mdl: LLM 模型用于 TOC 相关性评分
            topn: 返回的 top N 结果
            
        Returns:
            重新排序和补充后的 chunks 列表
        """
        from core.prompts.generator import relevant_chunks_with_toc

        if not chunks:
            return []

        # 从 chunks 中提取实际的 kb_id，查询对应的知识库名称
        from api.db.services.knowledgebase_service import KnowledgebaseService
        kb_id_set = set(ck.get("kb_id") for ck in chunks if ck.get("kb_id"))
        with db_connection() as db:
            kbs = KnowledgebaseService.get_by_ids(db, list(kb_id_set))

        # 构建 tenant_id -> kb_names 的映射
        tenant_kb_dict = {}
        for kb in kbs:
            tid = kb.tenant_id
            if tid not in tenant_kb_dict:
                tenant_kb_dict[tid] = []
            tenant_kb_dict[tid].append(kb.name)

        # 为每个 tenant_id 生成对应的索引名称
        idx_nms = []
        for tid in tenant_ids:
            if tid in tenant_kb_dict:
                idx_nms.extend(index_name(tid, tenant_kb_dict[tid]))
        
        ranks, doc_id2kb_id = {}, {}
        for ck in chunks:
            if ck["doc_id"] not in ranks:
                ranks[ck["doc_id"]] = 0
            ranks[ck["doc_id"]] += ck["similarity"]
            doc_id2kb_id[ck["doc_id"]] = ck["kb_id"]
        doc_id = sorted(ranks.items(), key=lambda x: x[1] * -1.)[0][0]
        kb_ids = [doc_id2kb_id[doc_id]]
        es_res = self.dataStore.search(["content_with_weight"], [], {"doc_id": doc_id, "toc_kwd": "toc"}, [],
                                       OrderByExpr(), 0, 128, idx_nms,
                                       kb_ids)
        toc = []
        dict_chunks = self.dataStore.get_fields(es_res, ["content_with_weight"])
        dict_chunks.pop("distance", None)
        for _, doc in dict_chunks.items():
            try:
                toc.extend(json.loads(doc["content_with_weight"]))
            except Exception as e:
                logging.exception(e)
        if not toc:
            return chunks

        ids = await relevant_chunks_with_toc(query, toc, chat_mdl, topn * 2)
        if not ids:
            return chunks

        vector_size = 1024
        id2idx = {ck["chunk_id"]: i for i, ck in enumerate(chunks)}
        for cid, sim in ids:
            if cid in id2idx:
                chunks[id2idx[cid]]["similarity"] += sim
                continue
            chunk = self.dataStore.get(cid, idx_nms[0], kb_ids)
            if not chunk:
                continue
            d = {
                "chunk_id": cid,
                "content_ltks": chunk["content_ltks"],
                "content_with_weight": chunk["content_with_weight"],
                "doc_id": doc_id,
                "docnm_kwd": chunk.get("docnm_kwd", ""),
                "kb_id": chunk["kb_id"],
                "important_kwd": chunk.get("important_kwd", []),
                "image_id": chunk.get("img_id", ""),
                "similarity": sim,
                "vector_similarity": sim,
                "term_similarity": sim,
                "vector": [0.0] * vector_size,
                "positions": chunk.get("position_int", []),
                "doc_type_kwd": chunk.get("doc_type_kwd", "")
            }
            for k in chunk.keys():
                if k[-4:] == "_vec":
                    d["vector"] = chunk[k]
                    vector_size = len(chunk[k])
                    break
            chunks.append(d)

        return sorted(chunks, key=lambda x: x["similarity"] * -1)[:topn]

    def retrieval_by_children(self, chunks: list[dict], tenant_ids: list[str]):
        """
        处理父子关系的chunk，将子chunk合并到父chunk中

        Args:
            chunks: 检索到的chunk列表
            tenant_ids: 租户ID列表

        Returns:
            处理后的chunks列表
        """
        if not chunks:
            return []

        # 从chunks中提取kb_id，构建索引名称
        from api.db.services.knowledgebase_service import KnowledgebaseService
        kb_id_set = set(ck.get("kb_id") for ck in chunks if ck.get("kb_id"))
        with db_connection() as db:
            kbs = KnowledgebaseService.get_by_ids(db, list(kb_id_set))

        # 构建 tenant_id -> kb_names 的映射
        tenant_kb_dict = {}
        for kb in kbs:
            tid = kb.tenant_id
            if tid not in tenant_kb_dict:
                tenant_kb_dict[tid] = []
            tenant_kb_dict[tid].append(kb.name)

        # 为每个 tenant_id 生成对应的索引名称
        idx_nms = []
        for tid in tenant_ids:
            if tid in tenant_kb_dict:
                idx_nms.extend(index_name(tid, tenant_kb_dict[tid]))

        mom_chunks = defaultdict(list)
        i = 0
        while i < len(chunks):
            ck = chunks[i]
            mom_id = ck.get("mom_id")
            if not isinstance(mom_id, str) or not mom_id.strip():
                i += 1
                continue
            mom_chunks[ck["mom_id"]].append(chunks.pop(i))

        if not mom_chunks:
            return chunks

        if not chunks:
            chunks = []

        vector_size = 1024
        for id, cks in mom_chunks.items():
            chunk = self.dataStore.get(id, idx_nms[0], [ck["kb_id"] for ck in cks])
            d = {
                "chunk_id": id,
                "content_ltks": " ".join([ck["content_ltks"] for ck in cks]),
                "text": chunk["content_with_weight"],
                "doc_id": chunk["doc_id"],
                "docnm_kwd": chunk.get("docnm_kwd", ""),
                "kb_id": chunk["kb_id"],
                "important_kwd": [kwd for ck in cks for kwd in ck.get("important_kwd", [])],
                "image_id": chunk.get("img_id", ""),
                "similarity": np.mean([ck["similarity"] for ck in cks]),
                "vector_similarity": np.mean([ck["similarity"] for ck in cks]),
                "term_similarity": np.mean([ck["similarity"] for ck in cks]),
                "vector": [0.0] * vector_size,
                "positions": chunk.get("position_int", []),
                "doc_type_kwd": chunk.get("doc_type_kwd", "")
            }
            for k in cks[0].keys():
                if k[-4:] == "_vec":
                    d["vector"] = cks[0][k]
                    vector_size = len(cks[0][k])
                    break
            chunks.append(d)

        return sorted(chunks, key=lambda x: x["similarity"] * -1)
