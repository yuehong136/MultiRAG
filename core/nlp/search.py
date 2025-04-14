import logging
import re
from dataclasses import dataclass

import numpy as np

from api.db.db_models import db_connection, SessionLocal
from api.db.services.knowledgebase_service import KnowledgebaseService
from core.utils import rmSpace
from core.settings import TAG_FLD, PAGERANK_FLD
from core.nlp import rag_tokenizer, query, is_english
from core.utils.doc_store_conn import MatchDenseExpr

from core.utils.doc_store_conn import (
    DocStoreConnection,
    MatchExpr,
    MatchTextExpr,
    MatchDenseExpr,
    FusionExpr,
    OrderByExpr,
)

def index_name(uid, kb_names):
    return [f"multirag_{uid}_{kb_name}" for kb_name in kb_names]


def index_name_one(uid, kb_name):
    return f"multirag_{uid}_{kb_name}"


class Dealer:
    def __init__(self, dataStore: DocStoreConnection):
        self.qryr = query.MilvusQueryer(dataStore)
        self.qryr.flds = [
            "title_tks^10",
            "title_sm_tks^5",
            "important_kwd^30",
            "important_tks^20",
            "question_tks^20",
            "content_ltks^2",
            "content_sm_ltks"]
        # self.milvus_conn = milvus_conn
        self.dataStore = dataStore

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

    def get_vector(self, collection_name, txt, emb_mdl, topk=10, similarity=0.1):
        qv, _ = emb_mdl.encode_queries(txt)
        shape = np.array(qv).shape
        if len(shape) > 1:
            raise Exception(
                f"Dealer.get_vector returned array's shape {shape} doesn't match expectation(exact one dimension).")

        embedding_data = [float(v) for v in qv]
        vector_dim = len(embedding_data)

        # 修改点：先使用标准向量字段名，避免报错
        vector_column_name = "vector"

        # 再检查维度特定字段是否存在于集合中，如果存在则使用它
        try:
            schema = self.dataStore.describe_collection(collection_name)
            for field in schema['fields']:
                if field['name'] == f"q_{vector_dim}_vec":
                    vector_column_name = f"q_{vector_dim}_vec"
                    break
        except Exception as e:
            logging.warning(f"检查字段 q_{vector_dim}_vec 时出错: {str(e)}，将使用默认字段 vector")

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
            condition["auth"] = req["filter_exp"]
        return condition


    # def search(self, req, idxnms, embd_mdl=None, rank_feature: dict | None = None):
    #     qst = req.get("question", "")
    #     bqry, keywords = self.qryr.question(qst, min_match=0.3)
    #     total, ids, fields = 0, [], {}
    #     if bqry is None:
    #         raise ValueError("Failed to generate query for the given question.")
    #
    #     src = req.get("fields", ["docnm_kwd", "content_ltks", "kb_id", "img_id", "title_tks",
    #                              "doc_id", "position_int", "content_with_weight", PAGERANK_FLD, TAG_FLD])
    #                              # "doc_id", "vector", "position_int", "content_with_weight"])
    #     filter = req.get("filter_exp", "")
    #
    #     # Vector search parameters
    #     if req.get("vector"):
    #         assert embd_mdl, "No embedding model selected"
    #
    #         for idxnm in idxnms:
    #             logging.info(f"正在搜索的集合: {idxnm}")
    #             vector_search_params = self.get_vector(idxnm, qst, embd_mdl, req.get("topk", 1024), req.get("similarity", 0.1))
    #             query_vector = vector_search_params.embedding_data
    #             vector_column_name = vector_search_params.vector_column_name
    #             src.append(vector_column_name)
    #             # todo 后续考虑不同维度字段检索情况，目前统一叫vector，eg.用户512维的输入无法比对718存储的vector，动态名字就可以了
    #             try:
    #                 # 在Milvus中执行搜索
    #                 # 参数:
    #                 # collection_name: 指定要搜索的集合名称
    #                 # data: 查询向量
    #                 # anns_field: 指定用于向量搜索的字段
    #                 # limit: 要返回的结果数量，默认为10，如果未指定的话
    #                 # search_params: 搜索参数，包括度量类型和nprobe值
    #                 # output_fields: 指定要在搜索结果中返回的字段
    #                 search_results = self.dataStore.search_by_milvus(
    #                     collection_name=idxnm,
    #                     data=[query_vector],
    #                     anns_field=vector_search_params.vector_column_name,
    #                     limit=req.get("size", 1024),
    #                     offset=(req.get("page", 1) - 1) * req.get("size", 10),
    #                     search_params={"metric_type": "COSINE", "params": {"nprobe": 10}},
    #                     output_fields=src,
    #                     filter=filter
    #                 )
    #
    #                 #logging.info(f"Search results for {idxnm}: {search_results}")
    #                 logging.info(f"Search results for {idxnm} ~ 详细查询数据请解开 core/nlp/search 下的注释")
    #
    #                 # Process search results
    #                 if search_results:
    #                     total += len(search_results[0])
    #                     for hit in search_results[0]:
    #                         # 根据Milvus版本选择pk或id，2.5.2及以下版本hit中是id
    #                         hit_id = str(hit.get('pk', hit.get('id')))
    #                         ids.append(hit_id)
    #
    #                         hit_fields = {}
    #                         for field in src:
    #                             hit_fields[field] = hit['entity'].get(field, "")  # 提取每个字段的数据
    #
    #                         # 存储到fields字典中
    #                         fields[hit_id] = hit_fields
    #             except Exception as e:
    #                 logging.error(f"Error searching in collection {idxnm}: {str(e)}")
    #
    #     # 如果没有向量搜索条件，则执行基于doc_id的简单查询
    #     else:
    #         doc_ids = req.get("doc_ids")
    #
    #         if not doc_ids:
    #             raise ValueError("doc_ids is required for non-vector search.")
    #
    #         fields_to_return = req.get("fields", ["pk", "content_with_weight", "doc_id", "docnm_kwd", "img_id", "position_int", "auth"])
    #
    #         for doc_id in doc_ids:
    #             logging.info(f"正在从集合 {idxnms} 获取文档 ID 为 {doc_id} 的数据")
    #             try:
    #                 # 使用 Milvus query 方法进行简单查询
    #                 search_results = self.dataStore.query(
    #                     collection_name=idxnms,
    #                     # filter=f"doc_id == {doc_id}",
    #                     filter=f"doc_id == '{{doc_id}}'".format(doc_id=doc_id),
    #                     output_fields=fields_to_return,
    #                     limit=req.get("size", 1024),
    #                     offset=(req.get("page", 1) - 1) * req.get("size", 10),
    #                 )
    #                 if search_results:
    #                     total += len(search_results)
    #                     for hit in search_results:
    #                         hit_id = str(hit["pk"])
    #                         ids.append(hit_id)
    #                         hit_fields = {field: hit.get(field, "") for field in fields_to_return}
    #                         fields[hit_id] = hit_fields
    #                 logging.info(f"Query results for {idxnms}->{doc_id}: {search_results}")
    #             except Exception as e:
    #                 logging.error(f"Error querying in collection {idxnms}->{doc_id}: {str(e)}")
    #
    #     kwds = set([])
    #     for k in keywords:
    #         kwds.add(k)
    #         for kk in rag_tokenizer.fine_grained_tokenize(k).split():
    #             if len(kk) < 2:
    #                 continue
    #             if kk in kwds:
    #                 continue
    #             kwds.add(kk)
    #
    #     aggs = self.getAggregation(search_results, "docnm_kwd")
    #     if req.get("vector"):
    #         return self.SearchResult(
    #             total=total,
    #             ids=ids,
    #             query_vector=query_vector,
    #             aggregation=aggs,
    #             highlight=self.getHighlight(search_results, keywords, "content_with_weight"),
    #             field=fields,
    #             keywords=list(kwds)
    #         )
    #
    #     else:
    #         return self.QueryResult(
    #             total=total,
    #             ids=ids,
    #             aggregation=aggs,
    #             field=fields,
    #             keywords=list(kwds)
    #         )
    def search(self, req, idx_names: str | list[str], kb_ids: list[str], emb_mdl=None, highlight=False,
               rank_feature: dict | None = None):
        """
        Search method aligning with ES-based implementation but using Milvus as backend

        Args:
            req: Request parameters dictionary
            idx_names: Index name(s) as string or list
            kb_ids: Knowledge base IDs list
            emb_mdl: Embedding model for vector search
            highlight: Whether to highlight matching results
            rank_feature: Ranking features dictionary

        Returns:
            SearchResult: Search results object
        """
        # Get filter conditions
        filters = self.get_filters(req)

        # Create ordering expression
        orderBy = OrderByExpr()

        # Calculate pagination parameters
        pg = int(req.get("page", 1)) - 1
        topk = int(req.get("topk", 1024))
        ps = int(req.get("size", topk))
        offset, limit = pg * ps, ps

        # Determine fields to return
        src = req.get("fields",
                      ["docnm_kwd", "content_ltks", "kb_id", "img_id", "title_tks", "important_kwd", "position_int",
                       "doc_id", "page_num_int", "top_int", "create_timestamp_flt", "knowledge_graph_kwd",
                       "question_kwd", "question_tks", "available_int", "content_with_weight", PAGERANK_FLD, TAG_FLD])

        kwds = set([])

        # Get query string
        qst = req.get("question", "")
        q_vec = []

        # If no query string, return sorted results
        if not qst:
            if req.get("sort"):
                orderBy.asc("page_num_int")
                orderBy.asc("top_int")
                orderBy.desc("create_timestamp_flt")
            res = self.dataStore.search(src, [], filters, [], orderBy, offset, limit, idx_names, kb_ids)
            total = self.dataStore.getTotal(res)
            logging.debug(f"Dealer.search TOTAL: {total}")
        else:
            # If query string exists, use highlight fields if needed
            highlightFields = ["content_ltks", "title_tks"] if highlight else []

            # Generate text matching expression
            matchText, keywords = self.qryr.question(qst, min_match=0.3)

            # If no embedding model, use only text matching
            if emb_mdl is None:
                matchExprs = [matchText]
                res = self.dataStore.search(src, highlightFields, filters, matchExprs, orderBy, offset, limit,
                                            idx_names, kb_ids, rank_feature=rank_feature)
                total = self.dataStore.getTotal(res)
                logging.debug(f"Dealer.search TOTAL: {total}")
                # doc_ids = req.get("doc_ids")
                #
                # if not doc_ids:
                #     raise ValueError("doc_ids is required for non-vector search.")
                # total, ids, fields = 0, [], {}
                # for doc_id in doc_ids:
                #     fields_to_return = req.get("fields", ["pk", "content_with_weight", "doc_id", "docnm_kwd", "img_id", "position_int", "auth"])
                #     res = self.dataStore.query(
                #         collection_name=idx_names,
                #         filter=f"doc_id == '{{doc_id}}'".format(doc_id=doc_id),
                #         output_fields=fields_to_return,
                #         limit=req.get("size", 1024),
                #         offset=(req.get("page", 1) - 1) * req.get("size", 10),
                #     )
                #     if res:
                #         total += len(res)
                #         for hit in res:
                #             hit_id = str(hit["pk"])
                #             ids.append(hit_id)
                #             hit_fields = {field: hit.get(field, "") for field in fields_to_return}
                #             fields[hit_id] = hit_fields
                # logging.debug(f"Dealer.search TOTAL: {total}")
            else:
                # If embedding model exists, use fusion search (text + vector)
                for idxnm in idx_names:
                    logging.info(f"正在搜索的集合: {idxnm}")
                    matchDense = self.get_vector(idxnm, qst, emb_mdl, topk, req.get("similarity", 0.1))
                    q_vec = matchDense.embedding_data
                    src.append(f"q_{len(q_vec)}_vec")

                    fusionExpr = FusionExpr("weighted_sum", topk, {"weights": "0.05, 0.95"})
                    matchExprs = [matchText, matchDense, fusionExpr]

                    res, total = self.dataStore.search(src, highlightFields, filters, matchExprs, orderBy, offset, limit,
                                                idx_names, kb_ids, rank_feature=rank_feature)
                    # total = self.dataStore.getTotal(res)
                    logging.debug(f"Dealer.search TOTAL: {total}")

                    # If no results, try with lower match threshold
                    if total == 0:
                        matchText, _ = self.qryr.question(qst, min_match=0.1)
                        filters.pop("doc_ids", None)
                        matchDense.extra_options["similarity"] = 0.17
                        res = self.dataStore.search(src, highlightFields, filters, [matchText, matchDense, fusionExpr],
                                                    orderBy, offset, limit, idx_names, kb_ids, rank_feature=rank_feature)
                        total = self.dataStore.getTotal(res)
                        logging.debug(f"Dealer.search 2 TOTAL: {total}")

            # Process keywords
            for k in keywords:
                kwds.add(k)
                for kk in rag_tokenizer.fine_grained_tokenize(k).split():
                    if len(kk) < 2:
                        continue
                    if kk in kwds:
                        continue
                    kwds.add(kk)

        # Get results
        logging.debug(f"TOTAL: {total}")
        ids = self.dataStore.getChunkIds(res)
        keywords = list(kwds)
        highlight_results = self.dataStore.getHighlight(res, keywords, "content_with_weight")
        aggs = self.dataStore.getAggregation(res, "docnm_kwd")

        # Return search result object
        return self.SearchResult(
            total=total,
            ids=ids,
            query_vector=q_vec,
            aggregation=aggs,
            highlight=highlight_results,
            field=self.dataStore.getFields(res, src),
            keywords=keywords
        )

    # def count(self, req, idxnms, embd_mdl=None):
    #     qst = req.get("question", "")
    #     bqry, keywords = self.qryr.question(qst, min_match=0.3)
    #     total, ids, fields = 0, [], {}
    #     if bqry is None:
    #         raise ValueError("Failed to generate query for the given question.")
    #
    #     src = req.get("fields", ["docnm_kwd", "content_ltks", "kb_id", "img_id", "title_tks",
    #                              "doc_id", "position_int", "content_with_weight"])
    #                              # "doc_id", "vector", "position_int", "content_with_weight"])
    #     filter = req.get("filter_exp", "")
    #
    #     # # 检查是否需要添加维度特定向量字段
    #     # vector_dim = None
    #     # if req.get("vector") and embd_mdl:
    #     #     # 获取向量维度
    #     #     sample_vec, _ = embd_mdl.encode_queries("测试")
    #     #     if len(sample_vec) > 0:
    #     #         vector_dim = len(sample_vec)
    #     #         dim_field = f"q_{vector_dim}_vec"
    #     #         if dim_field not in src:
    #     #             src.append(dim_field)
    #     #             logging.info(f"添加维度特定向量字段 {dim_field} 到查询字段")
    #
    #     # Vector search parameters
    #     if req.get("vector"):
    #         assert embd_mdl, "No embedding model selected"
    #
    #         for idxnm in idxnms:
    #             logging.info(f"正在搜索的集合: {idxnm}")
    #             vector_search_params = self.get_vector(idxnm, qst, embd_mdl, req.get("topk", 1024), req.get("similarity", 0.1))
    #             query_vector = vector_search_params.embedding_data
    #             vector_column_name = vector_search_params.vector_column_name
    #             src.append(vector_column_name)
    #             # todo 后续考虑不同维度字段检索情况，目前统一叫vector，eg.用户512维的输入无法比对718存储的vector，动态名字就可以了
    #             try:
    #                 # 在Milvus中执行搜索
    #                 # 参数:
    #                 # collection_name: 指定要搜索的集合名称
    #                 # data: 查询向量
    #                 # anns_field: 指定用于向量搜索的字段
    #                 # limit: 要返回的结果数量，默认为10，如果未指定的话
    #                 # search_params: 搜索参数，包括度量类型和nprobe值
    #                 # output_fields: 指定要在搜索结果中返回的字段
    #                 search_results = self.dataStore.search_by_milvus(
    #                     collection_name=idxnm,
    #                     data=[query_vector],
    #                     anns_field=vector_search_params.vector_column_name,
    #                     search_params={"metric_type": "COSINE", "params": {"nprobe": 10}},
    #                     output_fields=src,
    #                     filter=filter
    #                 )
    #
    #                 #logging.info(f"Search results for {idxnm}: {search_results}")
    #                 logging.info(f"Search results for {idxnm} ~ 详细查询数据请解开 core/nlp/search 下的注释")
    #
    #                 # Process search results
    #                 if search_results:
    #                     total += len(search_results[0])
    #                     for hit in search_results[0]:
    #                         # 根据Milvus版本选择pk或id，2.5.2及以下版本hit中是id
    #                         hit_id = str(hit.get('pk', hit.get('id')))
    #                         ids.append(hit_id)
    #
    #                         hit_fields = {}
    #                         for field in src:
    #                             hit_fields[field] = hit['entity'].get(field, "")  # 提取每个字段的数据
    #
    #                         # 存储到fields字典中
    #                         fields[hit_id] = hit_fields
    #             except Exception as e:
    #                 logging.error(f"Error searching in collection {idxnm}: {str(e)}")
    #
    #     # 如果没有向量搜索条件，则执行基于doc_id的简单查询
    #     else:
    #         doc_ids = req.get("doc_ids")
    #
    #         if not doc_ids:
    #             raise ValueError("doc_ids is required for non-vector search.")
    #
    #         fields_to_return = req.get("fields", ["pk", "content_with_weight", "doc_id", "docnm_kwd", "img_id", "position_int", "auth"])
    #
    #         for doc_id in doc_ids:
    #             logging.info(f"正在从集合 {idxnms} 获取文档 ID 为 {doc_id} 的数据")
    #             try:
    #                 # 使用 Milvus query 方法进行简单查询
    #                 search_results = self.dataStore.query(
    #                     collection_name=idxnms,
    #                     # filter=f"doc_id == {doc_id}",
    #                     filter=f"doc_id == '{{doc_id}}'".format(doc_id=doc_id),
    #                     output_fields=fields_to_return,
    #                 )
    #                 if search_results:
    #                     total += len(search_results)
    #                     for hit in search_results:
    #                         hit_id = str(hit["pk"])
    #                         ids.append(hit_id)
    #                         hit_fields = {field: hit.get(field, "") for field in fields_to_return}
    #                         fields[hit_id] = hit_fields
    #                 logging.info(f"Query results for {idxnms}->{doc_id}: {search_results}")
    #             except Exception as e:
    #                 logging.error(f"Error querying in collection {idxnms}->{doc_id}: {str(e)}")
    #
    #     kwds = set([])
    #     for k in keywords:
    #         kwds.add(k)
    #         for kk in rag_tokenizer.fine_grained_tokenize(k).split():
    #             if len(kk) < 2:
    #                 continue
    #             if kk in kwds:
    #                 continue
    #             kwds.add(kk)
    #
    #     aggs = self.getAggregation(search_results, "docnm_kwd")
    #     if req.get("vector"):
    #         return self.SearchResult(
    #             total=total,
    #             ids=ids,
    #             query_vector=query_vector,
    #             aggregation=aggs,
    #             highlight=self.getHighlight(search_results, keywords, "content_with_weight"),
    #             field=fields,
    #             keywords=list(kwds)
    #         )
    #
    #     else:
    #         return self.QueryResult(
    #             total=total,
    #             ids=ids,
    #             aggregation=aggs,
    #             field=fields,
    #             keywords=list(kwds)
    #         )

    def getAggregation(self, res, g):
        if not "aggregations" in res or "aggs_" + g not in res["aggregations"]:
            return
        bkts = res["aggregations"]["aggs_" + g]["buckets"]
        return [(b["key"], b["doc_count"]) for b in bkts]

    def getHighlight(self, res, keywords, fieldnm):
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
                if not re.search(r"<em>[^<>]+</em>", t, flags=re.IGNORECASE | re.MULTILINE): continue
                txts.append(t)

            # 拼接并返回最终结果
            ans[entity.get("doc_id", "")] = "...".join(txts) if txts else txt

        return ans

    @staticmethod
    def trans2floats(txt):
        return [float(t) for t in txt.split("\t")]

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
                    pieces_.extend(
                        re.split(
                            r"([^\|][；。？!！\n]|[a-z][.?;!][ \n])",
                            pieces[i]))
                    i += 1
            pieces = pieces_
        else:
            pieces = re.split(r"([^\|][；。？!！\n]|[a-z][.?;!][ \n])", answer)
        for i in range(1, len(pieces)):
            if re.match(r"([^\|][；。？!！\n]|[a-z][.?;!][ \n])", pieces[i]):
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
                chunk_v[i] = [0.0]*len(ans_v[0])
                logging.warning("The dimension of query and chunk do not match: {} vs. {}".format(len(ans_v[0]), len(chunk_v[i])))

        assert len(ans_v[0]) == len(chunk_v[0]), "The dimension of query and chunk do not match: {} vs. {}".format(
            len(ans_v[0]), len(chunk_v[0]))

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
                res += f" ##{c}$$"
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

        q_denor = np.sqrt(np.sum([s*s for t,s in query_rfea.items() if t != PAGERANK_FLD]))
        for i in search_res.ids:
            nor, denor = 0, 0
            for t, sc in eval(search_res.field[i].get(TAG_FLD, "{}")).items():
                if t in query_rfea:
                    nor += query_rfea[t] * sc
                denor += sc * sc
            if denor == 0:
                rank_fea.append(0)
            else:
                rank_fea.append(nor/np.sqrt(denor)/q_denor)
        return np.array(rank_fea)*10. + pageranks

    # def rerank(self, sres, query, tkweight=0.3,
    #            vtweight=0.7, cfield="content_ltks"):
    #     _, keywords = self.qryr.question(query)
    #     ins_embd = [sres.query_vector for i in sres.ids]
    #     if not ins_embd:
    #         return [], [], []
    #
    #     ins_tw = []
    #     # for i in sres.ids:
    #     #     tks = sres.field[i].split()
    #     #     ins_tw.append(tks)
    #     for i in sres.ids:
    #         content_ltks = sres.field[i][cfield].split()
    #         title_tks = [t for t in sres.field[i].get("title_tks", "").split() if t]
    #         question_tks = [t for t in sres.field[i].get("question_tks", "").split() if t]
    #         important_kwd = sres.field[i].get("important_kwd", [])
    #         tks = content_ltks + title_tks * 2 + important_kwd * 5 + question_tks * 6
    #         ins_tw.append(tks)
    #
    #     sim, tksim, vtsim = self.qryr.hybrid_similarity(sres.query_vector,
    #                                                     ins_embd,
    #                                                     keywords,
    #                                                     ins_tw, tkweight, vtweight)
    #     return sim, tksim, vtsim
    def rerank(self, sres, query, tkweight=0.3, vtweight=0.7, cfield="content_ltks",
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
        vector_dim = len(sres.query_vector)
        if vector_dim != 768:
            dim_field = f"q_{vector_dim}_vec"
        else:
            dim_field = "vector"
        ins_embd = []
        for i in sres.ids:
            # 优先使用维度特定字段，如果没有则使用标准vector字段
            if dim_field in sres.field[i] and sres.field[i][dim_field]:
                vector = sres.field[i][dim_field]
            else:
                vector = sres.field[i].get("vector", [0.0] * vector_dim)

            # 确保向量是列表格式
            if isinstance(vector, str):
                # 如果是字符串格式，尝试解析
                try:
                    vector = [float(v) for v in vector.split()]
                except:
                    vector = [0.0] * vector_dim

            ins_embd.append(vector)

        # 处理文本相似度比较所需的token列表
        ins_tw = []
        for i in sres.ids:
            content_ltks = sres.field[i].get(cfield, "").split()

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
            tks = content_ltks + title_tks + important_kwd
            ins_tw.append(tks)

        tksim = self.qryr.token_similarity(keywords, ins_tw)
        vtsim, _ = rerank_mdl.similarity(query, [rmSpace(" ".join(tks)) for tks in ins_tw])
        ## For rank feature(tag_fea) scores.
        rank_fea = self._rank_feature_scores(rank_feature, sres)

        return tkweight * (np.array(tksim) + rank_fea) + vtweight * vtsim, tksim, vtsim

    def hybrid_similarity(self, ans_embd, ins_embd, ans, inst):
        return self.qryr.hybrid_similarity(ans_embd,
                                           ins_embd,
                                           rag_tokenizer.tokenize(ans).split(),
                                           rag_tokenizer.tokenize(inst).split())

    def retrieval(self, question, filter_exp, embd_mdl, tenant_id, kb_names, page, page_size, similarity_threshold=0.2,
                  vector_similarity_weight=0.3, top=1024, doc_ids=None, aggs=True, rerank_mdl=None, highlight=False,
                  rank_feature=None):
        if rank_feature is None:
            rank_feature = {PAGERANK_FLD: 10}
        ranks = {"total": 0, "chunks": [], "doc_aggs": {}}
        if not question:
            return ranks
        RERANK_PAGE_LIMIT = 3
        req = {"kb_names": kb_names, "doc_ids": doc_ids, "size": max(page_size * RERANK_PAGE_LIMIT, 128),
               "question": question, "vector": True, "topk": top,
               "similarity": similarity_threshold,
               "available_int": 1, "filter_exp": filter_exp}
        idxnms = index_name(tenant_id, kb_names)
        if page > RERANK_PAGE_LIMIT:
            req["page"] = page
            req["size"] = page_size
        sres = self.search(req, idxnms, kb_names, embd_mdl, rank_feature=rank_feature)
        ranks["total"] = sres.total

        if not sres.ids:
            return ranks

        if page <= RERANK_PAGE_LIMIT:
            if rerank_mdl and sres.total > 0:
                sim, tsim, vsim = self.rerank_by_model(rerank_mdl,
                                                       sres, question, 1 - vector_similarity_weight,
                                                       vector_similarity_weight,
                                                       rank_feature=rank_feature)
            else:
                sim, tsim, vsim = self.rerank(
                    sres, question, 1 - vector_similarity_weight, vector_similarity_weight,
                    rank_feature=rank_feature)
            idx = np.argsort(sim * -1)[(page - 1) * page_size:page * page_size]
        else:
            sim = tsim = vsim = [1] * len(sres.ids)
            idx = list(range(len(sres.ids)))

        # def floor_sim(score):
        #     return (int(score * 100.) % 100) / 100.

        dim = len(sres.query_vector)
        if dim != 768:
            vector_column = f"q_{dim}_vec"
        else:
            vector_column = "vector"
        zero_vector = [0.0] * dim
        for i in idx:
            # if floor_sim(sim[i]) < similarity_threshold:
            if sim[i] < similarity_threshold:
                break
            # ranks["total"] += 1
            # start_idx -= 1
            # if start_idx >= 0:
            #     continue
            if len(ranks["chunks"]) >= page_size:
                if aggs:
                    continue
                break
            # id = sres.ids[i]
            # text = sres.field[id]["content_with_weight"]
            # dnm = sres.field[id].get("docnm_kwd", "")
            # did = sres.field[id]["doc_id"]

            id = sres.ids[i]
            chunk = sres.field[id]
            text = chunk["content_with_weight"]
            dnm = chunk["docnm_kwd"]
            did = chunk["doc_id"]
            # position_int = chunk.get("position_int", [])
            d = {
                "chunk_id": id,
                "content_ltks": sres.field[id].get("content_ltks", ""),
                "text": text,
                "doc_id": sres.field[id]["doc_id"],
                "docnm_kwd": dnm,
                "kb_id": sres.field[id]["kb_id"],
                "important_kwd": list(sres.field[id].get("important_kwd", [])), # todo 临时用list解决important_kwd非标准python类型问题
                "img_id": sres.field[id].get("img_id", ""),
                "similarity": sim[i],
                "vector_similarity": vsim[i],
                "term_similarity": tsim[i],
                # "vector": self.trans2floats("\t".join(map(str, sres.query_vector))),
                "vector": chunk.get(vector_column, zero_vector),
                "positions": sres.field[id].get("position_int", [])
            }
            if highlight and sres.highlight:
                if id in sres.highlight:
                    d["highlight"] = rmSpace(sres.highlight[id])
                else:
                    d["highlight"] = d["text"]
            if len(d["positions"]) % 5 == 0:
                poss = []
                for i in range(0, len(d["positions"]), 5):
                    poss.append([float(d["positions"][i]), float(d["positions"][i + 1]), float(d["positions"][i + 2]),
                                 float(d["positions"][i + 3]), float(d["positions"][i + 4])])
                d["positions"] = poss
            ranks["chunks"].append(d)
            if dnm not in ranks["doc_aggs"]:
                ranks["doc_aggs"][dnm] = {"doc_id": did, "count": 0}
            ranks["doc_aggs"][dnm]["count"] += 1
        ranks["doc_aggs"] = [{"doc_name": k,
                              "doc_id": v["doc_id"],
                              "count": v["count"]} for k,
                             v in sorted(ranks["doc_aggs"].items(),
                                         key=lambda x: x[1]["count"] * -1)]
        ranks["chunks"] = ranks["chunks"][:page_size]

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

        try:
            tbl = self.dataStore.sql(sql, fetch_size, format)
            return tbl
        except Exception as e:
            logging.error(f"SQL failure: {sql} =>" + str(e))
            return {"error": str(e)}


    def chunk_list(self, doc_id: str, tenant_id: str,
                   kb_ids: list[str], max_count=1024,
                   offset=0,
                   fields=["docnm_kwd", "content_with_weight", "img_id"]):
        condition = {"doc_id": doc_id}
        res = []
        bs = 128

        # db = SessionLocal()
        with db_connection() as db:
            kb = KnowledgebaseService.get_by_ids(db, kb_ids)[0]
        for p in range(offset, max_count, bs):
            milvus_res = self.dataStore.search(fields, [], condition, [], OrderByExpr(), p, bs, index_name(tenant_id, [kb.kb_name]),
                                           kb_ids)
            dict_chunks = self.dataStore.getFields(milvus_res, fields)
            for id, doc in dict_chunks.items():
                doc["id"] = id
            if dict_chunks:
                res.extend(dict_chunks.values())
            if len(dict_chunks.values()) < bs:
                break
        return res

    def all_tags(self, tenant_id: str, kb_ids: list[str], S=1000):
        with db_connection() as db:
            kb = KnowledgebaseService.get_by_ids(db, kb_ids)[0]
        res = self.dataStore.search([], [], {}, [], OrderByExpr(), 0, 0, index_name(tenant_id, [kb.kb_name]), kb_ids, ["tag_kwd"])
        return self.dataStore.getAggregation(res, "tag_kwd")

    def all_tags_in_portion(self, tenant_id: str, kb_ids: list[str], S=1000):
        with db_connection() as db:
            kb = KnowledgebaseService.get_by_ids(db, kb_ids)[0]
        res = self.dataStore.search([], [], {}, [], OrderByExpr(), 0, 0, index_name(tenant_id, [kb.kb_name]), kb_ids, ["tag_kwd"])
        res = self.dataStore.getAggregation(res, "tag_kwd")
        total = np.sum([c for _, c in res])
        return {t: (c + 1) / (total + S) for t, c in res}

    def tag_content(self, tenant_id: str, kb_ids: list[str], doc, all_tags, topn_tags=3, keywords_topn=30, S=1000):
        with db_connection() as db:
            kb = KnowledgebaseService.get_by_ids(db, kb_ids)[0]
        idx_nm = index_name(tenant_id, [kb.kb_name])
        match_txt = self.qryr.paragraph(doc["title_tks"] + " " + doc["content_ltks"], doc.get("important_kwd", []), keywords_topn)
        res = self.dataStore.search([], [], {}, [match_txt], OrderByExpr(), 0, 0, idx_nm, kb_ids, ["tag_kwd"])
        aggs = self.dataStore.getAggregation(res, "tag_kwd")
        if not aggs:
            return False
        cnt = np.sum([c for _, c in aggs])
        tag_fea = sorted([(a, round(0.1*(c + 1) / (cnt + S) / (all_tags.get(a, 0.0001)))) for a, c in aggs],
                         key=lambda x: x[1] * -1)[:topn_tags]
        doc[TAG_FLD] = {a: c for a, c in tag_fea if c > 0}
        return True

    def tag_query(self, question: str, tenant_ids: str | list[str], kb_ids: list[str], all_tags, topn_tags=3, S=1000):
        with db_connection() as db:
            kb = KnowledgebaseService.get_by_ids(db, kb_ids)[0]
        if isinstance(tenant_ids, str):
            idx_nms = index_name(tenant_ids, [kb.kb_name])
        else:
            idx_nms = [index_name(tid, [kb.kb_name]) for tid in tenant_ids]
        match_txt, _ = self.qryr.question(question, min_match=0.0)
        res = self.dataStore.search([], [], {}, [match_txt], OrderByExpr(), 0, 0, idx_nms, kb_ids, ["tag_kwd"])
        aggs = self.dataStore.getAggregation(res, "tag_kwd")
        if not aggs:
            return {}
        cnt = np.sum([c for _, c in aggs])
        tag_fea = sorted([(a, round(0.1*(c + 1) / (cnt + S) / (all_tags.get(a, 0.0001)))) for a, c in aggs],
                         key=lambda x: x[1] * -1)[:topn_tags]
        return {a: c for a, c in tag_fea if c > 0}

    # def chunk_list(self, doc_id: str, tenant_id: str,
    #                kb_ids: list[str], max_count=1024,
    #                offset=0,
    #                fields=["docnm_kwd", "content_with_weight", "img_id"]):
    #     """
    #     获取文档的所有块
    #
    #     Args:
    #         doc_id: 文档ID
    #         tenant_id: 租户ID
    #         kb_ids: 知识库ID列表
    #         max_count: 最大返回数量
    #         offset: 起始偏移
    #         fields: 要返回的字段列表
    #
    #     Returns:
    #         list: 文档块列表
    #     """
    #     condition = {"doc_id": doc_id}
    #     res = []
    #     bs = 128  # 批量大小
    #
    #     # 获取集合名称列表
    #     if isinstance(kb_ids, str):
    #         kb_ids = [kb_ids]
    #
    #     for kb_id in kb_ids:
    #         collection_name = index_name_one(tenant_id, kb_id)
    #
    #         try:
    #             # 检查集合是否存在
    #             if not self.dataStore.has_collection(collection_name):
    #                 logging.warning(f"集合 {collection_name} 不存在")
    #                 continue
    #
    #             # 分批获取文档块
    #             for p in range(offset, max_count, bs):
    #                 filter_expr = f"doc_id == '{doc_id}'"
    #
    #                 # 执行查询
    #                 query_results = self.dataStore.query(
    #                     collection_name=collection_name,
    #                     filter=filter_expr,
    #                     output_fields=fields,
    #                     offset=p,
    #                     limit=min(bs, max_count - p)
    #                 )
    #
    #                 if not query_results:
    #                     break
    #
    #                 # 处理结果
    #                 for chunk in query_results:
    #                     chunk_data = {}
    #                     for field in fields:
    #                         if field in chunk:
    #                             # 处理特殊字段
    #                             if field in ["important_kwd", "question_kwd", "entities_kwd"] and isinstance(
    #                                     chunk[field], str):
    #                                 chunk_data[field] = chunk[field].split("###") if chunk[field] else []
    #                             elif field == "position_int" and isinstance(chunk[field], str):
    #                                 if chunk[field]:
    #                                     arr = [int(hex_val, 16) for hex_val in chunk[field].split('_')]
    #                                     chunk_data[field] = [arr[i:i + 5] for i in range(0, len(arr), 5)]
    #                                 else:
    #                                     chunk_data[field] = []
    #                             elif field in ["page_num_int", "top_int"] and isinstance(chunk[field], str):
    #                                 if chunk[field]:
    #                                     chunk_data[field] = [int(hex_val, 16) for hex_val in chunk[field].split('_')]
    #                                 else:
    #                                     chunk_data[field] = []
    #                             else:
    #                                 chunk_data[field] = chunk[field]
    #
    #                     res.append(chunk_data)
    #
    #                 # 如果结果数量少于批量大小，说明已经没有更多结果
    #                 if len(query_results) < bs:
    #                     break
    #
    #         except Exception as e:
    #             logging.error(f"获取文档块失败: {str(e)}")
    #
    #     return res


    # def all_tags(self, tenant_id: str, kb_ids: list[str], S=1000):
    #     """
    #     获取所有标签
    #
    #     Args:
    #         tenant_id: 租户ID
    #         kb_ids: 知识库ID列表
    #         S: 平滑参数
    #
    #     Returns:
    #         list: 标签和频次列表
    #     """
    #     if isinstance(kb_ids, str):
    #         kb_ids = [kb_ids]
    #
    #     agg_results = []
    #
    #     for kb_id in kb_ids:
    #         collection_name = index_name_one(tenant_id, kb_id)
    #
    #         try:
    #             # 检查集合是否存在
    #             if not self.dataStore.has_collection(collection_name):
    #                 logging.warning(f"集合 {collection_name} 不存在")
    #                 continue
    #
    #             # 查询所有数据获取标签字段
    #             query_results = self.dataStore.query(
    #                 collection_name=collection_name,
    #                 filter="",  # 空过滤器查询所有
    #                 output_fields=["tag_kwd"]
    #             )
    #
    #             # 统计标签频次
    #             tag_count = {}
    #             for result in query_results:
    #                 if "tag_kwd" in result and result["tag_kwd"]:
    #                     tags = result["tag_kwd"]
    #                     # 处理标签字段，可能是字符串也可能是列表
    #                     if isinstance(tags, str):
    #                         tags = tags.split("###") if tags else []
    #
    #                     for tag in tags:
    #                         if tag:
    #                             tag_count[tag] = tag_count.get(tag, 0) + 1
    #
    #             # 将结果转换为(tag, count)元组列表
    #             for tag, count in tag_count.items():
    #                 agg_results.append((tag, count))
    #
    #         except Exception as e:
    #             logging.error(f"获取标签失败: {str(e)}")
    #
    #     return agg_results
    #
    # def all_tags_in_portion(self, tenant_id: str, kb_ids: list[str], S=1000):
    #     """
    #     获取所有标签的比例
    #
    #     Args:
    #         tenant_id: 租户ID
    #         kb_ids: 知识库ID列表
    #         S: 平滑参数
    #
    #     Returns:
    #         dict: 标签比例字典
    #     """
    #     # 获取标签统计
    #     tags_counts = self.all_tags(tenant_id, kb_ids)
    #
    #     # 计算总频次
    #     total = np.sum([c for _, c in tags_counts])
    #
    #     # 计算每个标签的比例
    #     result = {t: (c + 1) / (total + S) for t, c in tags_counts}
    #
    #     return result
    #
    # def tag_content(self, tenant_id: str, kb_ids: list[str], doc, all_tags, topn_tags=3, keywords_topn=30, S=1000):
    #     """
    #     为文档内容打标签
    #
    #     Args:
    #         tenant_id: 租户ID
    #         kb_ids: 知识库ID列表
    #         doc: 文档内容
    #         all_tags: 所有标签的比例
    #         topn_tags: 返回的标签数量
    #         keywords_topn: 关键词数量
    #         S: 平滑参数
    #
    #     Returns:
    #         bool: 是否成功添加标签
    #     """
    #     if isinstance(kb_ids, str):
    #         kb_ids = [kb_ids]
    #
    #     aggs = []
    #
    #     # 获取文档的文本内容
    #     doc_text = doc.get("title_tks", "") + " " + doc.get("content_ltks", "")
    #     important_keywords = doc.get("important_kwd", [])
    #
    #     # 创建查询匹配文本
    #     match_txt = self.qryr.paragraph(doc_text, important_keywords, keywords_topn)
    #
    #     for kb_id in kb_ids:
    #         collection_name = index_name_one(tenant_id, kb_id)
    #
    #         try:
    #             # 检查集合是否存在
    #             if not self.dataStore.has_collection(collection_name):
    #                 logging.warning(f"集合 {collection_name} 不存在")
    #                 continue
    #
    #             # 使用关键词查询相关内容
    #             query_results = self.dataStore.query(
    #                 collection_name=collection_name,
    #                 filter=f"match_phrase(title_tks, '{match_txt}') OR match_phrase(content_ltks, '{match_txt}')",
    #                 output_fields=["tag_kwd"]
    #             )
    #
    #             # 统计标签频次
    #             tag_count = {}
    #             for result in query_results:
    #                 if "tag_kwd" in result and result["tag_kwd"]:
    #                     tags = result["tag_kwd"]
    #                     # 处理标签字段，可能是字符串也可能是列表
    #                     if isinstance(tags, str):
    #                         tags = tags.split("###") if tags else []
    #
    #                     for tag in tags:
    #                         if tag:
    #                             tag_count[tag] = tag_count.get(tag, 0) + 1
    #
    #             # 将结果转换为(tag, count)元组列表
    #             for tag, count in tag_count.items():
    #                 aggs.append((tag, count))
    #
    #         except Exception as e:
    #             logging.error(f"为内容添加标签失败: {str(e)}")
    #
    #     # 如果没有获取到标签，返回失败
    #     if not aggs:
    #         return False
    #
    #     # 计算总频次
    #     cnt = np.sum([c for _, c in aggs])
    #
    #     # 计算标签特征值并排序
    #     tag_fea = sorted([(a, round(0.1 * (c + 1) / (cnt + S) / (all_tags.get(a, 0.0001)))) for a, c in aggs],
    #                      key=lambda x: x[1] * -1)[:topn_tags]
    #
    #     # 将标签特征添加到文档中
    #     doc["tag_fea"] = {a: c for a, c in tag_fea if c > 0}
    #
    #     return True
    #
    # def tag_query(self, question: str, tenant_ids: str | list[str], kb_ids: list[str], all_tags, topn_tags=3, S=1000):
    #     """
    #     为查询添加标签
    #
    #     Args:
    #         question: 查询问题
    #         tenant_ids: 租户ID（单个或列表）
    #         kb_ids: 知识库ID列表
    #         all_tags: 所有标签的比例
    #         topn_tags: 返回的标签数量
    #         S: 平滑参数
    #
    #     Returns:
    #         dict: 标签特征字典
    #     """
    #     # 统一tenant_ids格式
    #     if isinstance(tenant_ids, str):
    #         tenant_ids = [tenant_ids]
    #
    #     # 统一kb_ids格式
    #     if isinstance(kb_ids, str):
    #         kb_ids = [kb_ids]
    #
    #     # 生成查询匹配文本
    #     match_txt, _ = self.qryr.question(question, min_match=0.0)
    #
    #     aggs = []
    #
    #     # 为每个租户和知识库执行查询
    #     for tenant_id in tenant_ids:
    #         for kb_id in kb_ids:
    #             collection_name = index_name_one(tenant_id, kb_id)
    #
    #             try:
    #                 # 检查集合是否存在
    #                 if not self.dataStore.has_collection(collection_name):
    #                     logging.warning(f"集合 {collection_name} 不存在")
    #                     continue
    #
    #                 # 使用查询匹配文本搜索相关内容
    #                 query_results = self.dataStore.query(
    #                     collection_name=collection_name,
    #                     filter=f"match_phrase(title_tks, '{match_txt}') OR match_phrase(content_ltks, '{match_txt}')",
    #                     output_fields=["tag_kwd"]
    #                 )
    #
    #                 # 统计标签频次
    #                 tag_count = {}
    #                 for result in query_results:
    #                     if "tag_kwd" in result and result["tag_kwd"]:
    #                         tags = result["tag_kwd"]
    #                         # 处理标签字段，可能是字符串也可能是列表
    #                         if isinstance(tags, str):
    #                             tags = tags.split("###") if tags else []
    #
    #                         for tag in tags:
    #                             if tag:
    #                                 tag_count[tag] = tag_count.get(tag, 0) + 1
    #
    #                 # 将结果转换为(tag, count)元组列表
    #                 for tag, count in tag_count.items():
    #                     aggs.append((tag, count))
    #
    #             except Exception as e:
    #                 logging.error(f"为查询添加标签失败: {str(e)}")
    #
    #     # 如果没有获取到标签，返回空字典
    #     if not aggs:
    #         return {}
    #
    #     # 计算总频次
    #     cnt = np.sum([c for _, c in aggs])
    #
    #     # 计算标签特征值并排序
    #     tag_fea = sorted([(a, round(0.1 * (c + 1) / (cnt + S) / (all_tags.get(a, 0.0001)))) for a, c in aggs],
    #                      key=lambda x: x[1] * -1)[:topn_tags]
    #
    #     # 返回标签特征字典
    #     return {a: c for a, c in tag_fea if c > 0}
