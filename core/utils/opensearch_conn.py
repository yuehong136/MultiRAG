#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import copy
import json
import logging
import math
import os
import re
import time
from collections.abc import Iterator
from typing import Any

from opensearchpy import ConnectionTimeout, Index, NotFoundError, OpenSearch, Q, Search, UpdateByQuery
from opensearchpy.helpers import scan

from common import settings
from common.constants import PAGERANK_FLD, TAG_FLD
from common.decorator import singleton
from common.doc_store.doc_store_base import EXACT_SCORE_NO_VECTOR, DocStoreConnection, FusionExpr, MatchDenseExpr, MatchExpr, MatchTextExpr, OrderByExpr
from common.doc_store.search_metadata import SearchMetadata
from common.file_utils import get_project_base_directory
from core.nlp import is_english, rag_tokenizer

ATTEMPT_TIME = 2

_PAGERANK_FEA_ADJUST_SCRIPT = """
double cur = 0.0;
if (ctx._source.containsKey(params.pf)) {
  Object v = ctx._source[params.pf];
  if (v != null) {
    if (v instanceof Number) {
      cur = ((Number)v).doubleValue();
    } else {
      try { cur = Double.parseDouble(v.toString()); } catch (Exception e) { cur = 0.0; }
    }
  }
}
double nw = cur + params.delta;
if (nw < params.min_w) { nw = params.min_w; }
if (nw > params.max_w) { nw = params.max_w; }
if (nw <= 0.0) {
  if (ctx._source.containsKey(params.pf)) {
    ctx._source.remove(params.pf);
  }
} else {
  ctx._source[params.pf] = nw;
}
"""

logger = logging.getLogger("multirag.opensearch_conn")


