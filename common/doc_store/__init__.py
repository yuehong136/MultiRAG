#
#  Copyright 2025 The MultiRAG Authors. All Rights Reserved.
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

from common.doc_store.doc_store_base import (
    DocStoreConnection,
    MatchExpr,
    MatchTextExpr,
    MatchDenseExpr,
    MatchSparseExpr,
    MatchTensorExpr,
    FusionExpr,
    OrderByExpr,
    SparseVector,
    VEC,
    DEFAULT_MATCH_VECTOR_TOPN,
    DEFAULT_MATCH_SPARSE_TOPN,
)

# Base classes are imported lazily to avoid import errors when dependencies are not installed
# Use: from common.doc_store.milvus_conn_base import MilvusConnectionBase
# Use: from common.doc_store.es_conn_base import ESConnectionBase
# Use: from common.doc_store.infinity_conn_base import InfinityConnectionBase

__all__ = [
    "DocStoreConnection",
    "MatchExpr",
    "MatchTextExpr",
    "MatchDenseExpr",
    "MatchSparseExpr",
    "MatchTensorExpr",
    "FusionExpr",
    "OrderByExpr",
    "SparseVector",
    "VEC",
    "DEFAULT_MATCH_VECTOR_TOPN",
    "DEFAULT_MATCH_SPARSE_TOPN",
]
