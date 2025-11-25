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
import logging
from typing import Any, Literal
from tavily import TavilyClient
from common.misc_utils import get_uuid
from core.nlp import rag_tokenizer


class Tavily:
    def __init__(self, api_key: str):
        self.tavily_client = TavilyClient(api_key=api_key)

    def search(self, query: str) -> list[dict[str, Any]]:
        try:
            response = self.tavily_client.search(
                query=query,
                search_depth="advanced",
                max_results=6
            )
            return [{"url": res["url"], "title": res["title"], "content": res["content"], "score": res["score"]} for res in response["results"]]
        except Exception as e:
            logging.exception(e)

        return []

    def extract(
        self,
        urls: str | list[str],
        include_images: bool = False,
        extract_depth: Literal["basic", "advanced"] = "basic",
        format: Literal["markdown", "text"] = "markdown",
        timeout: int | None = None,
        include_favicon: bool = False,
    ) -> dict[str, Any]:
        """Thin wrapper around TavilyClient.extract: no local validation, direct passthrough."""
        try:
            kwargs = {
                "urls": urls,
                "include_images": include_images,
                "extract_depth": extract_depth,
                "format": format,
                "include_favicon": include_favicon,
            }
            # 仅在非空时传入 timeout，避免 SDK 内部对 None 执行 min()
            if timeout is not None:
                kwargs["timeout"] = timeout

            response = self.tavily_client.extract(**kwargs)
            return response
        except Exception as e:
            logging.exception(e)
            return {"results": [], "failed_results": [], "response_time": 0.0, "request_id": ""}


    def retrieve_chunks(self, question: str) -> dict[str, Any]:
        chunks = []
        aggs = []
        logging.info("[Tavily]Q: " + question)
        for r in self.search(question):
            id = get_uuid()
            chunks.append({
                "chunk_id": id,
                "content_ltks": rag_tokenizer.tokenize(r["content"]),
                "content_with_weight": r["content"],
                "doc_id": id,
                "docnm_kwd": r["title"],
                "kb_id": [],
                "important_kwd": [],
                "image_id": "",
                "similarity": r["score"],
                "vector_similarity": 1.,
                "term_similarity": 0,
                "vector": [],
                "positions": [],
                "url": r["url"]
            })
            aggs.append({
                "doc_name": r["title"],
                "doc_id": id,
                "count": 1,
                "url": r["url"]
            })
            logging.info("[Tavily]R: "+r["content"][:128]+"...")
        return {"chunks": chunks, "doc_aggs": aggs}