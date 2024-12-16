import logging
import re
from dataclasses import dataclass
import numpy as np
from core.utils import rmSpace
from core.nlp import rag_tokenizer, query, is_english
from core.utils.doc_store_conn import MatchDenseExpr


def index_name(uid, kb_names):
    return [f"multirag_{uid}_{kb_name}" for kb_name in kb_names]


def index_name_one(uid, kb_name):
    return f"multirag_{uid}_{kb_name}"


class Dealer:
    def __init__(self, milvus_conn):
        self.qryr = query.MilvusQueryer(milvus_conn)
        self.qryr.flds = [
            "title_tks^10",
            "title_sm_tks^5",
            "important_kwd^30",
            "important_tks^20",
            "content_ltks^2",
            "content_sm_ltks"]
        self.milvus_conn = milvus_conn

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

    def get_vector(self, txt, emb_mdl, topk=10, similarity=0.1):
        qv, _ = emb_mdl.encode_queries(txt)
        shape = np.array(qv).shape
        if len(shape) > 1:
            raise Exception(
                f"Dealer.get_vector returned array's shape {shape} doesn't match expectation(exact one dimension).")
        embedding_data = [float(v) for v in qv]
        # todo 适配任意维度向量列名
        # vector_column_name = f"q_{len(embedding_data)}_vec"
        vector_column_name = "vector"
        return MatchDenseExpr(vector_column_name, embedding_data, 'float', 'cosine', topk, {"similarity": similarity})

    def _add_filters(self, base_filter, req):
        filters = []

        # 添加知识库ID过滤条件
        if req.get("kb_ids"):
            kb_ids_filter = f"kb_id in {tuple(req['kb_ids'])}"
            filters.append(kb_ids_filter)

        # 添加文档ID过滤条件
        if req.get("doc_ids"):
            doc_ids_filter = f"doc_id in {tuple(req['doc_ids'])}"
            filters.append(doc_ids_filter)

        # 添加知识图谱关键字过滤条件
        if req.get("knowledge_graph_kwd"):
            kg_kwd_filter = f"knowledge_graph_kwd in {tuple(req['knowledge_graph_kwd'])}"
            filters.append(kg_kwd_filter)

        # 添加可用性过滤条件
        if "available_int" in req:
            if req["available_int"] == 0:
                available_int_filter = "available_int < 1"
            else:
                available_int_filter = "available_int >= 1"
            filters.append(available_int_filter)

        # 将所有过滤条件组合成SQL风格的表达式
        if filters:
            combined_filter = " AND ".join(filters)
            base_filter = f"{base_filter} AND {combined_filter}" if base_filter else combined_filter

        return base_filter

    def search(self, req, idxnms, embd_mdl=None):
        qst = req.get("question", "")
        bqry, keywords = self.qryr.question(qst, min_match="30%")
        total, ids, fields = 0, [], {}
        if bqry is None:
            raise ValueError("Failed to generate query for the given question.")

        src = req.get("fields", ["docnm_kwd", "content_ltks", "kb_id", "img_id", "title_tks",
                                 "doc_id", "vector", "position_int", "content_with_weight"])
        filter = req.get("filter_exp", "")
        # Vector search parameters
        if req.get("vector"):
            assert embd_mdl, "No embedding model selected"
            vector_search_params = self.get_vector(qst, embd_mdl, req.get("topk", 1024), req.get("similarity", 0.1))
            query_vector = vector_search_params.embedding_data

            for idxnm in idxnms:
                logging.info(f"正在搜索的集合: {idxnm}")
                # todo 后续考虑不同维度字段检索情况，目前统一叫vector，eg.用户512维的输入无法比对718存储的vector，动态名字就可以了
                try:
                    # 在Milvus中执行搜索
                    # 参数:
                    # collection_name: 指定要搜索的集合名称
                    # data: 查询向量
                    # anns_field: 指定用于向量搜索的字段
                    # limit: 要返回的结果数量，默认为10，如果未指定的话
                    # search_params: 搜索参数，包括度量类型和nprobe值
                    # output_fields: 指定要在搜索结果中返回的字段
                    search_results = self.milvus_conn.search(
                        collection_name=idxnm,
                        data=[query_vector],
                        anns_field=vector_search_params.vector_column_name,
                        limit=req.get("size", 10),
                        search_params={"metric_type": "COSINE", "params": {"nprobe": 10}},
                        output_fields=src,
                        filter=filter
                    )

                    # logging.info(f"Search results for {idxnm}: {search_results}")
                    logging.info(f"Search results for {idxnm} ~ 详细查询数据请解开 core/nlp/search 下的注释")

                    # Process search results
                    if search_results:
                        total += len(search_results[0])
                        ids.extend([str(hit['id']) for hit in search_results[0]])
                        for hit in search_results[0]:
                            hit_fields = {}
                            for field in src:
                                hit_fields[field] = hit['entity'].get(field, "")  # Extract each field's data
                            # 将字典存储在以id为键的字段字典中
                            fields[str(hit['id'])] = hit_fields
                            # fields[str(hit['id'])] = hit['entity'].get("content_with_weight", "")  # Extract the 'text' field
                except Exception as e:
                    logging.error(f"Error searching in collection {idxnm}: {str(e)}")

        # 如果没有向量搜索条件，则执行基于doc_id的简单查询
        else:
            doc_ids = req.get("doc_ids")

            if not doc_ids:
                raise ValueError("doc_ids is required for non-vector search.")

            fields_to_return = req.get("fields", ["pk", "content_with_weight", "doc_id", "docnm_kwd", "img_id", "position_int", "auth"])

            for doc_id in doc_ids:
                logging.info(f"正在从集合 {idxnms} 获取文档 ID 为 {doc_id} 的数据")
                try:
                    # 使用 Milvus query 方法进行简单查询
                    search_results = self.milvus_conn.query(
                        collection_name=idxnms,
                        # filter=f"doc_id == {doc_id}",
                        filter=f"doc_id == '{{doc_id}}'".format(doc_id=doc_id),
                        output_fields=fields_to_return,
                        limit=req.get("size", 1024)
                    )
                    if search_results:
                        total += len(search_results)
                        for hit in search_results:
                            hit_id = str(hit["pk"])
                            ids.append(hit_id)
                            hit_fields = {field: hit.get(field, "") for field in fields_to_return}
                            fields[hit_id] = hit_fields
                    logging.info(f"Query results for {idxnms}->{doc_id}: {search_results}")
                except Exception as e:
                    logging.error(f"Error querying in collection {idxnms}->{doc_id}: {str(e)}")

        kwds = set([])
        for k in keywords:
            kwds.add(k)
            for kk in rag_tokenizer.fine_grained_tokenize(k).split():
                if len(kk) < 2:
                    continue
                if kk in kwds:
                    continue
                kwds.add(kk)

        aggs = self.getAggregation(search_results, "docnm_kwd")
        if req.get("vector"):
            return self.SearchResult(
                total=total,
                ids=ids,
                query_vector=query_vector,
                aggregation=aggs,
                highlight=self.getHighlight(search_results, keywords, "content_with_weight"),
                field=fields,
                keywords=list(kwds)
            )

        else:
            return self.QueryResult(
                total=total,
                ids=ids,
                aggregation=aggs,
                field=fields,
                keywords=list(kwds)
            )

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

    # def rerank(self, sres, query, tkweight=0.3,
    #            vtweight=0.7, cfield="content_ltks"):
    #     _, keywords = self.qryr.question(query)
    #     ins_embd = [
    #         Dealer.trans2floats(
    #             sres.field[i].get("q_%d_vec" % len(sres.query_vector), "\t".join(["0"] * len(sres.query_vector)))) for i
    #         in sres.ids]
    #     if not ins_embd:
    #         return [], [], []
    #
    #     for i in sres.ids:
    #         if isinstance(sres.field[i].get("important_kwd", []), str):
    #             sres.field[i]["important_kwd"] = [sres.field[i]["important_kwd"]]
    #     ins_tw = []
    #     for i in sres.ids:
    #         content_ltks = sres.field[i][cfield].split()
    #         title_tks = [t for t in sres.field[i].get("title_tks", "").split() if t]
    #         important_kwd = sres.field[i].get("important_kwd", [])
    #         tks = content_ltks + title_tks + important_kwd
    #         ins_tw.append(tks)
    #
    #     sim, tksim, vtsim = self.qryr.hybrid_similarity(sres.query_vector,
    #                                                     ins_embd,
    #                                                     keywords,
    #                                                     ins_tw, tkweight, vtweight)
    #     return sim, tksim, vtsim
    def rerank(self, sres, query, tkweight=0.3,
               vtweight=0.7, cfield="content_ltks"):
        _, keywords = self.qryr.question(query)
        ins_embd = [sres.query_vector for i in sres.ids]
        if not ins_embd:
            return [], [], []

        ins_tw = []
        # for i in sres.ids:
        #     tks = sres.field[i].split()
        #     ins_tw.append(tks)
        for i in sres.ids:
            content_ltks = sres.field[i][cfield].split()
            title_tks = [t for t in sres.field[i].get("title_tks", "").split() if t]
            important_kwd = sres.field[i].get("important_kwd", [])
            tks = content_ltks + title_tks + important_kwd
            ins_tw.append(tks)

        sim, tksim, vtsim = self.qryr.hybrid_similarity(sres.query_vector,
                                                        ins_embd,
                                                        keywords,
                                                        ins_tw, tkweight, vtweight)
        return sim, tksim, vtsim

    def rerank_by_model(self, rerank_mdl, sres, query, tkweight=0.3,
                        vtweight=0.7, cfield="content_ltks"):
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

        return tkweight * np.array(tksim) + vtweight * vtsim, tksim, vtsim

    def hybrid_similarity(self, ans_embd, ins_embd, ans, inst):
        return self.qryr.hybrid_similarity(ans_embd,
                                           ins_embd,
                                           rag_tokenizer.tokenize(ans).split(),
                                           rag_tokenizer.tokenize(inst).split())

    def retrieval(self, question, filter_exp, embd_mdl, tenant_id, kb_names, page, page_size, similarity_threshold=0.2,
                  vector_similarity_weight=0.3, top=1024, doc_ids=None, aggs=True, rerank_mdl=None):
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
        sres = self.search(req, idxnms, embd_mdl)
        ranks["total"] = sres.total

        if not sres.ids:
            return ranks

        if page <= RERANK_PAGE_LIMIT:
            if rerank_mdl:
                sim, tsim, vsim = self.rerank_by_model(rerank_mdl,
                                                       sres, question, 1 - vector_similarity_weight,
                                                       vector_similarity_weight)
            else:
                sim, tsim, vsim = self.rerank(
                    sres, question, 1 - vector_similarity_weight, vector_similarity_weight)
            idx = np.argsort(sim * -1)[(page - 1) * page_size:page * page_size]
        else:
            sim = tsim = vsim = [1] * len(sres.ids)
            idx = list(range(len(sres.ids)))

        dim = len(sres.query_vector)
        # start_idx = (page - 1) * page_size
        for i in idx:
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
            id = sres.ids[i]
            text = sres.field[id]["content_with_weight"]
            dnm = sres.field[id].get("docnm_kwd", "")
            did = sres.field[id]["doc_id"]
            d = {
                "chunk_id": id,
                "content_ltks": sres.field[id].get("content_ltks", ""),
                "text": text,
                "doc_id": sres.field[id]["doc_id"],
                "docnm_kwd": dnm,
                "kb_id": sres.field[id]["kb_id"],
                "important_kwd": sres.field[id].get("important_kwd", []),
                "img_id": sres.field[id].get("img_id", ""),
                "similarity": sim[i],
                "vector_similarity": vsim[i],
                "term_similarity": tsim[i],
                "vector": self.trans2floats("\t".join(map(str, sres.query_vector))),
                "positions": sres.field[id].get("position_int", "").split("\t")
            }
            # if highlight:
            #     if id in sres.highlight:
            #         d["highlight"] = rmSpace(sres.highlight[id])
            #     else:
            #         d["highlight"] = d["content_with_weight"]
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
            tbl = self.milvus_conn.sql(sql, fetch_size, format)
            return tbl
        except Exception as e:
            logging.error(f"SQL failure: {sql} =>" + str(e))
            return {"error": str(e)}

    def chunk_list(self, doc_id, tenant_id, max_count=1024, fields=None):
        if fields is None:
            fields = ["docnm_kwd", "content_with_weight", "img_id"]
        from api.db.services.document_service import DocumentService
        from api.db.services.knowledgebase_service import KnowledgebaseService
        from api.db.database import SessionLocal

        db = SessionLocal()
        kb_id = DocumentService.get_by_doc_id(db, doc_id)["kb_id"]
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        milvus_res = self.milvus_conn.query(
            collection_name=index_name_one(tenant_id, kb.name),
            filter=f"doc_id == {doc_id}",
            anns_field="doc_id",
            limit=max_count,
            output_fields=fields
        )
        res = []
        for index, chunk in enumerate(milvus_res[0]):
            res.append({fld: chunk.entity.get(fld) for fld in fields})
        return res