@singleton
class OSConnection(SearchMetadata, DocStoreConnection):
    def __init__(self):
        self.info = {}
        logger.info(f"Use OpenSearch {settings.OS['hosts']} as the doc engine.")
        for _ in range(ATTEMPT_TIME):
            try:
                self.os = OpenSearch(
                    settings.OS["hosts"].split(","),
                    http_auth=(settings.OS["username"], settings.OS["password"]) if "username" in settings.OS and "password" in settings.OS else None,
                    verify_certs=False,
                    timeout=600,
                )
                if self.os:
                    self.info = self.os.info()
                    break
            except Exception as e:
                logger.warning(f"{e!s}. Waiting OpenSearch {settings.OS['hosts']} to be healthy.")
                time.sleep(5)
        if not self.os.ping():
            msg = f"OpenSearch {settings.OS['hosts']} is unhealthy in 120s."
            logger.error(msg)
            raise Exception(msg)
        v = self.info.get("version", {"number": "2.18.0"})
        v = v["number"].split(".")[0]
        if int(v) < 2:
            msg = f"OpenSearch version must be greater than or equal to 2, current version: {v}"
            logger.error(msg)
            raise Exception(msg)
        fp_mapping = os.path.join(get_project_base_directory(), "configs", "os_mapping.json")
        if not os.path.exists(fp_mapping):
            msg = f"OpenSearch mapping file not found at {fp_mapping}"
            logger.error(msg)
            raise Exception(msg)
        with open(fp_mapping) as f:
            self.mapping = json.load(f)
        logger.info(f"OpenSearch {settings.OS['hosts']} is healthy.")

    """
    Database operations
    """

    def db_type(self) -> str:
        return "opensearch"

    def health(self) -> dict:
        health_dict = dict(self.os.cluster.health())
        health_dict["type"] = "opensearch"
        return health_dict

    """
    Table operations
    """

    def create_idx(self, indexName: str, knowledgebaseId: str, vectorSize: int, parser_id: str = None):
        if self.index_exist(indexName, knowledgebaseId):
            return True
        try:
            from opensearchpy.client import IndicesClient

            return IndicesClient(self.os).create(index=indexName, body=self.mapping)
        except Exception:
            logger.exception("OSConnection.createIndex error %s" % (indexName))

    def _doc_meta_client(self) -> Any:
        return self.os

    def create_doc_meta_idx(self, index_name: str) -> bool:
        if self.doc_meta_index_exists(index_name):
            return True
        try:
            with open(os.path.join(get_project_base_directory(), "configs", "doc_meta_es_mapping.json")) as stream:
                mapping = json.load(stream)
            mappings = mapping["mappings"]
            # OpenSearch does not support ES runtime fields; keep the shared file intact.
            if mappings.get("dynamic") == "runtime":
                mappings = {**mappings, "dynamic": True}
            response = self.os.indices.create(index=index_name, body={"settings": mapping["settings"], "mappings": mappings})
            return response.get("acknowledged") is True
        except Exception:
            logger.exception("OpenSearch metadata index creation failed: %s", index_name)
            return False

    def delete_idx(self, indexName: str, knowledgebaseId: str):
        if len(knowledgebaseId) > 0:
            # The index need to be alive after any kb deletion since all kb under this tenant are in one index.
            return
        try:
            self.os.indices.delete(index=indexName, allow_no_indices=True)
        except NotFoundError:
            pass
        except Exception:
            logger.exception("OSConnection.delete_idx error %s" % (indexName))

    def index_exist(self, indexName: str, knowledgebaseId: str = None) -> bool:
        s = Index(indexName, self.os)
        for i in range(ATTEMPT_TIME):
            try:
                return s.exists()
            except Exception as e:
                logger.exception("OSConnection.index_exist got exception")
                if str(e).find("Timeout") > 0 or str(e).find("Conflict") > 0:
                    continue
                break
        return False

    """
    CRUD operations
    """

    def iter_document_chunks(self, index_name: str, kb_id: str, doc_id: str, fields: list[str]) -> Iterator[dict[str, Any]]:
        """Complete scoped scroll; the SDK clears its cursor on every exit."""
        if not index_name or not kb_id or not doc_id:
            raise ValueError("A document scroll requires an explicit dataset binding")
        body: dict[str, Any] = {"_source": fields or False, "query": {"bool": {"filter": [{"term": {"kb_id": kb_id}}, {"term": {"doc_id": doc_id}}]}}}
        for hit in scan(self.os, index=index_name, query=body, size=128, scroll="2m", raise_on_error=True, clear_scroll=True):
            row = self.get_fields({"hits": {"hits": [hit]}}, fields).get(hit["_id"], {})
            yield {**row, "id": hit["_id"]}

    def search(
        self,
        select_fields: list[str],
        highlight_fields: list[str],
        condition: dict[str, Any],
        match_expressions: list[MatchExpr],
        order_by: OrderByExpr | None,
        offset: int,
        limit: int,
        index_names: str | list[str],
        knowledgebase_ids: list[str],
        agg_fields: list[str] | None = None,
        rank_feature: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Refers to https://github.com/opensearch-project/opensearch-py/blob/main/guides/dsl.md
        """
        if isinstance(index_names, str):
            index_names = index_names.split(",")
        assert isinstance(index_names, list) and len(index_names) > 0
        assert "_id" not in condition

        bqry = Q("bool", must=[])
        for k, v in {**condition, "kb_id": knowledgebase_ids}.items():
            if k == "available_int":
                if v == 0:
                    bqry.filter.append(Q("range", available_int={"lt": 1}))
                else:
                    bqry.filter.append(Q("bool", must_not=Q("range", available_int={"lt": 1})))
                continue
            if not v:
                continue
            if isinstance(v, list):
                bqry.filter.append(Q("terms", **{k: v}))
            elif isinstance(v, str) or isinstance(v, int):
                bqry.filter.append(Q("term", **{k: v}))
            else:
                raise Exception(f"Condition `{k!s}={v!s}` value type is {type(v)!s}, expected to be int, str or list.")

        s = Search()
        # Snapshot only scalar scope: lexical matches must not filter kNN candidates.
        scalar_filter = bqry.to_dict()
        text_similarity_weight = 1.0
        vector_similarity_weight = 1.0
        for m in match_expressions:
            if isinstance(m, FusionExpr) and m.method == "weighted_sum" and "weights" in m.fusion_params:
                assert (
                    len(match_expressions) == 3
                    and isinstance(match_expressions[0], MatchTextExpr)
                    and isinstance(match_expressions[1], MatchDenseExpr)
                    and isinstance(match_expressions[2], FusionExpr)
                )
                weights = m.fusion_params["weights"]
                text_similarity_weight, vector_similarity_weight = (float(value) for value in weights.split(","))
        candidate_queries: list[dict[str, Any]] = []
        for m in match_expressions:
            if isinstance(m, MatchTextExpr):
                minimum_should_match = m.extra_options.get("minimum_should_match", 0.0)
                if isinstance(minimum_should_match, float):
                    minimum_should_match = str(int(minimum_should_match * 100)) + "%"
                candidate_queries.append(
                    Q("query_string", fields=m.fields, type="best_fields", query=m.matching_text, minimum_should_match=minimum_should_match, boost=text_similarity_weight).to_dict()
                )

            # Elasticsearch has the encapsulation of KNN_search in python sdk
            # while the Python SDK for OpenSearch does not provide encapsulation for KNN_search,
            # the following codes implement KNN_search in OpenSearch using DSL
            # Besides, Opensearch's DSL for KNN_search query syntax differs from that in Elasticsearch, I also made some adaptions for it
            elif isinstance(m, MatchDenseExpr):
                if m.extra_options.get("exact"):
                    if not condition.get("id") or len(match_expressions) != 1:
                        raise ValueError("Exact vector scoring requires explicit candidate IDs")
                    candidate_queries.append(
                        {
                            "script_score": {
                                "query": scalar_filter,
                                "script": {
                                    "source": f"doc[params.field].size() == 0 ? {EXACT_SCORE_NO_VECTOR} : 1.0 + cosineSimilarity(params.vector, doc[params.field])",
                                    "params": {"vector": list(m.embedding_data), "field": m.vector_column_name},
                                },
                            }
                        }
                    )
                    continue
                # OpenSearch k and min_score are mutually exclusive. Dealer applies
                # the similarity threshold after reranking; it is not a score boost.
                candidate_queries.append(
                    {
                        "knn": {
                            m.vector_column_name: {
                                "vector": list(m.embedding_data),
                                "k": m.topn,
                                "filter": scalar_filter,
                                "boost": vector_similarity_weight,
                            }
                        }
                    }
                )

        if bqry and rank_feature:
            for fld, sc in rank_feature.items():
                if fld != PAGERANK_FLD:
                    fld = f"{TAG_FLD}.{fld}"
                bqry.should.append(Q("rank_feature", field=fld, linear={}, boost=sc))

        if bqry:
            s = s.query(bqry)
        for field in highlight_fields:
            s = s.highlight(field, force_source=True, no_match_size=30, require_field_match=False)

        if order_by:
            orders = []
            for field, order in order_by.fields:
                order = "asc" if order == 0 else "desc"
                if field in ["page_num_int", "top_int"]:
                    order_info = {"order": order, "unmapped_type": "float", "mode": "avg", "numeric_type": "double"}
                elif field.endswith("_int") or field.endswith("_flt"):
                    order_info = {"order": order, "unmapped_type": "float"}
                else:
                    order_info = {"order": order, "unmapped_type": "text"}
                orders.append({field: order_info})
            s = s.sort(*orders)

        for fld in agg_fields or []:
            s.aggs.bucket(f"aggs_{fld}", "terms", field=fld, size=1000000)

        if limit > 0:
            s = s[offset : offset + limit]
        if select_fields:
            s = s.source(select_fields)
        elif any(isinstance(expr, MatchDenseExpr) and expr.extra_options.get("exact") for expr in match_expressions):
            s = s.source(False)
        q = s.to_dict()
        if candidate_queries:
            # opensearch-py 2.7 has no kNN DSL class, so insert the raw clauses
            # after serialization. Rank features cannot admit unrelated rows.
            q["query"]["bool"].setdefault("must", []).append({"bool": {"should": candidate_queries, "minimum_should_match": 1}})
        logger.debug(f"OSConnection.search {index_names!s} query: " + json.dumps(q))

        for i in range(ATTEMPT_TIME):
            try:
                res = self.os.search(
                    index=index_names,
                    body=q,
                    timeout=600,
                    # search_type="dfs_query_then_fetch",
                    track_total_hits=True,
                )
                if res.get("_shards", {}).get("failed", 0):
                    raise RuntimeError("Partial search shard failure")
                if str(res.get("timed_out", "")).lower() == "true":
                    raise Exception("OpenSearch Timeout.")
                logger.debug(f"OSConnection.search {index_names!s} res: " + str(res))
                return res
            except Exception as e:
                logger.exception(f"OSConnection.search {index_names!s} query: " + str(q))
                if str(e).find("Timeout") > 0:
                    continue
                raise e
        logger.error(f"OSConnection.search timeout for {ATTEMPT_TIME} times!")
        raise Exception("OSConnection.search timeout.")

    def get(self, chunkId: str, indexName: str, knowledgebaseIds: list[str]) -> dict | None:
        for i in range(ATTEMPT_TIME):
            try:
                res = self.os.get(
                    index=(indexName),
                    id=chunkId,
                    _source=True,
                )
                if str(res.get("timed_out", "")).lower() == "true":
                    raise Exception("Es Timeout.")
                chunk = res["_source"]
                chunk["id"] = chunkId
                return chunk
            except NotFoundError:
                return None
            except Exception as e:
                logger.exception(f"OSConnection.get({chunkId}) got exception")
                if str(e).find("Timeout") > 0:
                    continue
                raise e
        logger.error(f"OSConnection.get timeout for {ATTEMPT_TIME} times!")
        raise Exception("OSConnection.get timeout.")

    def insert(self, documents: list[dict], indexName: str, knowledgebaseId: str = None) -> list[str]:
        # Refers to https://opensearch.org/docs/latest/api-reference/document-apis/bulk/
        operations = []
        for d in documents:
            assert "_id" not in d
            assert "id" in d
            d_copy = copy.deepcopy(d)
            meta_id = d_copy.get("id", "")
            operations.append({"index": {"_index": indexName, "_id": meta_id}})
            operations.append(d_copy)

        res = []
        for _ in range(ATTEMPT_TIME):
            try:
                res = []
                r = self.os.bulk(index=(indexName), body=operations, refresh="wait_for", timeout=60)
                if r["errors"] is False:
                    return res

                for item in r["items"]:
                    for action in ["create", "delete", "index", "update"]:
                        if action in item and "error" in item[action]:
                            res.append(str(item[action]["_id"]) + ":" + str(item[action]["error"]))
                return res or ["OpenSearch bulk write failed without item errors"]
            except Exception as e:
                res = [str(e)]
                logger.warning("OSConnection.insert got exception: " + str(e))
                if re.search(r"(Timeout|time out)", str(e), re.IGNORECASE):
                    time.sleep(3)
                    continue
                return res
        return res

    def update(self, condition: dict, newValue: dict, indexName: str, knowledgebaseId: str) -> bool:
        from common.doc_store.availability import availability_script, availability_search_parents, complete_availability_update

        availability = set(newValue) == {"available_int"} and "doc_id" in condition
        if availability:
            condition = {**condition, "kb_id": knowledgebaseId}
        doc = copy.deepcopy(newValue)
        doc.pop("id", None)
        if "id" in condition and isinstance(condition["id"], str):
            # update specific single document
            chunkId = condition["id"]
            for i in range(ATTEMPT_TIME):
                try:
                    self.os.update(index=indexName, id=chunkId, body={"doc": doc})
                    return True
                except Exception as e:
                    logger.exception(f"OSConnection.update(index={indexName}, id={id}, doc={json.dumps(condition, ensure_ascii=False)}) got exception")
                    if re.search(r"(timeout|connection)", str(e).lower()):
                        continue
                    break
            return False

        # update unspecific maybe-multiple documents
        bqry = Q("bool")
        for k, v in condition.items():
            if not isinstance(k, str) or not v:
                continue
            if k == "exists":
                bqry.filter.append(Q("exists", field=v))
                continue
            if isinstance(v, list):
                bqry.filter.append(Q("terms", **{k: v}))
            elif isinstance(v, str) or isinstance(v, int):
                bqry.filter.append(Q("term", **{k: v}))
            else:
                raise Exception(f"Condition `{k!s}={v!s}` value type is {type(v)!s}, expected to be int, str or list.")
        scripts = []
        params = {}
        for k, v in newValue.items():
            if k == "remove":
                if isinstance(v, str):
                    scripts.append(f"ctx._source.remove('{v}');")
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        scripts.append(f"int i=ctx._source.{kk}.indexOf(params.p_{kk});ctx._source.{kk}.remove(i);")
                        params[f"p_{kk}"] = vv
                continue
            if k == "add":
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        scripts.append(f"ctx._source.{kk}.add(params.pp_{kk});")
                        params[f"pp_{kk}"] = vv.strip()
                continue
            if (not isinstance(k, str) or not v) and k != "available_int":
                continue
            if isinstance(v, str):
                v = re.sub(r"(['\n\r]|\\.)", " ", v)
                params[f"pp_{k}"] = v
                scripts.append(f"ctx._source.{k}=params.pp_{k};")
            elif isinstance(v, int) or isinstance(v, float):
                scripts.append(f"ctx._source.{k}={v};")
            elif isinstance(v, list):
                scripts.append(f"ctx._source.{k}=params.pp_{k};")
                params[f"pp_{k}"] = json.dumps(v, ensure_ascii=False)
            else:
                raise Exception(f"newValue `{k!s}={v!s}` value type is {type(v)!s}, expected to be int, str.")
        ubq = UpdateByQuery(index=indexName).using(self.os).query(bqry)
        ubq = ubq.script(source="".join(scripts), params=params)
        ubq = ubq.params(refresh=True)
        ubq = ubq.params(slices=5)
        ubq = ubq.params(conflicts="proceed")

        for _ in range(ATTEMPT_TIME):
            try:
                if availability:
                    parents = availability_search_parents(self.os, indexName, bqry.to_dict())
                    ubq = ubq.script(**availability_script(int(newValue["available_int"]), parents))
                response = ubq.execute()
                return complete_availability_update(response) if availability else True
            except Exception as e:
                logger.error("OSConnection.update got exception: " + str(e) + "\n".join(scripts))
                if re.search(r"(timeout|connection|conflict)", str(e).lower()):
                    continue
                break
        return False

    def adjust_chunk_pagerank_fea(
        self,
        chunk_id: str,
        indexName: str,
        knowledgebaseId: str,
        delta: float,
        min_w: float = 0.0,
        max_w: float = 100.0,
        row_id: int | None = None,
    ) -> bool:
        _ = knowledgebaseId, row_id
        for _attempt in range(ATTEMPT_TIME):
            try:
                self.os.update(
                    index=indexName,
                    id=chunk_id,
                    retry_on_conflict=3,
                    body={
                        "script": {
                            "source": _PAGERANK_FEA_ADJUST_SCRIPT.strip(),
                            "lang": "painless",
                            "params": {
                                "pf": PAGERANK_FLD,
                                "delta": float(delta),
                                "min_w": float(min_w),
                                "max_w": float(max_w),
                            },
                        }
                    },
                )
                return True
            except Exception as e:
                logger.exception(
                    "OSConnection.adjust_chunk_pagerank_fea(index=%s, id=%s): %s",
                    indexName,
                    chunk_id,
                    e,
                )
                if re.search(r"(timeout|connection|conflict)", str(e).lower()):
                    continue
                break
        return False

    def delete(self, condition: dict, indexName: str, knowledgebaseId: str) -> int:
        assert "_id" not in condition
        condition["kb_id"] = knowledgebaseId

        # Build a bool query that combines id filter with other conditions
        bool_query = Q("bool")

        # Handle chunk IDs if present
        if "id" in condition:
            chunk_ids = condition["id"]
            if not isinstance(chunk_ids, list):
                chunk_ids = [chunk_ids]
            if chunk_ids:
                # Filter by specific chunk IDs
                bool_query.filter.append(Q("ids", values=chunk_ids))
            # If chunk_ids is empty, we don't add an ids filter - rely on other conditions

        # Add all other conditions as filters
        for k, v in condition.items():
            if k == "id":
                continue  # Already handled above
            if k == "exists":
                bool_query.filter.append(Q("exists", field=v))
            elif k == "must_not":
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        if kk == "exists":
                            bool_query.must_not.append(Q("exists", field=vv))
            elif isinstance(v, list):
                bool_query.must.append(Q("terms", **{k: v}))
            elif isinstance(v, str) or isinstance(v, int):
                bool_query.must.append(Q("term", **{k: v}))
            elif v is not None:
                raise Exception("Condition value must be int, str or list.")

        # If no filters were added, use match_all (for tenant-wide operations)
        if not bool_query.filter and not bool_query.must and not bool_query.must_not:
            qry = Q("match_all")
        else:
            qry = bool_query
        logger.debug("OSConnection.delete query: " + json.dumps(qry.to_dict()))
        for _ in range(ATTEMPT_TIME):
            try:
                # print(Search().query(qry).to_dict(), flush=True)
                res = self.os.delete_by_query(index=indexName, body=Search().query(qry).to_dict(), refresh=True)
                return res["deleted"]
            except Exception as e:
                logger.warning("OSConnection.delete got exception: " + str(e))
                if re.search(r"(timeout|connection)", str(e).lower()):
                    time.sleep(3)
                    continue
                if re.search(r"(not_found)", str(e), re.IGNORECASE):
                    return 0
        return 0

    """
    Helper functions for search result
    """

    def get_total(self, res):
        if isinstance(res["hits"]["total"], type({})):
            return res["hits"]["total"]["value"]
        return res["hits"]["total"]

    def get_doc_ids(self, res):
        return [d["_id"] for d in res["hits"]["hits"]]

    @staticmethod
    def get_scores(res: Any) -> dict[str, float]:
        """Read the second-pass score headers (also used by Elasticsearch)."""
        scores: dict[str, float] = {}
        for hit in res["hits"]["hits"]:
            identifier, raw = hit["_id"], hit["_score"]
            if not isinstance(identifier, str) or not identifier or identifier in scores or isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
                raise ValueError("Invalid or duplicate candidate score")
            scores[identifier] = float(raw)
        return scores

    def __getSource(self, res):
        rr = []
        for d in res["hits"]["hits"]:
            d["_source"]["id"] = d["_id"]
            d["_source"]["_score"] = d["_score"]
            rr.append(d["_source"])
        return rr

    def get_fields(self, res, fields: list[str]) -> dict[str, dict]:
        res_fields = {}
        if not fields:
            return {}
        for d in self.__getSource(res):
            m = {n: d.get(n) for n in fields if d.get(n) is not None}
            for n, v in m.items():
                if isinstance(v, list):
                    m[n] = v
                    continue
                if not isinstance(v, str):
                    m[n] = str(m[n])
                # if n.find("tks") > 0:
                #     m[n] = remove_redundant_spaces(m[n])

            if m:
                res_fields[d["id"]] = m
        return res_fields

    def get_highlight(self, res, keywords: list[str], fieldnm: str):
        ans = {}
        for d in res["hits"]["hits"]:
            hlts = d.get("highlight")
            if not hlts:
                continue
            txt = "...".join(list(next(iter(hlts.items()))[1]))
            if not is_english(txt.split()):
                ans[d["_id"]] = txt
                continue

            txt = d["_source"][fieldnm]
            txt = re.sub(r"[\r\n]", " ", txt, flags=re.IGNORECASE | re.MULTILINE)
            txts = []
            for t in re.split(r"[.?!;\n]", txt):
                for w in keywords:
                    t = re.sub(r"(^|[ .?/'\"\(\)!,:;-])(%s)([ .?/'\"\(\)!,:;-])" % re.escape(w), r"\1<em>\2</em>\3", t, flags=re.IGNORECASE | re.MULTILINE)
                if not re.search(r"<em>[^<>]+</em>", t, flags=re.IGNORECASE | re.MULTILINE):
                    continue
                txts.append(t)
            ans[d["_id"]] = "...".join(txts) if txts else "...".join(list(next(iter(hlts.items()))[1]))

        return ans

    def get_aggregation(self, res, fieldnm: str):
        agg_field = "aggs_" + fieldnm
        if "aggregations" not in res or agg_field not in res["aggregations"]:
            return []
        bkts = res["aggregations"][agg_field]["buckets"]
        return [(b["key"], b["doc_count"]) for b in bkts]

    """
    SQL
    """

    def sql(self, sql: str, fetch_size: int, format: str):
        logger.debug(f"OSConnection.sql get sql: {sql}")
        sql = re.sub(r"[ `]+", " ", sql)
        sql = sql.replace("%", "")
        replaces = []
        for r in re.finditer(r" ([a-z_]+_l?tks)( like | ?= ?)'([^']+)'", sql):
            fld, v = r.group(1), r.group(3)
            match = f" MATCH({fld}, '{rag_tokenizer.fine_grained_tokenize(rag_tokenizer.tokenize(v))}', 'operator=OR;minimum_should_match=30%') "
            replaces.append((f"{r.group(1)}{r.group(2)}'{r.group(3)}'", match))

        for p, r in replaces:
            sql = sql.replace(p, r, 1)
        logger.debug(f"OSConnection.sql to os: {sql}")

        for i in range(ATTEMPT_TIME):
            try:
                res = self.os.sql.query(body={"query": sql, "fetch_size": fetch_size}, format=format, request_timeout="2s")
                return res
            except ConnectionTimeout:
                logger.exception("OSConnection.sql timeout")
                continue
            except Exception:
                logger.exception("OSConnection.sql got exception")
                return None
        logger.error(f"OSConnection.sql timeout for {ATTEMPT_TIME} times!")
        return None
