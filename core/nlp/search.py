import json
import re
from copy import deepcopy
from typing import List, Optional, Dict, Union
from dataclasses import dataclass
import numpy as np
from core.settings import milvus_logger
from core.utils import rmSpace
from core.nlp import rag_tokenizer, query


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
        ids: List[str]
        query_vector: List[float] = None
        field: Optional[Dict] = None
        highlight: Optional[Dict] = None
        aggregation: Union[List, Dict, None] = None
        keywords: Optional[List[str]] = None
        group_docs: List[List] = None

    def _vector(self, txt, emb_mdl, sim=0.8, topk=10):
        qv, c = emb_mdl.encode_queries(txt)
        return {
            "field": "vector",
            "k": topk,
            "similarity": sim,
            "num_candidates": topk * 2,
            "query_vector": [float(v) for v in qv]
        }

    def search(self, req, idxnms, embd_mdl=None):
        qst = req.get("question", "")
        bqry, keywords = self.qryr.question(qst)

        if bqry is None:
            raise ValueError("Failed to generate query for the given question.")

        # Vector search parameters
        vector_search_params = self._vector(qst, embd_mdl, req.get("similarity", 0.1), req.get("topk", 1024))
        query_vector = vector_search_params["query_vector"]

        total = 0
        ids = []
        fields = {}

        for idxnm in idxnms:
            milvus_logger.info(f"Searching in collection: {idxnm}")
            try:
                # Perform the search in Milvus
                search_results = self.milvus_conn.search(
                    collection_name=idxnm,
                    data=[query_vector],
                    anns_field=vector_search_params["field"],
                    limit=req.get("size", 10),
                    search_params={"metric_type": "COSINE", "params": {"nprobe": 10}},
                    output_fields=["content_with_weight"]  # Specify the field(s) to return
                )
                milvus_logger.info(f"Search results for {idxnm}: {search_results}")

                # Process search results
                if search_results:
                    total += len(search_results[0])
                    ids.extend([str(hit['id']) for hit in search_results[0]])
                    for hit in search_results[0]:
                        fields[str(hit['id'])] = hit['entity'].get("content_with_weight", "")  # Extract the 'text' field
            except Exception as e:
                milvus_logger.error(f"Error searching in collection {idxnm}: {str(e)}")

        return self.SearchResult(
            total=total,
            ids=ids,
            query_vector=query_vector,
            field=fields,
            keywords=keywords
        )

    @staticmethod
    def trans2floats(txt):
        return [float(t) for t in txt.split("\t")]

    def insert_citations(self, answer, chunks, chunk_v,
                         embd_mdl, tkweight=0.1, vtweight=0.9):
        assert len(chunks) == len(chunk_v)
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
        milvus_logger.info("{} => {}".format(answer, pieces_))
        if not pieces_:
            return answer, set([])

        ans_v, _ = embd_mdl.encode(pieces_)
        assert len(ans_v[0]) == len(chunk_v[0]), "The dimension of query and chunk do not match: {} vs. {}".format(
            len(ans_v[0]), len(chunk_v[0]))

        chunks_tks = [rag_tokenizer.tokenize(self.qryr.rmWWW(ck)).split(" ")
                      for ck in chunks]
        cites = {}
        thr = 0.63
        while thr > 0.3 and len(cites.keys()) == 0 and pieces_ and chunks_tks:
            for i, a in enumerate(pieces_):
                sim, tksim, vtsim = self.qryr.hybrid_similarity(ans_v[i],
                                                                chunk_v,
                                                                rag_tokenizer.tokenize(
                                                                    self.qryr.rmWWW(pieces_[i])).split(" "),
                                                                chunks_tks,
                                                                tkweight, vtweight)
                mx = np.max(sim) * 0.99
                milvus_logger.info("{} SIM: {}".format(pieces_[i], mx))
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
    #         content_ltks = sres.field[i][cfield].split(" ")
    #         title_tks = [t for t in sres.field[i].get("title_tks", "").split(" ") if t]
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
        for i in sres.ids:
            tks = sres.field[i].split(" ")
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
            content_ltks = sres.field[i][cfield].split(" ")
            title_tks = [t for t in sres.field[i].get("title_tks", "").split(" ") if t]
            important_kwd = sres.field[i].get("important_kwd", [])
            tks = content_ltks + title_tks + important_kwd
            ins_tw.append(tks)

        tksim = self.qryr.token_similarity(keywords, ins_tw)
        vtsim, _ = rerank_mdl.similarity(" ".join(keywords), [rmSpace(" ".join(tks)) for tks in ins_tw])

        return tkweight * np.array(tksim) + vtweight * vtsim, tksim, vtsim

    def hybrid_similarity(self, ans_embd, ins_embd, ans, inst):
        return self.qryr.hybrid_similarity(ans_embd,
                                           ins_embd,
                                           rag_tokenizer.tokenize(ans).split(" "),
                                           rag_tokenizer.tokenize(inst).split(" "))

    def retrieval(self, question, embd_mdl, tenant_id, kb_names, page, page_size, similarity_threshold=0.2,
                  vector_similarity_weight=0.3, top=1024, doc_ids=None, aggs=True, rerank_mdl=None):
        ranks = {"total": 0, "chunks": [], "doc_aggs": {}}
        if not question:
            return ranks
        req = {"kb_names": kb_names, "doc_ids": doc_ids, "size": page_size,
               "question": question, "vector": True, "topk": top,
               "similarity": similarity_threshold,
               "available_int": 1}
        idxnms = index_name(tenant_id, kb_names)
        sres = self.search(req, idxnms, embd_mdl)

        if not sres.ids:
            return ranks

        if rerank_mdl:
            sim, tsim, vsim = self.rerank_by_model(rerank_mdl,
                                                   sres, question, 1 - vector_similarity_weight,
                                                   vector_similarity_weight)
        else:
            sim, tsim, vsim = self.rerank(
                sres, question, 1 - vector_similarity_weight, vector_similarity_weight)
        idx = np.argsort(sim * -1)

        dim = len(sres.query_vector)
        start_idx = (page - 1) * page_size
        for i in idx:
            if sim[i] < similarity_threshold:
                break
            ranks["total"] += 1
            start_idx -= 1
            if start_idx >= 0:
                continue
            if len(ranks["chunks"]) >= page_size:
                if aggs:
                    continue
                break
            id = sres.ids[i]
            text = sres.field[id]
            d = {
                "chunk_id": id,
                "text": text,
                "similarity": sim[i],
                "vector_similarity": vsim[i],
                "term_similarity": tsim[i],
                "vector": self.trans2floats("\t".join(map(str, sres.query_vector))),
                # Ensure this is the correct format
            }
            ranks["chunks"].append(d)
            # 对文档聚合结果进行排序，依据是文档出现的次数，次数多的排在前面
            # ranks["doc_aggs"] = [{"doc_name": k,
            #                       "doc_id": v["doc_id"],
            #                       "count": v["count"]} for k,
            #                      v in sorted(ranks["doc_aggs"].items(),
            #                                  key=lambda x: x[1]["count"] * -1)]
        # Ensure doc_aggs is a dictionary
        if not isinstance(ranks["doc_aggs"], dict):
            ranks["doc_aggs"] = {}

        # 对文档聚合结果进行排序，依据是文档出现的次数，次数多的排在前面
        ranks["doc_aggs"] = [{"doc_name": k,
                              "doc_id": v["doc_id"],
                              "count": v["count"]} for k,
                             v in sorted(ranks["doc_aggs"].items(),
                                         key=lambda x: x[1]["count"] * -1)]
        return ranks

    def sql_retrieval(self, sql, fetch_size=128, format="json"):
        from api.settings import chat_logger
        sql = re.sub(r"[ `]+", " ", sql)
        sql = sql.replace("%", "")
        milvus_logger.info(f"Get es sql: {sql}")
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
        chat_logger.info(f"To es: {sql}")

        try:
            tbl = self.milvus_conn.sql(sql, fetch_size, format)
            return tbl
        except Exception as e:
            chat_logger.error(f"SQL failure: {sql} =>" + str(e))
            return {"error": str(e)}

    def chunk_list(self, doc_id, tenant_id, max_count=1024, fields=["docnm_kwd", "content_with_weight", "img_id"]):
        s = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"doc_id": doc_id}}
                    ]
                }
            },
            "_source": fields,
            "size": max_count
        }
        milvus_res = self.milvus_conn.search(
            collection_name=index_name(tenant_id, kb_names),
            data=s["query"]["bool"]["must"],
            anns_field="doc_id",
            limit=max_count,
            search_params={"metric_type": "COSINE", "params": {"nprobe": 10}}
        )
        res = []
        for index, chunk in enumerate(milvus_res[0]):
            res.append({fld: chunk.entity.get(fld) for fld in fields})
        return res
