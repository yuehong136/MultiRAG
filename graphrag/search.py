import json
from copy import deepcopy

import pandas as pd

from core.nlp.search import Dealer


class KGSearch(Dealer):
    def search(self, req, idxnm, emb_mdl=None):
        def merge_into_first(sres, title="") -> dict[str, str]:
            df, texts = [], []
            for d in sres["hits"]:
                try:
                    df.append(json.loads(d["content_with_weight"]))
                except Exception:
                    texts.append(d["content_with_weight"])
                    pass
            if not df and not texts: return False
            if df:
                try:
                    sres["hits"][0]["content_with_weight"] = title + "\n" + pd.DataFrame(df).to_csv()
                except Exception as e:
                    pass
            else:
                sres["hits"][0]["content_with_weight"] = title + "\n" + "\n".join(texts)
            return True

        src = req.get("fields", ["docnm_kwd", "content_ltks", "kb_id", "img_id", "title_tks", "important_kwd",
                                 "image_id", "doc_id", "position_int", "name_kwd","vector", "available_int",
                                 "content_with_weight","weight_int", "weight_flt", "rank_int"
                                 ])

        qst = req.get("question", "")
        binary_query, keywords = self.qryr.question(qst, min_match="5%")
        binary_query = self._add_filters(binary_query, req)

        ## Entity retrieval
        bqry = deepcopy(binary_query)
        # 执行向量查询并替换 Elasticsearch 的查询方式
        vector_search_params = self.get_vector(qst, emb_mdl, 1024, req.get("similarity", 0.1))
        q_vec = vector_search_params.embedding_data

        # 在 Milvus 中执行实体查询
        entity_res = self.milvus_conn.search(
            collection_name=idxnm,
            data=[q_vec],
            anns_field=vector_search_params.vector_column_name,
            limit=req.get("size", 32),
            search_params={"metric_type": "COSINE", "params": {"nprobe": 10}},
            output_fields=src
        )

        entities = [d["name_kwd"] for d in entity_res[0]]
        entity_ids = [d["id"] for d in entity_res[0]]

        if merge_into_first(entity_res, "-Entities-"):
            entity_ids = entity_ids[0:1]

        ## Community retrieval
        comm_res = self.milvus_conn.search(
            collection_name=idxnm,
            data=[q_vec],
            anns_field=vector_search_params["field"],
            limit=req.get("size", 32),
            search_params={"metric_type": "COSINE", "params": {"nprobe": 10}},
            output_fields=src
        )

        comm_ids = [d["id"] for d in comm_res[0]]
        if merge_into_first(comm_res, "-Community Report-"):
            comm_ids = comm_ids[0:1]

        ## Text content retrieval
        text_res = self.milvus_conn.search(
            collection_name=idxnm,
            data=[q_vec],
            anns_field=vector_search_params["field"],
            limit=req.get("size", 6),
            search_params={"metric_type": "COSINE", "params": {"nprobe": 10}},
            output_fields=src
        )

        text_ids = [d["id"] for d in text_res[0]]
        if merge_into_first(text_res, "-Original Content-"):
            text_ids = text_ids[0:1]

        return self.SearchResult(
            total=len(entity_ids) + len(comm_ids) + len(text_ids),
            ids=[*entity_ids, *comm_ids, *text_ids],
            query_vector=q_vec,
            aggregation=None,
            field={**self.getFields(entity_res, src), **self.getFields(comm_res, src), **self.getFields(text_res, src)},
            keywords=[]
        )